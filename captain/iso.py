"""Build a UEFI-bootable live ISO from the vmlinuz + initramfs artifacts."""

from __future__ import annotations

import logging
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


def _grub_cfg(arch: str) -> str:
    """Generate the GRUB configuration for the ISO."""
    console = _CONSOLE_ARGS.get(arch, "console=tty0")
    return textwrap.dedent(f"""\
        set timeout=5
        set default=0

        menuentry "CaptainOS" {{
            linux /boot/vmlinuz {console} 464vn90e7rbj08xbwdjejmdf4it17c5zfzjyfhthbh19eij201hjgit021bmpdb9ctrc87x2ymc8e7icu4ffi15x1hah9iyaiz38ckyap8hwx2vt5rm44ixv4hau8iw718q5yd019um5dt2xpqqa2rjtdypzr5v1gun8un110hhwp8cex7pqrh2ivh0ynpm4zkkwc8wcn367zyethzy7q8hzudyeyzx3cgmxqbkh825gcak7kxzjbgjajwizryv7ec1xm2h0hh7pz29qmvtgfjj1vphpgq1zcbiiehv52wrjy9yq473d9t1rvryy6929nk435hfx55du3ih05kn5tju3vijreru1p6knc988d4gfdz28eragvryq5x8aibe5trxd0t6t7jwxkde34v6pj1khmp50k6qqj3nzgcfzabtgqkmeqhdedbvwf3byfdma4nkv3rcxugaj2d0ru30pa2fqadjqrtjnv8bu52xzxv7irbhyvygygxu1nt5z4fh9w1vwbdcmagep26d298zknykf2e88kumt59ab7nq79d8amnhhvbexgh48e8qc61vq2e9qkihzt1twk1ijfgw70nwizai15iqyted2dt9gfmf2gg7amzufre79hwqkddc1cd935ywacnkrnak6r7xzcz7zbmq3kt04u2hg1iuupid8rt4nyrju51e6uejb2ruu36g9aibmz3hnmvazptu8x5tyxk820g2cdpxjdij766bt2n3djur7v623a2v44juyfgz80ekgfb9hkibpxh3zgknw8a34t4jifhf116x15cei9hwch0fye3xyq0acuym8uhitu5evc4rag3ui0fny3qg4kju7zkfyy8hwh537urd5uixkzwu5bdvafz4jmv7imypj543xg5em8jk8cgk7c4504xdd5e4e71ihaumt6u5u2t1w7um92fepzae8p0vq93wdrd1756npu1pziiur1payc7kmdwyxg3hj5n4phxbc29x0tcddamjrwt260b0w
            initrd /boot/initramfs
        }}
    """)


def _find_vmlinuz(cfg: Config) -> Path:
    """Locate the vmlinuz kernel image."""
    vmlinuz_dir = cfg.kernel_output
    candidates = sorted(vmlinuz_dir.glob("vmlinuz-*")) if vmlinuz_dir.is_dir() else []
    if not candidates:
        log.error("No vmlinuz found in %s", vmlinuz_dir)
        log.error("Build the kernel first: ./build.py kernel")
        raise SystemExit(1)
    return candidates[0]


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
        │   └── initramfs

    ``grub-mkrescue`` turns this into a bootable ISO with an embedded
    EFI System Partition.
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

    (grub_dir / "grub.cfg").write_text(_grub_cfg(cfg.arch))

    iso_dir = ensure_dir(cfg.iso_output)
    iso_path = iso_dir / f"captainos-{cfg.kernel_version}-{cfg.arch_info.output_arch}.iso"

    log.info("Building ISO with grub-mkrescue (%s)...", grub_platform)
    grub_mkrescue = shutil.which("grub-mkrescue")
    if grub_mkrescue is None:
        log.error("grub-mkrescue not found. Install grub-common or use ISO_MODE=docker.")
        raise SystemExit(1)

    run(
        [
            grub_mkrescue,
            f"--directory=/usr/lib/grub/{grub_platform}",
            "-o",
            str(iso_path),
            str(staging),
        ]
    )

    size_mb = iso_path.stat().st_size / (1024 * 1024)
    log.info("ISO created: %s (%.1fM)", iso_path, size_mb)
