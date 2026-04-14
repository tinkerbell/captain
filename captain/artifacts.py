"""Collect build artifacts into out/."""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from captain.config import Config
from captain.util import ensure_dir

log = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human_size(size: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f}T"


def collect_kernel(cfg: Config) -> None:
    """Copy the kernel image from mkosi.output/kernel/{version}/{arch}/ to out/."""
    out = ensure_dir(cfg.output_dir)
    vmlinuz_dir = cfg.kernel_output
    vmlinuz_files = sorted(vmlinuz_dir.glob("vmlinuz-*")) if vmlinuz_dir.is_dir() else []
    if vmlinuz_files:
        vmlinuz_src = vmlinuz_files[0]
        vmlinuz_dst = out / f"vmlinuz-{cfg.kernel_version}-{cfg.arch_info.output_arch}"
        shutil.copy2(vmlinuz_src, vmlinuz_dst)
        log.info("kernel: %s (%s)", vmlinuz_dst, _human_size(vmlinuz_dst.stat().st_size))
    else:
        log.warning("No kernel image found in %s", cfg.kernel_output)


def collect_initramfs(cfg: Config) -> None:
    """Copy the initramfs CPIO from mkosi.output/initramfs/{arch}/ to out/."""
    out = ensure_dir(cfg.output_dir)
    cpio_files = sorted(cfg.initramfs_output.glob("*.cpio*"))
    if cpio_files:
        initrd_src = cpio_files[0]
        initrd_dst = out / f"initramfs-{cfg.kernel_version}-{cfg.arch_info.output_arch}"
        shutil.copy2(initrd_src, initrd_dst)
        log.info("initramfs: %s (%s)", initrd_dst, _human_size(initrd_dst.stat().st_size))
    else:
        log.warning("No initramfs CPIO found in %s", cfg.initramfs_output)


def collect_iso(cfg: Config) -> None:
    """Copy the ISO image from mkosi.output/iso/{arch}/ to out/."""
    out = ensure_dir(cfg.output_dir)
    iso_dir = cfg.iso_output
    iso_files = sorted(iso_dir.glob("*.iso")) if iso_dir.is_dir() else []
    if iso_files:
        iso_src = iso_files[0]
        iso_dst = out / f"captainos-{cfg.kernel_version}-{cfg.arch_info.output_arch}.iso"
        shutil.copy2(iso_src, iso_dst)
        log.info("iso: %s (%s)", iso_dst, _human_size(iso_dst.stat().st_size))


def collect_checksums(
    files: list[Path],
    output: Path,
) -> None:
    """Compute SHA-256 checksums for *files* and write them to *output*.

    The checksum file uses the standard ``sha256sum`` format::

        <hex-digest>  <filename>

    Only the bare filename (no directory component) is recorded so that
    ``sha256sum -c`` works from the directory containing the files.
    """
    lines: list[str] = []
    for path in files:
        if not path.is_file():
            log.warning("Skipping missing file: %s", path)
            continue
        digest = _sha256(path)
        lines.append(f"{digest}  {path.name}")
    if lines:
        content = "\n".join(lines) + "\n"
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_file() and output.read_text() == content:
            log.info("Checksums unchanged: %s", output)
        else:
            output.write_text(content)
            log.info("Wrote checksums to %s", output)
        for line in lines:
            log.info("  %s", line)
    else:
        log.warning(
            "No checksums were written for %d requested file(s); "
            "no output checksum file was created.",
            len(files),
        )


def collect(cfg: Config) -> None:
    """Copy initramfs, kernel, and ISO images from mkosi.output/ to out/."""
    log.info("Collecting build artifacts...")
    collect_initramfs(cfg)
    collect_kernel(cfg)
    collect_iso(cfg)
