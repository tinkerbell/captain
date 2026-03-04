"""Collect build artifacts into out/."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from captain.config import Config
from captain.log import StageLogger, for_stage
from captain.util import ensure_dir

_default_log = for_stage("artifacts")


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


def collect_kernel(cfg: Config, logger: StageLogger | None = None) -> None:
    """Copy the kernel image from mkosi.output/vmlinuz/{arch}/ to out/."""
    _log = logger or _default_log
    out = ensure_dir(cfg.output_dir)
    vmlinuz_dir = cfg.vmlinuz_output
    vmlinuz_files = sorted(vmlinuz_dir.glob("vmlinuz-*")) if vmlinuz_dir.is_dir() else []
    if vmlinuz_files:
        vmlinuz_src = vmlinuz_files[0]
        vmlinuz_dst = out / f"vmlinuz-{cfg.arch}"
        shutil.copy2(vmlinuz_src, vmlinuz_dst)
        _log.log(f"kernel: {vmlinuz_dst} ({_human_size(vmlinuz_dst.stat().st_size)})")
    else:
        _log.warn("No kernel image found in mkosi.output/vmlinuz/{arch}/")


def collect_initramfs(cfg: Config, logger: StageLogger | None = None) -> None:
    """Copy the initramfs CPIO from mkosi.output/initramfs/{arch}/ to out/."""
    _log = logger or _default_log
    out = ensure_dir(cfg.output_dir)
    cpio_files = sorted(cfg.initramfs_output.glob("*.cpio*"))
    if cpio_files:
        initrd_src = cpio_files[0]
        initrd_dst = out / f"initramfs-{cfg.arch}.cpio.zst"
        shutil.copy2(initrd_src, initrd_dst)
        _log.log(f"initramfs: {initrd_dst} ({_human_size(initrd_dst.stat().st_size)})")
    else:
        _log.warn("No initramfs CPIO found in mkosi.output/initramfs/{arch}/")


def collect_iso(cfg: Config, logger: StageLogger | None = None) -> None:
    """Copy the ISO image from mkosi.output/iso/{arch}/ to out/."""
    _log = logger or _default_log
    out = ensure_dir(cfg.output_dir)
    iso_dir = cfg.iso_output
    iso_files = sorted(iso_dir.glob("*.iso")) if iso_dir.is_dir() else []
    if iso_files:
        iso_src = iso_files[0]
        iso_dst = out / f"captainos-{cfg.arch}.iso"
        shutil.copy2(iso_src, iso_dst)
        _log.log(f"iso: {iso_dst} ({_human_size(iso_dst.stat().st_size)})")


def collect_checksums(cfg: Config, logger: StageLogger | None = None) -> None:
    """Print SHA-256 checksums for all artifacts in out/."""
    _log = logger or _default_log
    out = cfg.output_dir
    if not out.is_dir():
        return
    artifact_files = sorted(f for f in out.iterdir() if f.is_file())
    if artifact_files:
        _log.log("Checksums:")
        for artifact in artifact_files:
            digest = _sha256(artifact)
            print(f"  {digest}  {artifact}")


def collect(cfg: Config, logger: StageLogger | None = None) -> None:
    """Copy initramfs, kernel, and ISO images from mkosi.output/ to out/."""
    _log = logger or _default_log
    _log.log("Collecting build artifacts...")
    collect_initramfs(cfg, logger=_log)
    collect_kernel(cfg, logger=_log)
    collect_iso(cfg, logger=_log)
    collect_checksums(cfg, logger=_log)
