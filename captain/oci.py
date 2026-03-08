"""High-level OCI artifact operations for publishing and retrieving releases.

Each artifact file is pushed as its own layer so that OCI registries
can deduplicate blobs between per-arch and combined images.

Combined image (``target="combined"``, no tag suffix):
  A multi-arch index where each platform manifest has the native
  arch's layers first, then the other arch's layers (8 layers total).
  ``linux/amd64`` → ``[A1‥A4, B1‥B4]``,
  ``linux/arm64`` → ``[B1‥B4, A1‥A4]``.

Per-arch image (``target="amd64"`` or ``"arm64"``, tag suffix ``-{arch}``):
  A multi-arch index where both platform entries contain the same
  4 layers (only that arch's artifacts).

Docker layer caching: pulling the combined image first and then the
native per-arch image gives cache hits, because the per-arch layers
form a prefix of the combined chain.  Cross-arch caching is not
possible due to Docker's chain-ID Merkle structure.

Images are built locally via ``buildah`` and pushed as finished
manifests — no intermediate untagged manifests are created on the
registry.  Read operations (digest, tag, export) use ``skopeo``.

* **containerd** can pull it (valid ``rootfs.diff_ids`` in the config) —
  Kubernetes image-volume mounts work.
* ``skopeo`` extracts individual files for release workflows.
"""

from __future__ import annotations

import contextlib
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from captain import artifacts, buildah, skopeo
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
    logger: StageLogger,
) -> list[Path]:
    """Collect and return the artifact files for a single architecture.

    Returns [vmlinuz, initramfs, iso, checksums] paths in *out*.
    """
    import shutil

    # Collect kernel
    vmlinuz_dir = project_dir / "mkosi.output" / "kernel" / kernel_version / arch
    vmlinuz_files = sorted(vmlinuz_dir.glob("vmlinuz-*")) if vmlinuz_dir.is_dir() else []
    vmlinuz_dst = out / f"vmlinuz-{kernel_version}-{arch}"
    if vmlinuz_files:
        shutil.copy2(vmlinuz_files[0], vmlinuz_dst)
        logger.log(f"kernel: {vmlinuz_dst}")
    else:
        logger.warn(f"No kernel image found for {arch}")

    arch_files = [
        out / f"vmlinuz-{kernel_version}-{arch}",
        out / f"initramfs-{kernel_version}-{arch}.cpio.zst",
        out / f"captainos-{kernel_version}-{arch}.iso",
    ]
    checksums_path = out / f"sha256sums-{kernel_version}-{arch}.txt"
    artifacts.collect_checksums(arch_files, checksums_path, logger=logger)

    push_files = [*arch_files, checksums_path]
    for f in push_files:
        if not f.is_file():
            logger.err(f"Missing artifact: {f}")
            raise SystemExit(1)
    return push_files


def _build_platform_image(
    layer_tars: list[Path],
    platform: str,
    sha: str,
    repository: str,
    logger: StageLogger,
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
        ctr = buildah.from_image(current, platform=platform, logger=logger)
        buildah.add(ctr, [tar_path], logger=logger)
        if is_last:
            buildah.config(
                ctr,
                os=os_name,
                arch=arch,
                annotations=oci_metadata,
                labels=oci_metadata,
                logger=logger,
            )
        prev = current
        current = buildah.commit(ctr, timestamp=epoch, logger=logger)
        if prev != base:
            intermediates.append(prev)

    for img in intermediates:
        with contextlib.suppress(Exception):
            buildah.rmi(img, logger=logger)

    return current


def _create_push_cleanup(
    image_ids: list[str],
    dest_ref: str,
    logger: StageLogger,
) -> None:
    """Create a manifest list from *image_ids*, push it to *dest_ref*, and clean up.

    Uses a temporary local manifest name to avoid collisions on repeated
    publishes.  After a successful (or failed) push, the local manifest
    and all *image_ids* are removed on a best-effort basis.
    """
    temp_name = f"captain-local-{uuid4().hex[:12]}"
    manifest_id: str | None = None
    try:
        manifest_id = buildah.manifest_create(temp_name, logger=logger)
        for image_id in image_ids:
            buildah.manifest_add(manifest_id, image_id, logger=logger)
        buildah.manifest_push(manifest_id, dest_ref, logger=logger)
    finally:
        if manifest_id is not None:
            with contextlib.suppress(Exception):
                buildah.rmi(manifest_id, logger=logger)
        for image_id in image_ids:
            with contextlib.suppress(Exception):
                buildah.rmi(image_id, logger=logger)


def _publish_single_arch(
    *,
    layer_tars: list[Path],
    ref: str,
    tag: str,
    sha: str,
    repository: str,
    artifact_name: str,
    created: str,
    logger: StageLogger,
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
            logger,
            created=created,
            tag=tag,
            artifact_name=artifact_name,
        )
        image_ids.append(image_id)

    _create_push_cleanup(image_ids, ref, logger)


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
    logger: StageLogger,
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
    if not force and skopeo.image_exists(combined_ref, logger=logger):
        logger.log(f"{combined_ref} already exists — skipping (use --force to overwrite)")
        return False

    # Ensure per-arch images exist in the registry.
    for arch in _ARCHES:
        per_arch_tag = f"{tag}-{arch}"
        per_arch_ref = _image_ref(registry, repository, artifact_name, per_arch_tag)
        if skopeo.image_exists(per_arch_ref, logger=logger):
            logger.log(f"Found {per_arch_ref} in registry — will reuse layers for combined image")
        else:
            logger.log(
                f"{per_arch_ref} not found in registry — building and pushing before combined image"
            )
            _publish_single_arch(
                layer_tars=arch_layer_tars[arch],
                ref=per_arch_ref,
                tag=per_arch_tag,
                sha=sha,
                repository=repository,
                artifact_name=artifact_name,
                created=created,
                logger=logger,
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
            logger,
            created=created,
            tag=tag,
            artifact_name=artifact_name,
            base=f"docker://{per_arch_ref}",
        )
        image_ids.append(image_id)

    _create_push_cleanup(image_ids, combined_ref, logger)
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
    logger: StageLogger | None = None,
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
    _log = logger or _default_log
    arches = list(_ARCHES) if target == "combined" else [target]
    tag_suffix = "" if target == "combined" else f"-{target}"
    full_tag = f"{tag}{tag_suffix}"
    final_ref = _image_ref(registry, repository, artifact_name, full_tag)

    # For per-arch targets, skip if the image already exists.
    if target != "combined" and not force and skopeo.image_exists(final_ref, logger=_log):
        _log.log(f"{final_ref} already exists — skipping (use --force to overwrite)")
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
            _log,
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
                logger=_log,
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
                logger=_log,
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
    _log.log("")
    _log.log("Publish complete")
    _log.log(f"  Image:     {final_ref}")
    _log.log(f"  Target:    {target}")
    _log.log(f"  Platforms: {', '.join(platforms)}")
    _log.log(f"  Layers:    {len(artifact_names)}")
    _log.log("  Artifacts:")
    for name in artifact_names:
        _log.log(f"    - {name}")


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
