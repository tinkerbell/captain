"""OCI image building and layer creation."""

from __future__ import annotations

import contextlib
import logging
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from captain import artifacts, buildah
from captain.util import get_arch_info

log = logging.getLogger(__name__)


def _deterministic_tar(file_path: Path, output_dir: Path) -> Path:
    """Create a tar containing a single file with deterministic metadata.

    Zeroes mtime, uid, gid, uname, gname and uses fixed permissions so
    that the same file content always produces byte-identical tar bytes
    and therefore the same OCI layer digest.
    """
    tar_path = output_dir / f".layer-{file_path.name}.tar"
    with tarfile.open(tar_path, "w") as tf:
        info = tf.gettarinfo(str(file_path), arcname=file_path.name)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.mode = 0o644
        with open(file_path, "rb") as fh:
            tf.addfile(info, fh)
    return tar_path


def _collect_arch_artifacts(
    project_dir: Path,
    out: Path,
    arch: str,
    kernel_version: str,
) -> list[Path]:
    """Collect and return the artifact files for a single architecture.

    Returns [vmlinuz, initramfs, iso, checksums] paths in *out*.
    """
    # Collect kernel
    vmlinuz_dir = project_dir / "mkosi.output" / "kernel" / kernel_version / arch
    vmlinuz_files = sorted(vmlinuz_dir.glob("vmlinuz-*")) if vmlinuz_dir.is_dir() else []
    oarch = get_arch_info(arch).output_arch
    vmlinuz_dst = out / f"vmlinuz-{kernel_version}-{oarch}"
    if vmlinuz_files:
        shutil.copy2(vmlinuz_files[0], vmlinuz_dst)
        log.info("kernel: %s", vmlinuz_dst)
    else:
        log.warning("No kernel image found for %s", arch)

    arch_files = [
        out / f"vmlinuz-{kernel_version}-{oarch}",
        out / f"initramfs-{kernel_version}-{oarch}",
        out / f"captainos-{kernel_version}-{oarch}.iso",
    ]
    checksums_path = out / f"sha256sums-{kernel_version}-{oarch}.txt"
    artifacts.collect_checksums(arch_files, checksums_path)

    push_files = [*arch_files, checksums_path]
    for f in push_files:
        if not f.is_file():
            log.error("Missing artifact: %s", f)
            raise SystemExit(1)
    return push_files


def _build_platform_image(
    layer_tars: list[Path],
    platform: str,
    sha: str,
    repository: str,
    *,
    created: str,
    tag: str,
    artifact_name: str,
    base: str = "scratch",
) -> str:
    """Build an OCI image locally for *platform*.

    Each tar becomes its own OCI layer (add → commit cycle).  All commits
    use the same fixed timestamp; only the final commit carries the image
    metadata.

    *base* is the starting image — ``"scratch"`` for a new image, or a
    ``docker://`` ref to extend an existing registry image.  When a
    registry ref is used the inherited layers keep their original blob
    digests.

    Returns the local image ID (not pushed yet — the caller adds it
    to a manifest list and pushes everything via ``manifest push --all``).
    """
    os_name, arch = platform.split("/")
    epoch = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
    oci_metadata = {
        "org.opencontainers.image.created": created,
        "org.opencontainers.image.source": f"https://github.com/{repository}",
        "org.opencontainers.image.revision": sha,
        "org.opencontainers.image.version": tag,
        "org.opencontainers.image.title": artifact_name,
        "org.opencontainers.image.description": "CaptainOS build artifacts",
        "org.opencontainers.image.vendor": "Tinkerbell",
        "org.opencontainers.image.licenses": "Apache-2.0",
    }

    # Build one layer per tar: from base → add file → commit → repeat.
    # Track intermediate image IDs so they can be cleaned up afterwards;
    # only the final image (returned to the caller) is kept.
    current: str = base
    intermediates: list[str] = []
    for i, tar_path in enumerate(layer_tars):
        is_last = i == len(layer_tars) - 1
        ctr = buildah.from_image(current, platform=platform)
        buildah.add(ctr, [tar_path])
        if is_last:
            buildah.config(
                ctr,
                os=os_name,
                arch=arch,
                annotations=oci_metadata,
                labels=oci_metadata,
            )
        prev = current
        current = buildah.commit(ctr, timestamp=epoch)
        if prev != base:
            intermediates.append(prev)

    for img in intermediates:
        with contextlib.suppress(Exception):
            buildah.rmi(img)

    return current
