"""Publishing OCI artifacts to a registry."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from captain import buildah, skopeo
from captain.artifacts import OutputArchArtifactType
from captain.config import Config
from captain.flavor import Flavor
from captain.util import ensure_dir, get_arch_info

from ._build import (
    _build_platform_image,
    _checksum_files,
    _deterministic_tar,
    _deterministic_tar_multiple,
)
from ._common import _ARCHES, _image_ref

log = logging.getLogger(__name__)


def _create_push_cleanup(
    image_ids: list[str],
    dest_ref: str,
    oci_metadata: dict[str, str],
) -> None:
    """Create a manifest list from *image_ids*, push it to *dest_ref*, and clean up.

    Uses a temporary local manifest name to avoid collisions on repeated
    publishes.  After a successful (or failed) push, the local manifest
    and all *image_ids* are removed on a best-effort basis.
    """
    temp_name = f"captain-local-{uuid4().hex[:12]}"
    manifest_id: str | None = None
    try:
        manifest_id = buildah.manifest_create(temp_name, oci_metadata)
        for image_id in image_ids:
            buildah.manifest_add(manifest_id, image_id)
        buildah.manifest_push(manifest_id, dest_ref)
    finally:
        if manifest_id is not None:
            log.debug("Cleaning up manifest: %s", manifest_id)
            with contextlib.suppress(Exception):
                buildah.rmi(manifest_id)
        log.debug("Cleaning up intermediate images: %s", image_ids)
        for image_id in image_ids:
            with contextlib.suppress(Exception):
                buildah.rmi(image_id)


def _publish_single_arch(
    *,
    flavor: Flavor,
    layer_tars: list[Path],
    ref: str,
    repository: str,
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
            created=created,
            oci_metadata=create_oci_metadata(
                flavor=flavor,
                image_type="inner-arch",
                arch=platform_arch,
                repository=repository,
            ),
        )
        image_ids.append(image_id)

    _create_push_cleanup(
        image_ids=image_ids,
        dest_ref=ref,
        oci_metadata=create_oci_metadata(
            flavor=flavor,
            image_type="outer-arch",
            arch=None,
            repository=repository,
        ),
    )


def _publish_combined(
    *,
    flavor: Flavor,
    arch_layer_tars: dict[str, list[Path]],
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    arch_tags: dict[str, str],
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
        per_arch_ref = _image_ref(registry, repository, artifact_name, arch_tags[arch])
        if skopeo.image_exists(per_arch_ref):
            log.info("Found %s in registry — will reuse layers for combined image", per_arch_ref)
        else:
            log.info(
                "%s not found in registry — building and pushing before combined image",
                per_arch_ref,
            )
            _publish_single_arch(
                flavor=flavor,
                layer_tars=arch_layer_tars[arch],
                ref=per_arch_ref,
                repository=repository,
                created=created,
            )

    # Build the combined image using per-arch registry images as bases.
    # Inherited layers keep their original blob digests.
    image_ids: list[str] = []
    for arch in _ARCHES:
        other = next(a for a in _ARCHES if a != arch)
        per_arch_ref = _image_ref(registry, repository, artifact_name, arch_tags[arch])
        image_id = _build_platform_image(
            arch_layer_tars[other],
            f"linux/{arch}",
            created=created,
            oci_metadata=create_oci_metadata(
                flavor=flavor,
                image_type="inner-combined",
                arch=arch,
                repository=repository,
            ),
            base=f"docker://{per_arch_ref}",
        )
        image_ids.append(image_id)

    _create_push_cleanup(
        image_ids=image_ids,
        dest_ref=combined_ref,
        oci_metadata=create_oci_metadata(
            flavor=flavor,
            image_type="outer-combined",
            arch=None,
            repository=repository,
        ),
    )

    return True


def publish(
    cfg: Config,
    flavor: Flavor,
    *,
    target: str,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    arch_tags: dict[str, str] | None = None,
    force: bool = False,
) -> None:
    """Collect artifacts and publish a multi-arch OCI index.

    Each artifact file becomes its own layer.  Deterministic tar
    generation ensures byte-identical layers across publish runs,
    so OCI registries deduplicate blobs automatically.

    *target* selects which artifacts to include: ``"amd64"``,
    ``"arm64"``, or ``"combined"``. *tag* is the image's full tag; for
    ``"combined"``, *arch_tags* are the per-arch images' tags (its bases).

    Images are skipped if they already exist in the registry
    (unless *force* is ``True``).  For per-arch targets this prevents
    overwriting images that the combined image depends on.
    """
    arches = list(_ARCHES) if target == "combined" else [target]
    final_ref = _image_ref(registry, repository, artifact_name, tag)
    if target == "combined" and arch_tags is None:
        raise ValueError("combined publish needs the per-arch images' tags")

    # For per-arch targets, skip if the image already exists.
    if target != "combined" and not force and skopeo.image_exists(final_ref):
        log.info("%s already exists — skipping (use --force to overwrite)", final_ref)
        return

    out = ensure_dir(cfg.output_dir)

    # Set created to the epoch so it is always the same
    created = datetime(1970, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Collect artifacts for every requested architecture.
    arch_files: dict[str, list[Path]] = {}
    arch_directories: dict[str, list[Path]] = {}
    for arch in arches:
        output_arch = get_arch_info(arch).output_arch
        arch_artifacts = flavor.list_arch_artifacts(output_arch)

        # First, pick all the type=file artifacts for this arch.
        file_arch_artifacts = [a for a in arch_artifacts if a.type == OutputArchArtifactType.FILE]
        if not file_arch_artifacts:
            log.warning("No file artifacts found for %s/%s", flavor.id, output_arch)
            continue

        file_arch_artifacts_in_out = []
        for a in file_arch_artifacts:
            path_in_out = out / a.name
            if not path_in_out.is_file():
                log.error("Expected artifact file not found in output: %s", path_in_out)
                raise SystemExit(1)
            file_arch_artifacts_in_out.append(path_in_out)

        # Calculate the checksums & add to arch_files
        arch_files[arch] = _checksum_files(
            file_arch_artifacts_in_out, cfg.flavor_id, output_arch, out
        )

        # Also add directories; those are not checksummed
        dir_arch_artifacts_in_out = []
        dir_arch_artifacts = [
            a for a in arch_artifacts if a.type == OutputArchArtifactType.DIRECTORY
        ]
        if dir_arch_artifacts:
            for a in dir_arch_artifacts:
                dir_in_out = out / a.name
                if not dir_in_out.is_dir():
                    log.error("Expected artifact directory not found in output: %s", dir_in_out)
                    raise SystemExit(1)
                dir_arch_artifacts_in_out.append(dir_in_out)
        arch_directories[arch] = dir_arch_artifacts_in_out

    # Create deterministic layer tars (shared across manifest pushes).
    arch_layer_tars: dict[str, list[Path]] = {}

    # Add the files first, each in a deterministic tar
    for arch, files in arch_files.items():
        log.info("Creating layer tars (single-file) for %s... files: %s", arch, files)
        arch_layer_tars[arch] = []
        for f in files:
            log.info("Creating layer tar (single-file) for %s... file: %s", arch, f)
            arch_layer_tars[arch].append(_deterministic_tar(f, out))

    # Add all directories (with every file within); single tar for better compression
    for arch, dirs in arch_directories.items():
        for directory in dirs:
            log.info("Creating layer tar (directory) for %s... directory: %s", arch, directory)
            all_dir_files: list[Path] = sorted(directory.glob("**/*"))
            all_dir_files = [f for f in all_dir_files if f.is_file()]  # only files, not dirs
            dir_tar_path = out / f".layer-dir-{cfg.flavor_id}-{arch}-{directory.name}.tar"
            log.debug("Adding %d files to layer tar for arch %s", len(all_dir_files), arch)
            _deterministic_tar_multiple(all_dir_files, dir_tar_path, out)
            arch_layer_tars[arch].append(dir_tar_path)

    pushed = True
    try:
        if target == "combined":
            pushed = _publish_combined(
                flavor=flavor,
                arch_layer_tars=arch_layer_tars,
                registry=registry,
                repository=repository,
                artifact_name=artifact_name,
                tag=tag,
                arch_tags=arch_tags or {},
                created=created,
                force=force,
            )
        else:
            _publish_single_arch(
                flavor=flavor,
                layer_tars=arch_layer_tars[target],
                ref=final_ref,
                repository=repository,
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
        artifact_names.extend(d.name for d in arch_directories.get(arch, []))

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


def create_oci_metadata(
    *,
    flavor: Flavor,
    image_type: str,
    arch: str | None = None,
    repository: str,
) -> dict[str, str]:
    title_parts = ["CaptainOS"]
    title_parts += [f"{flavor.id}"]
    title_parts += [f"{image_type}"]
    if arch is not None:
        title_parts += [f"{arch}"]

    desc_parts = ["Tinkerbell CaptainOS"]

    # Add flavor info
    desc_parts += [f"flavor {flavor.id}"]
    desc_parts += [f"({flavor.description})"]

    if arch is not None:
        desc_parts += [f"{arch}"]

    match image_type:
        case "inner-arch":
            desc_parts += ["single-platform inner image"]
        case "inner-combined":
            desc_parts += ["combined inner image"]
        case "outer-combined":
            desc_parts += ["combined multi-platform image"]
        case "outer-arch":
            desc_parts += ["single-platform image"]
        case _:
            log.warning("Unknown OCI metadata type: %s", image_type)

    description = " ".join(desc_parts)
    title = " ".join(title_parts)

    oci_metadata = {
        "org.opencontainers.image.vendor": "Tinkerbell",
        "org.opencontainers.image.licenses": "Apache-2.0",
        "org.opencontainers.image.source": f"https://github.com/{repository}",
        "org.opencontainers.image.title": title,
        "org.opencontainers.image.description": description,
    }
    return oci_metadata
