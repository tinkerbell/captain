"""Thin wrapper around the ``buildah`` CLI for OCI image construction.

Images are built entirely locally (layers, metadata, timestamps) and
pushed as a single finished manifest — no intermediate manifests are
created on the registry.  This avoids the orphaned-untagged-manifest
problem caused by ``crane append`` rewriting tags per layer.

* **containerd** can pull and unpack the resulting images (valid
  ``rootfs.diff_ids`` in the config) — Kubernetes image volumes work.
* ``buildah manifest`` commands manage multi-arch OCI indexes.
"""

from __future__ import annotations

from pathlib import Path

from captain.log import StageLogger, for_stage
from captain.util import run

_default_log = for_stage("buildah")


def from_image(
    image: str,
    *,
    platform: str | None = None,
    logger: StageLogger | None = None,
) -> str:
    """Create a working container from *image* (local ID or ``scratch``).

    Returns the container ID.
    """
    _log = logger or _default_log
    cmd: list[str] = ["buildah", "from"]
    if platform:
        cmd += ["--platform", platform]
    cmd.append(image)
    _log.log(f"buildah from {image}")
    result = run(cmd, capture=True)
    return result.stdout.strip()


def add(
    container: str,
    files: list[Path],
    *,
    logger: StageLogger | None = None,
) -> None:
    """Add *files* into the root of *container*."""
    _log = logger or _default_log
    _log.log(f"buildah add {container} ({len(files)} files)")
    cmd: list[str] = ["buildah", "add", container]
    cmd += [str(f) for f in files]
    cmd.append("/")
    run(cmd)


def config(
    container: str,
    *,
    os: str | None = None,
    arch: str | None = None,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Set image metadata on *container*."""
    _log = logger or _default_log
    cmd: list[str] = ["buildah", "config"]
    if os:
        cmd += ["--os", os]
    if arch:
        cmd += ["--arch", arch]
    for key, value in (annotations or {}).items():
        cmd += ["--annotation", f"{key}={value}"]
    for key, value in (labels or {}).items():
        cmd += ["--label", f"{key}={value}"]
    cmd.append(container)
    _log.log(f"buildah config {container}")
    run(cmd)


def commit(
    container: str,
    *,
    timestamp: int | None = None,
    logger: StageLogger | None = None,
) -> str:
    """Commit *container* to a local image and remove the container.

    *timestamp* sets the creation timestamp (epoch seconds) for
    deterministic builds.  Returns the image ID.
    """
    _log = logger or _default_log
    _log.log(f"buildah commit {container}")
    cmd: list[str] = ["buildah", "commit", "--rm"]
    if timestamp is not None:
        cmd += ["--timestamp", str(timestamp)]
    cmd.append(container)
    result = run(cmd, capture=True)
    return result.stdout.strip()


def push(
    image_id: str,
    dest: str,
    *,
    logger: StageLogger | None = None,
) -> None:
    """Push *image_id* to a remote registry.

    *dest* should be a fully-qualified image reference (without the
    ``docker://`` transport prefix — it is added automatically).
    """
    _log = logger or _default_log
    _log.log(f"buildah push → {dest}")
    run(["buildah", "push", image_id, f"docker://{dest}"])


def manifest_create(
    ref: str,
    *,
    logger: StageLogger | None = None,
) -> str:
    """Create a new manifest list named *ref*.

    Returns the manifest list ID.
    """
    _log = logger or _default_log
    _log.log(f"buildah manifest create {ref}")
    result = run(["buildah", "manifest", "create", ref], capture=True)
    return result.stdout.strip()


def manifest_add(
    manifest: str,
    image: str,
    *,
    os: str | None = None,
    arch: str | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Add *image* to a manifest list."""
    _log = logger or _default_log
    cmd: list[str] = ["buildah", "manifest", "add"]
    if os:
        cmd += ["--os", os]
    if arch:
        cmd += ["--arch", arch]
    cmd += [manifest, image]
    _log.log(f"buildah manifest add {manifest} ← {image}")
    run(cmd)


def manifest_push(
    manifest: str,
    dest: str,
    *,
    logger: StageLogger | None = None,
) -> None:
    """Push *manifest* list (with all referenced images) to *dest*."""
    _log = logger or _default_log
    _log.log(f"buildah manifest push → {dest}")
    run(["buildah", "manifest", "push", "--all", manifest, f"docker://{dest}"])


def rmi(
    image: str,
    *,
    logger: StageLogger | None = None,
) -> None:
    """Remove a local image or manifest list."""
    _log = logger or _default_log
    _log.log(f"buildah rmi {image}")
    run(["buildah", "rmi", image])
