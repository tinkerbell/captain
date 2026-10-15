"""Build a UEFI-bootable live ISO from the vmlinuz + initramfs artifacts."""

from __future__ import annotations

import logging
import mmap
import shutil
import textwrap
from pathlib import Path

from captain.config import Config
from captain.util import ensure_dir, run

log = logging.getLogger(__name__)

# GRUB platform directory name per architecture.
_GRUB_PLATFORM = {
    "amd64": "x86_64-efi",
    "arm64": "arm64-efi",
}

# Console devices per architecture for the kernel cmdline.
_CONSOLE_ARGS = {
    "amd64": "console=tty0 console=ttyS0,115200",
    "arm64": "console=tty0 console=ttyAMA0,115200",
}

# Path of the bootstrap data slot inside the ISO filesystem. GRUB loads it as a
# second initrd so the kernel extracts it alongside the OS initramfs. Tinkerbell
# substitutes its bytes per host while serving the shared ISO, so its size is
# fixed at build time and never changes afterwards.
#
# Local to this build: Tinkerbell finds the slot by its sector headers, not by
# name, so this only has to agree with the initrd line in _grub_cfg.
BOOTSTRAP_SLOT_ISO_PATH = "/boot/bootstrap.cpio"

# Reserved capacity of the slot, a whole number of sectors. Raising it requires
# a new ISO release; changes that fit within it do not. Tinkerbell reads it from
# the sector headers, so it may differ per flavor.
BOOTSTRAP_SLOT_CAPACITY = 4 * 1024 * 1024

# The slot contract with Tinkerbell (smee/internal/bootstrap/slot.go). Every
# sector of the unpersonalized slot begins with a header identifying it, so
# Tinkerbell finds the slot in the bytes it serves, with no metadata to publish.
# Each header is a newc TRAILER!!! record, an empty archive, so an
# unpersonalized slot extracts as nothing and the ISO still boots. The record's
# otherwise unused fields carry:
#
#   c_mtime, c_devmajor, c_devminor: BOOTSTRAP_SLOT_MAGIC
#   c_rdevmajor:                     BOOTSTRAP_SLOT_VERSION
#   c_rdevminor:                     the sector's index
#   c_check:                         the slot's sector count
#
# Bump the version only when the meaning of these fields changes.
BOOTSTRAP_SLOT_SECTOR_SIZE = 2048
BOOTSTRAP_SLOT_VERSION = 1
BOOTSTRAP_SLOT_MAGIC = (0x1E2D339C, 0xF7CBD880, 0x9A9E3913)


def _grub_cfg(arch: str) -> str:
    """Generate the GRUB configuration for the ISO."""
    console = _CONSOLE_ARGS.get(arch, "console=tty0")
    return textwrap.dedent(f"""\
        set timeout=5
        set default=0

        menuentry "CaptainOS" {{
            linux /boot/vmlinuz {console} 464vn90e7rbj08xbwdjejmdf4it17c5zfzjyfhthbh19eij201hjgit021bmpdb9ctrc87x2ymc8e7icu4ffi15x1hah9iyaiz38ckyap8hwx2vt5rm44ixv4hau8iw718q5yd019um5dt2xpqqa2rjtdypzr5v1gun8un110hhwp8cex7pqrh2ivh0ynpm4zkkwc8wcn367zyethzy7q8hzudyeyzx3cgmxqbkh825gcak7kxzjbgjajwizryv7ec1xm2h0hh7pz29qmvtgfjj1vphpgq1zcbiiehv52wrjy9yq473d9t1rvryy6929nk435hfx55du3ih05kn5tju3vijreru1p6knc988d4gfdz28eragvryq5x8aibe5trxd0t6t7jwxkde34v6pj1khmp50k6qqj3nzgcfzabtgqkmeqhdedbvwf3byfdma4nkv3rcxugaj2d0ru30pa2fqadjqrtjnv8bu52xzxv7irbhyvygygxu1nt5z4fh9w1vwbdcmagep26d298zknykf2e88kumt59ab7nq79d8amnhhvbexgh48e8qc61vq2e9qkihzt1twk1ijfgw70nwizai15iqyted2dt9gfmf2gg7amzufre79hwqkddc1cd935ywacnkrnak6r7xzcz7zbmq3kt04u2hg1iuupid8rt4nyrju51e6uejb2ruu36g9aibmz3hnmvazptu8x5tyxk820g2cdpxjdij766bt2n3djur7v623a2v44juyfgz80ekgfb9hkibpxh3zgknw8a34t4jifhf116x15cei9hwch0fye3xyq0acuym8uhitu5evc4rag3ui0fny3qg4kju7zkfyy8hwh537urd5uixkzwu5bdvafz4jmv7imypj543xg5em8jk8cgk7c4504xdd5e4e71ihaumt6u5u2t1w7um92fepzae8p0vq93wdrd1756npu1pziiur1payc7kmdwyxg3hj5n4phxbc29x0tcddamjrwt260b0w
            initrd /boot/initramfs {BOOTSTRAP_SLOT_ISO_PATH}
        }}
    """)


