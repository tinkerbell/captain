"""Pull and tag operations for OCI artifacts."""

from __future__ import annotations

from pathlib import Path

from captain import skopeo
from captain.log import StageLogger

from ._common import _ARCHES, _default_log, _image_ref


def pull(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    target: str,
    output_dir: Path,
    logger: StageLogger | None = None,
) -> None:
    """Pull and extract OCI artifacts.

    *target* may be ``"amd64"``, ``"arm64"``, or ``"combined"``.  The tag
    suffix is ``-{target}`` for single architectures, or bare ``{tag}``
    for ``"combined"``.
    """
    _log = logger or _default_log
    tag_suffix = "" if target == "combined" else f"-{target}"
    ref = _image_ref(registry, repository, artifact_name, f"{tag}{tag_suffix}")
    skopeo.export_image(ref, output_dir, logger=_log)

    # Recap
    extracted = sorted(f.name for f in Path(output_dir).iterdir() if f.is_file())
    _log.log("")
    _log.log("Pull complete")
    _log.log(f"  Image:  {ref}")
    _log.log(f"  Target: {target}")
    _log.log("  Artifacts:")
    for name in extracted:
        _log.log(f"    - {name}")


def tag_image(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    src_tag: str,
    new_tag: str,
    logger: StageLogger | None = None,
) -> None:
    """Tag an existing OCI artifact image with a new version."""
    _log = logger or _default_log
    src_ref = _image_ref(registry, repository, artifact_name, src_tag)
    dest_ref = _image_ref(registry, repository, artifact_name, new_tag)
    skopeo.copy(src_ref, dest_ref, logger=_log)
    _log.log(f"Tagged {src_ref} → {new_tag}")


def tag_all(
    *,
    registry: str,
    repository: str,
    artifact_name: str,
    src_tag: str,
    new_tag: str,
    arches: list[str] | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Tag all artifact images (per-arch + combined) with a new version."""
    _log = logger or _default_log
    arches = arches or list(_ARCHES)
    for a in arches:
        tag_image(
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            src_tag=f"{src_tag}-{a}",
            new_tag=f"{new_tag}-{a}",
            logger=_log,
        )
    # Tag the combined image (no arch suffix).
    tag_image(
        registry=registry,
        repository=repository,
        artifact_name=artifact_name,
        src_tag=src_tag,
        new_tag=new_tag,
        logger=_log,
    )

    # Recap
    image = f"{registry}/{repository}/{artifact_name}"
    _log.log("")
    _log.log("Tag complete")
    _log.log(f"  Image:  {image}")
    for a in arches:
        _log.log(f"  {src_tag}-{a}  →  {new_tag}-{a}")
    _log.log(f"  {src_tag}  →  {new_tag}")
