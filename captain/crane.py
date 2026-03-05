"""Thin wrapper around the ``crane`` CLI for OCI image operations.

Each artifact file is pushed as its own tar layer via ``crane append``,
producing a valid OCI image with correct ``rootfs.diff_ids`` in the
config.  This means:

* **containerd** can pull and unpack the image (Kubernetes image volumes
  work).
* **crane export** extracts the individual files for release workflows.
* ``crane index append`` / ``crane tag`` manage multi-arch indexes.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

from captain.log import StageLogger, for_stage
from captain.util import run

_default_log = for_stage("crane")


def append(
    tar_path: Path,
    image_ref: str,
    *,
    base: str | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Append *tar_path* as a new layer to *image_ref*.

    When *base* is given the layer is appended on top of the existing
    image at *base*; otherwise a new single-layer image is created.
    """
    _log = logger or _default_log
    cmd: list[str] = ["crane", "append", "-f", str(tar_path), "-t", image_ref]
    if base:
        cmd += ["-b", base]
    _log.log(f"crane append → {image_ref}")
    run(cmd)


def mutate(
    image_ref: str,
    *,
    platform: str | None = None,
    annotations: dict[str, str] | None = None,
    tag: str | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Mutate metadata on *image_ref* (platform, annotations, re-tag)."""
    _log = logger or _default_log
    cmd: list[str] = ["crane", "mutate", image_ref]
    if platform:
        cmd += ["--set-platform", platform]
    for key, value in (annotations or {}).items():
        cmd += ["-a", f"{key}={value}"]
    if tag:
        cmd += ["-t", tag]
    _log.log(f"crane mutate {image_ref}")
    run(cmd)


def _safe_tar_extract(tar: tarfile.TarFile, output_dir: Path) -> None:
    """Extract *tar* members into *output_dir*, rejecting unsafe paths.

    Prevents path-traversal attacks where a malicious image could contain
    entries with ``../`` or absolute paths that write outside the target
    directory.
    """
    resolved_base = output_dir.resolve()
    for member in tar:
        member_path = os.path.normpath(member.name)
        if os.path.isabs(member_path) or member_path.startswith(".."):
            raise ValueError(f"Refusing to extract tar member with unsafe path: {member.name!r}")
        dest = (resolved_base / member_path).resolve()
        if not str(dest).startswith(str(resolved_base) + os.sep) and dest != resolved_base:
            raise ValueError(f"Tar member escapes output directory: {member.name!r}")
        tar.extract(member, path=output_dir)


def export_image(
    image_ref: str,
    output_dir: Path,
    *,
    platform: str | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Export the filesystem of *image_ref* into *output_dir*.

    Streams ``crane export`` output through Python's :mod:`tarfile` for
    extraction with path-traversal validation, preventing malicious
    images from writing outside the target directory.
    """
    _log = logger or _default_log
    output_dir.mkdir(parents=True, exist_ok=True)

    crane_cmd: list[str] = ["crane", "export"]
    if platform:
        crane_cmd += ["--platform", platform]
    crane_cmd += [image_ref, "-"]

    _log.log(f"crane export {image_ref} → {output_dir}")
    crane_proc = subprocess.Popen(crane_cmd, stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=crane_proc.stdout, mode="r|") as tf:
            _safe_tar_extract(tf, output_dir)
    finally:
        crane_proc.stdout.close()  # type: ignore[union-attr]
    crane_rc = crane_proc.wait()
    if crane_rc != 0:
        raise subprocess.CalledProcessError(crane_rc, crane_cmd)


def index_append(
    index_ref: str,
    manifests: list[str],
    *,
    logger: StageLogger | None = None,
) -> None:
    """Create an OCI index at *index_ref* from per-arch *manifests*."""
    _log = logger or _default_log
    cmd: list[str] = ["crane", "index", "append", "-t", index_ref]
    for m in manifests:
        cmd += ["-m", m]
    _log.log(f"crane index append → {index_ref}")
    run(cmd)


def tag(src_ref: str, new_tag: str, *, logger: StageLogger | None = None) -> None:
    """Tag *src_ref* with *new_tag*."""
    _log = logger or _default_log
    _log.log(f"crane tag {src_ref} {new_tag}")
    run(["crane", "tag", src_ref, new_tag])


def delete(image_ref: str, *, logger: StageLogger | None = None) -> None:
    """Delete *image_ref* from the registry."""
    _log = logger or _default_log
    _log.log(f"crane delete {image_ref}")
    run(["crane", "delete", image_ref])
