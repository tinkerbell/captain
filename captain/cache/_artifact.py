"""ORAS-style cache artifacts: one deterministic tar layer of an output directory."""

from __future__ import annotations

import gzip
import hashlib
import logging
import tarfile
from collections.abc import Iterable
from pathlib import Path

from captain import progress
from captain.util import safe_extractall

from ._keys import CacheKey
from ._registry import OCI_MANIFEST, Registry, RegistryError

log = logging.getLogger(__name__)

ARTIFACT_TYPE = "application/vnd.tinkerbell.captain.cache.v1.{kind}"
EMPTY_CONFIG = b"{}"
EMPTY_CONFIG_DESCRIPTOR = {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": f"sha256:{hashlib.sha256(EMPTY_CONFIG).hexdigest()}",
    "size": len(EMPTY_CONFIG),
}
INPUTS_ANNOTATION = "io.tinkerbell.captain.cache.inputs"
TAR = "application/vnd.oci.image.layer.v1.tar"
TAR_GZIP = f"{TAR}+gzip"

# Kinds whose outputs are not already compressed.
_GZIP_KINDS = frozenset(["tools", "kernel"])


def _file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
    return f"sha256:{sha.hexdigest()}"


def _deterministic_tar(src_dir: Path, tar_path: Path, compress: bool, exclude: Iterable[str]):
    """Tar *src_dir* sorted, with zeroed times/owners; file modes are kept."""
    excluded = set(exclude)
    paths = sorted(
        p
        for p in src_dir.rglob("*")
        if not excluded.intersection(p.relative_to(src_dir).parts) and (p.is_file() or p.is_dir())
    )
    raw = open(tar_path, "wb")  # noqa: SIM115 - closed below, possibly via GzipFile
    fileobj = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) if compress else raw
    try:
        with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tf:
            for path in paths:
                info = tf.gettarinfo(str(path), arcname=str(path.relative_to(src_dir)))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.isfile():
                    with open(path, "rb") as f:
                        tf.addfile(info, f)
                else:
                    tf.addfile(info)
    finally:
        fileobj.close()
        raw.close()


def push(
    registry: Registry,
    repo: str,
    key: CacheKey,
    src_dir: Path,
    work_dir: Path,
    *,
    source_url: str | None,
    exclude: Iterable[str] = (),
) -> None:
    compress = key.kind in _GZIP_KINDS
    tar_path = work_dir / f"{key.tag}.tar{'.gz' if compress else ''}"
    try:
        log.info("Packing %s for the cache...", src_dir)
        with progress.status(f"Packing {key}..."):
            _deterministic_tar(src_dir, tar_path, compress, exclude)
            digest, size = _file_digest(tar_path), tar_path.stat().st_size
        registry.upload_blob(
            repo, EMPTY_CONFIG_DESCRIPTOR["digest"], len(EMPTY_CONFIG), EMPTY_CONFIG
        )
        if registry.blob_exists(repo, digest):
            log.info("Layer of %s is already in %s/%s", key.tag, registry.host, repo)
        else:
            log.info(
                "Uploading %s (%.1fM) to %s/%s", key.tag, size / (1 << 20), registry.host, repo
            )
            with progress.transfer(f"    Uploading {key.kind}", size) as advance:
                registry.upload_blob(repo, digest, size, tar_path, advance)
    finally:
        tar_path.unlink(missing_ok=True)

    annotations = {INPUTS_ANNOTATION: key.canonical}
    if source_url:
        # Links the ghcr.io package to its GitHub repo (so GITHUB_TOKEN can keep pushing).
        annotations["org.opencontainers.image.source"] = source_url
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "artifactType": ARTIFACT_TYPE.format(kind=key.kind),
        "config": EMPTY_CONFIG_DESCRIPTOR,
        "layers": [
            {
                "mediaType": TAR_GZIP if compress else TAR,
                "digest": digest,
                "size": size,
                "annotations": {"org.opencontainers.image.title": tar_path.name},
            }
        ],
        "annotations": annotations,
    }
    registry.put_manifest(repo, key.tag, manifest)


def pull(registry: Registry, repo: str, key: CacheKey, dest_dir: Path, work_dir: Path) -> bool:
    """Fetch *key* into *dest_dir* (which must not exist). False if not in *repo*."""
    manifest = registry.get_manifest(repo, key.tag)
    if manifest is None:
        return False
    if manifest.get("annotations", {}).get(INPUTS_ANNOTATION) != key.canonical:
        raise RegistryError(f"{repo}:{key.tag} was built from different inputs; ignoring it")
    layers = manifest.get("layers", [])
    if len(layers) != 1:
        raise RegistryError(f"{repo}:{key.tag} has {len(layers)} layers, expected 1")
    layer = layers[0]
    blob = work_dir / layer["digest"].replace(":", "-")
    log.info(
        "Downloading %s (%.1fM) from %s/%s", key.tag, layer["size"] / (1 << 20), registry.host, repo
    )
    try:
        with progress.transfer(f"    Downloading {key.kind}", layer["size"]) as advance:
            registry.get_blob(repo, layer["digest"], blob, advance)
        dest_dir.mkdir(parents=True)
        with progress.status(f"Extracting {key}..."), tarfile.open(blob, "r:*") as tf:
            safe_extractall(tf, dest_dir)
    finally:
        blob.unlink(missing_ok=True)
    return True