def _slot_sector_header(index: int, count: int) -> bytes:
    """Return the header of sector *index* of a *count* sector slot."""
    magic = BOOTSTRAP_SLOT_MAGIC
    name = b"TRAILER!!!\0"
    # ino, mode, uid, gid, nlink, mtime, filesize, devmajor, devminor
    fields = [0, 0, 0, 0, 1, magic[0], 0, magic[1], magic[2]]
    # rdevmajor, rdevminor, namesize, check
    fields += [BOOTSTRAP_SLOT_VERSION, index, len(name), count]
    out = b"070701" + b"".join(b"%08X" % f for f in fields) + name
    return out + b"\0" * (-len(out) % 4)


def _slot_placeholder() -> bytes:
    """Return the unpersonalized slot: every sector starts with its header."""
    size = BOOTSTRAP_SLOT_SECTOR_SIZE
    count = BOOTSTRAP_SLOT_CAPACITY // size
    return b"".join(_slot_sector_header(i, count).ljust(size, b"\0") for i in range(count))


def _stage_bootstrap_slot(boot_dir: Path) -> None:
    """Reserve the bootstrap slot in the ISO staging tree."""
    if BOOTSTRAP_SLOT_CAPACITY % BOOTSTRAP_SLOT_SECTOR_SIZE:
        log.error("Bootstrap slot capacity %d is not whole sectors", BOOTSTRAP_SLOT_CAPACITY)
        raise SystemExit(1)
    (boot_dir / Path(BOOTSTRAP_SLOT_ISO_PATH).name).write_bytes(_slot_placeholder())


def check_bootstrap_slot(iso_path: Path) -> int:
    """Return the slot's offset in *iso_path*, checking Tinkerbell will find exactly it.

    The slot must be stored intact and contiguous, and no other bytes in the
    ISO may look like one of its sector headers. Raises ValueError otherwise.
    """
    placeholder = _slot_placeholder()
    # The part of every header that does not vary: the newc magic through c_rdevmajor.
    prefix = placeholder[: 6 + 10 * 8]
    count = BOOTSTRAP_SLOT_CAPACITY // BOOTSTRAP_SLOT_SECTOR_SIZE
    with iso_path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        offset = mm.find(placeholder)
        if offset < 0:
            raise ValueError(f"bootstrap slot is not stored intact in {iso_path}")
        # So that each sector read of virtual media holds whole sector headers.
        if offset % BOOTSTRAP_SLOT_SECTOR_SIZE:
            raise ValueError(f"bootstrap slot at {offset} is not sector aligned in {iso_path}")
        headers = 0
        at = mm.find(prefix)
        while at >= 0:
            headers += 1
            at = mm.find(prefix, at + 1)
    if headers != count:
        raise ValueError(
            f"found {headers} bootstrap slot sector headers in {iso_path}, want {count}"
        )
    return offset


