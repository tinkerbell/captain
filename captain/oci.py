"""High-level OCI artifact operations for publishing and retrieving releases.

Each artifact file is stored as its own tar layer in a proper OCI image
so that:

* **containerd** can pull it (valid ``rootfs.diff_ids`` in the config) —
  Kubernetes image-volume mounts work.
* **crane export** extracts individual files for release workflows.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

from captain import artifacts, crane
from captain.config import Config
from captain.log import StageLogger, for_stage
from captain.util import ensure_dir

_default_log = for_stage("release")

_ARCHES = ("amd64", "arm64")


def _image_ref(registry: str, repository: str, artifact_name: str, tag: str) -> str:
    """Build a fully-qualified OCI image reference."""
    return f"{registry}/{repository}/{artifact_name}:{tag}"


def compute_version_tag(
    project_dir: Path,
    sha: str,
    *,
    exclude: str | None = None,
) -> str:
    """Compute the version tag from git describe + short SHA.

    Mirrors the CI logic::

        VERSION=$(git describe --tags --first-parent --abbrev=0 \\
                    --match 'v[0-9]*' 2>/dev/null || echo "v0.0.0")
        TAG="${VERSION}-${SHA::7}"
    """
    cmd = [
        "git",
        "describe",
        "--tags",
        "--first-parent",
        "--abbrev=0",
        "--match",
        "v[0-9]*",
    ]
    if exclude:
        cmd += [f"--exclude={exclude}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=project_dir,
        )
        version = result.stdout.strip()
    except subprocess.CalledProcessError:
        version = "v0.0.0"
    return f"{version}-{sha[:7]}"


def publish(
    cfg: Config,
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    sha: str,
    logger: StageLogger | None = None,
) -> None:
    """Collect artifacts, compute checksums, and push to an OCI registry.

    This replaces the inline shell in the ``publish-artifacts`` CI job.
    """
    _log = logger or _default_log
    arch = cfg.arch
    out = ensure_dir(cfg.output_dir)

    # Collect vmlinuz into out/
    artifacts.collect_kernel(cfg, logger=_log)

    # Compute checksums for the three main artifacts
    arch_files = [
        out / f"vmlinuz-{arch}",
        out / f"initramfs-{arch}.cpio.zst",
        out / f"captainos-{arch}.iso",
    ]
    checksums_path = out / f"sha256sums-{arch}.txt"
    artifacts.collect_checksums(arch_files, checksums_path, logger=_log)

    # Verify all files exist
    push_files = [*arch_files, checksums_path]
    for f in push_files:
        if not f.is_file():
            _log.err(f"Missing artifact: {f}")
            raise SystemExit(1)

    # Build a multi-layer OCI image — one layer per artifact file.
    # Each layer is a tar containing a single file at the root,
    # so the image filesystem exposes individual files.  crane computes
    # rootfs.diff_ids automatically, keeping containerd happy.
    #
    # Layers are appended to a temporary ref so that a partial failure
    # never leaves the final tag pointing at an incomplete image.
    ref = _image_ref(registry, repository, artifact_name, f"{tag}-{arch}")
    wip_ref = f"{ref}-wip"
    layer_tars: list[Path] = []
    try:
        for i, f in enumerate(push_files):
            layer_tar = out / f".layer-{f.name}.tar"
            layer_tars.append(layer_tar)
            with tarfile.open(layer_tar, "w") as tf:
                tf.add(f, arcname=f.name)
            crane.append(
                layer_tar,
                wip_ref,
                base=wip_ref if i > 0 else None,
                logger=_log,
            )
        # All layers succeeded — set metadata and promote to the final tag.
        crane.mutate(
            wip_ref,
            platform=f"linux/{arch}",
            annotations={
                "org.opencontainers.image.source": f"https://github.com/{repository}",
                "org.opencontainers.image.revision": sha,
            },
            tag=ref,
            logger=_log,
        )
    finally:
        # Best-effort cleanup of the temporary WIP tag (both on success
        # and failure) so partial builds don't accumulate in the registry.
        try:
            crane.delete(wip_ref, logger=_log)
        except subprocess.CalledProcessError:
            _log.log(f"Warning: could not delete temporary tag {wip_ref}")
        for t in layer_tars:
            t.unlink(missing_ok=True)

    _log.log(f"Published {arch} artifacts → {ref}")


def create_index(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    arches: list[str] | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Create a multi-arch OCI index from per-arch manifests."""
    _log = logger or _default_log
    arches = arches or list(_ARCHES)
    index_ref = _image_ref(registry, repository, artifact_name, tag)
    manifests = [_image_ref(registry, repository, artifact_name, f"{tag}-{a}") for a in arches]
    crane.index_append(index_ref, manifests, logger=_log)
    _log.log(f"Created index → {index_ref}")


def pull(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    arch: str,
    output_dir: Path,
    logger: StageLogger | None = None,
) -> None:
    """Pull and extract OCI artifacts for a single architecture."""
    _log = logger or _default_log
    ref = _image_ref(registry, repository, artifact_name, tag)
    crane.export_image(ref, output_dir, platform=f"linux/{arch}", logger=_log)
    _log.log(f"Pulled {arch} artifacts → {output_dir}")


def tag_image(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    src_tag: str,
    new_tag: str,
    logger: StageLogger | None = None,
) -> None:
    """Tag an existing OCI artifact index with a new version."""
    _log = logger or _default_log
    ref = _image_ref(registry, repository, artifact_name, src_tag)
    crane.tag(ref, new_tag, logger=_log)
    _log.log(f"Tagged {ref} → {new_tag}")
