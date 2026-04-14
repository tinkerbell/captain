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

import logging
from pathlib import Path

from captain.util import run

log = logging.getLogger(__name__)


def from_image(
    image: str,
    *,
    platform: str | None = None,
) -> str:
    cmd: list[str] = ["buildah", "from"]
    if platform:
        cmd += ["--platform", platform]
    cmd.append(image)
    log.info("buildah from %s", image)
    result = run(cmd, capture=True)
    return result.stdout.strip()


def add(
    container: str,
    files: list[Path],
) -> None:
    log.info("buildah add %s (%d files)", container, len(files))
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
) -> None:
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
    log.info("buildah config %s", container)
    run(cmd)


def commit(
    container: str,
    *,
    timestamp: int | None = None,
) -> str:
    log.info("buildah commit %s", container)
    cmd: list[str] = ["buildah", "commit", "--rm"]
    if timestamp is not None:
        cmd += ["--timestamp", str(timestamp)]
    cmd.append(container)
    result = run(cmd, capture=True)
    return result.stdout.strip()


def push(
    image_id: str,
    dest: str,
) -> None:
    log.info("buildah push → %s", dest)
    run(["buildah", "push", image_id, f"docker://{dest}"])


def manifest_create(
    ref: str,
) -> str:
    log.info("buildah manifest create %s", ref)
    result = run(["buildah", "manifest", "create", ref], capture=True)
    return result.stdout.strip()


def manifest_add(
    manifest: str,
    image: str,
    *,
    os: str | None = None,
    arch: str | None = None,
) -> None:
    cmd: list[str] = ["buildah", "manifest", "add"]
    if os:
        cmd += ["--os", os]
    if arch:
        cmd += ["--arch", arch]
    cmd += [manifest, image]
    log.info("buildah manifest add %s ← %s", manifest, image)
    run(cmd)


def manifest_push(
    manifest: str,
    dest: str,
) -> None:
    log.info("buildah manifest push → %s", dest)
    run(["buildah", "manifest", "push", "--all", manifest, f"docker://{dest}"])


def rmi(
    image: str,
) -> None:
    log.info("buildah rmi %s", image)
    run(["buildah", "rmi", image])