def _find_vmlinuz(cfg: Config) -> Path:
    """Locate the vmlinuz kernel image."""
    vmlinuz_files = sorted(cfg.initramfs_output.glob("*.vmlinuz*"))
    if not vmlinuz_files:
        log.error("No vmlinuz found in %s", cfg.initramfs_output)
        raise SystemExit(1)
    return vmlinuz_files[0]


def _find_initramfs(cfg: Config) -> Path:
    """Locate the initramfs CPIO image."""
    cpio_files = sorted(cfg.initramfs_output.glob("*.cpio*"))
    if not cpio_files:
        log.error("No initramfs CPIO found in %s", cfg.initramfs_output)
        log.error("Build the initramfs first: ./build.py initramfs")
        raise SystemExit(1)
    return cpio_files[0]


def build(cfg: Config) -> None:
    """Build a UEFI-bootable ISO image for the configured architecture.

    The ISO layout is::

        iso/{version}/{arch}/staging/
        ├── boot/
        │   ├── grub/
        │   │   └── grub.cfg
        │   ├── vmlinuz
        │   ├── initramfs
        │   └── bootstrap.cpio

    ``grub-mkrescue`` turns this into a bootable ISO with an embedded
    EFI System Partition.

    ``bootstrap.cpio`` is a fixed-capacity slot loaded as a second initrd: a run
    of empty CPIO archives, one per sector, that Tinkerbell overwrites in place
    while serving the ISO, so per-host data needs no ISO rebuild.
    """
    vmlinuz = _find_vmlinuz(cfg)
    initramfs = _find_initramfs(cfg)

    grub_platform = _GRUB_PLATFORM.get(cfg.arch)
    if grub_platform is None:
        log.error("Unsupported architecture for ISO build: %s", cfg.arch)
        raise SystemExit(1)

    staging = cfg.iso_staging
    if staging.exists():
        shutil.rmtree(staging)

    boot_dir = ensure_dir(staging / "boot")
    grub_dir = ensure_dir(boot_dir / "grub")

    log.info("Staging ISO filesystem at %s", staging)

    shutil.copy2(vmlinuz, boot_dir / "vmlinuz")
    shutil.copy2(initramfs, boot_dir / "initramfs")
    _stage_bootstrap_slot(boot_dir)

    (grub_dir / "grub.cfg").write_text(_grub_cfg(cfg.arch))

    iso_dir = ensure_dir(cfg.iso_output)
    iso_path = iso_dir / f"captainos-{cfg.flavor_id}-{cfg.arch_info.output_arch}.iso"
    # A later build reuses any ISO at iso_path whose slot checks out, and the
    # slot check cannot tell a truncated ISO from a complete one. So the ISO is
    # built and checked under another name, and only moved into place once both
    # succeed. The suffix keeps it out of the *.iso glob in collect_iso.
    partial_path = iso_path.with_name(iso_path.name + ".partial")
    partial_path.unlink(missing_ok=True)

    log.info("Building ISO with grub-mkrescue (%s)...", grub_platform)
    grub_mkrescue = shutil.which("grub-mkrescue")
    if grub_mkrescue is None:
        log.error("grub-mkrescue not found. Install grub-common or use ISO_MODE=docker.")
        raise SystemExit(1)

    try:
        run(
            [
                grub_mkrescue,
                f"--directory=/usr/lib/grub/{grub_platform}",
                "-o",
                str(partial_path),
                str(staging),
            ]
        )
        try:
            offset = check_bootstrap_slot(partial_path)
        except ValueError as err:
            log.error("%s", err)
            raise SystemExit(1) from err
        partial_path.replace(iso_path)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise

    size_mb = iso_path.stat().st_size / (1024 * 1024)
    log.info("ISO created: %s (%.1fM)", iso_path, size_mb)
    log.info(
        "Bootstrap slot %s reserved at offset %d (%d bytes)",
        BOOTSTRAP_SLOT_ISO_PATH,
        offset,
        BOOTSTRAP_SLOT_CAPACITY,
    )
