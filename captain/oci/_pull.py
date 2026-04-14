"""Pull and tag operations for OCI artifacts."""

from __future__ import annotations

import logging
from pathlib import Path

from captain import skopeo

from ._common import _ARCHES, _image_ref

log = logging.getLogger(__name__)


def pull(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    target: str,
    output_dir: Path,
) -> None:
    """Pull and extract OCI artifacts.

    *target* may be ``"amd64"``, ``"arm64"``, or ``"combined"``.  The tag
    suffix is ``-{target}`` for single architectures, or bare ``{tag}``
    for ``"combined"``.
    """
    tag_suffix = "" if target == "combined" else f"-{target}"
    ref = _image_ref(registry, repository, artifact_name, f"{tag}{tag_suffix}")
    skopeo.export_image(ref, output_dir)

    # Recap
    extracted = sorted(f.name for f in Path(output_dir).iterdir() if f.is_file())
    log.info("")
    log.info("Pull complete")
    log.info("  Image:  %s", ref)
    log.info("  Target: %s", target)
    log.info("  Artifacts:")
    for name in extracted:
        log.info("    - %s", name)


def tag_image(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    src_tag: str,
    new_tag: str,
) -> None:
    """Tag an existing OCI artifact image with a new version."""
    src_ref = _image_ref(registry, repository, artifact_name, src_tag)
    dest_ref = _image_ref(registry, repository, artifact_name, new_tag)
    skopeo.copy(src_ref, dest_ref)
    log.info("Tagged %s → %s", src_ref, new_tag)


def tag_all(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    src_tag: str,
    new_tag: str,
    arches: list[str] | None = None,
) -> None:
    """Tag all artifact images (per-arch + combined) with a new version."""
    arches = arches or list(_ARCHES)
    for a in arches:
        tag_image(
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            src_tag=f"{src_tag}-{a}",
            new_tag=f"{new_tag}-{a}",
        )
    # Tag the combined image (no arch suffix).
    tag_image(
        registry=registry,
        repository=repository,
        artifact_name=artifact_name,
        src_tag=src_tag,
        new_tag=new_tag,
    )

    # Recap
    image = f"{registry}/{repository}/{artifact_name}"
    log.info("")
    log.info("Tag complete")
    log.info("  Image:  %s", image)
    for a in arches:
        log.info("  %s-%s  →  %s-%s", src_tag, a, new_tag, a)
    log.info("  %s  →  %s", src_tag, new_tag)
