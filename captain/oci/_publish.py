"""Publishing OCI artifacts to a registry."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from captain import buildah, skopeo
from captain.config import Config
from captain.util import ensure_dir

from ._build import _build_platform_image, _collect_arch_artifacts, _deterministic_tar
from ._common import _ARCHES, _image_ref

log = logging.getLogger(__name__)


def _create_push_cleanup(
    image_ids: list[str],
    dest_ref: str,
) -> None:
    """Create a manifest list from *image_ids*, push it to *dest_ref*, and clean up.

    Uses a temporary local manifest name to avoid collisions on repeated
    publishes.  After a successful (or failed) push, the local manifest
    and all *image_ids* are removed on a best-effort basis.
    """
    temp_name = f"captain-local-{uuid4().hex[:12]}"
    manifest_id: str | None = None
    try:
        manifest_id = buildah.manifest_create(temp_name)
        for image_id in image_ids:
            buildah.manifest_add(manifest_id, image_id)
        buildah.manifest_push(manifest_id, dest_ref)
    finally:
        if manifest_id is not None:
            with contextlib.suppress(Exception):
                buildah.rmi(manifest_id)
        for image_id in image_ids:
            with contextlib.suppress(Exception):
                buildah.rmi(image_id)


def _publish_single_arch(
    *,
    layer_tars: list[Path],
    ref: str,
    tag: str,
    sha: str,
    repository: str,
    artifact_name: str,
    created: str,
) -> None:
    """Build a per-arch multi-arch index and push it.

    Both platform entries (linux/amd64 and linux/arm64) carry the same
    4 layers.
    """
    image_ids: list[str] = []
    for platform_arch in _ARCHES:
        image_id = _build_platform_image(
            layer_tars,
            f"linux/{platform_arch}",
            sha,
            repository,
            created=created,
            tag=tag,
            artifact_name=artifact_name,
        )
        image_ids.append(image_id)

    _create_push_cleanup(image_ids, ref)


def _publish_combined(
    *,
    arch_layer_tars: dict[str, list[Path]],
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    sha: str,
    created: str,
    force: bool = False,
) -> bool:
    """Build and push the combined multi-arch image.

    Each platform manifest has the native arch's layers first, then the
    other arch's layers (8 layers total).  The native layers are
    inherited from the per-arch image in the registry so that blob
    digests match exactly between the per-arch and combined images.

    If the per-arch images don't exist in the registry yet (e.g.
    running ``--target combined`` locally with no prior per-arch publish),
    they are built and pushed first as a fallback.

    Skips the combined image if it already exists (unless *force*).
    """
    combined_ref = _image_ref(registry, repository, artifact_name, tag)

    # Skip if the combined image already exists.
    if not force and skopeo.image_exists(combined_ref):
        log.info("%s already exists — skipping (use --force to overwrite)", combined_ref)
        return False

    # Ensure per-arch images exist in the registry.
    for arch in _ARCHES:
        per_arch_tag = f"{tag}-{arch}"
        per_arch_ref = _image_ref(registry, repository, artifact_name, per_arch_tag)
        if skopeo.image_exists(per_arch_ref):
            log.info("Found %s in registry — will reuse layers for combined image", per_arch_ref)
        else:
            log.info(
                "%s not found in registry — building and pushing before combined image",
                per_arch_ref,
            )
            _publish_single_arch(
                layer_tars=arch_layer_tars[arch],
                ref=per_arch_ref,
                tag=per_arch_tag,
                sha=sha,
                repository=repository,
                artifact_name=artifact_name,
                created=created,
            )

    # Build the combined image using per-arch registry images as bases.
    # Inherited layers keep their original blob digests.
    image_ids: list[str] = []
    for arch in _ARCHES:
        other = next(a for a in _ARCHES if a != arch)
        per_arch_ref = _image_ref(registry, repository, artifact_name, f"{tag}-{arch}")
        image_id = _build_platform_image(
            arch_layer_tars[other],
            f"linux/{arch}",
            sha,
            repository,
            created=created,
            tag=tag,
            artifact_name=artifact_name,
            base=f"docker://{per_arch_ref}",
        )
        image_ids.append(image_id)

    _create_push_cleanup(image_ids, combined_ref)
    return True


def publish(
    cfg: Config,
    *,
    target: str,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    sha: str,
    force: bool = False,
) -> None:
    """Collect artifacts and publish a multi-arch OCI index.

    Each artifact file becomes its own layer.  Deterministic tar
    generation ensures byte-identical layers across publish runs,
    so OCI registries deduplicate blobs automatically.

    *target* selects which artifacts to include: ``"amd64"``,
    ``"arm64"``, or ``"combined"``.

    Images are skipped if they already exist in the registry
    (unless *force* is ``True``).  For per-arch targets this prevents
    overwriting images that the combined image depends on.
    """
    arches = list(_ARCHES) if target == "combined" else [target]
    tag_suffix = "" if target == "combined" else f"-{target}"
    full_tag = f"{tag}{tag_suffix}"
    final_ref = _image_ref(registry, repository, artifact_name, full_tag)

    # For per-arch targets, skip if the image already exists.
    if target != "combined" and not force and skopeo.image_exists(final_ref):
        log.info("%s already exists — skipping (use --force to overwrite)", final_ref)
        return

    out = ensure_dir(cfg.output_dir)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Collect artifacts for every requested architecture.
    arch_files: dict[str, list[Path]] = {}
    for arch in arches:
        arch_files[arch] = _collect_arch_artifacts(
            cfg.project_dir,
            out,
            arch,
            cfg.kernel_version,
        )

    # Create deterministic layer tars (shared across manifest pushes).
    arch_layer_tars: dict[str, list[Path]] = {}
    for arch, files in arch_files.items():
        arch_layer_tars[arch] = [_deterministic_tar(f, out) for f in files]

    pushed = True
    try:
        if target == "combined":
            pushed = _publish_combined(
                arch_layer_tars=arch_layer_tars,
                registry=registry,
                repository=repository,
                artifact_name=artifact_name,
                tag=tag,
                sha=sha,
                created=created,
                force=force,
            )
        else:
            _publish_single_arch(
                layer_tars=arch_layer_tars[target],
                ref=final_ref,
                tag=full_tag,
                sha=sha,
                repository=repository,
                artifact_name=artifact_name,
                created=created,
            )
    finally:
        for tars in arch_layer_tars.values():
            for t in tars:
                t.unlink(missing_ok=True)

    if not pushed:
        return

    # Recap
    artifact_names: list[str] = []
    for arch in arches:
        artifact_names.extend(f.name for f in arch_files.get(arch, []))
    platforms = [f"linux/{a}" for a in _ARCHES]
    log.info("")
    log.info("Publish complete")
    log.info("  Image:     %s", final_ref)
    log.info("  Target:    %s", target)
    log.info("  Platforms: %s", ", ".join(platforms))
    log.info("  Layers:    %d", len(artifact_names))
    log.info("  Artifacts:")
    for name in artifact_names:
        log.info("    - %s", name)
