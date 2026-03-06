"""High-level OCI artifact operations for publishing and retrieving releases.

Each artifact file is pushed as its own layer so that OCI registries
can deduplicate blobs between per-arch and combined images.

Combined image (``target="both"``, no tag suffix):
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

* **containerd** can pull it (valid ``rootfs.diff_ids`` in the config) —
  Kubernetes image-volume mounts work.
* **crane export** extracts individual files for release workflows.
"""

from __future__ import annotations

import subprocess
import tarfile
from datetime import datetime, timezone
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
    logger: StageLogger,
) -> list[Path]:
    """Collect and return the artifact files for a single architecture.

    Returns [vmlinuz, initramfs, iso, checksums] paths in *out*.
    """
    import shutil

    # Collect kernel
    vmlinuz_dir = project_dir / "mkosi.output" / "vmlinuz" / arch
    vmlinuz_files = sorted(vmlinuz_dir.glob("vmlinuz-*")) if vmlinuz_dir.is_dir() else []
    vmlinuz_dst = out / f"vmlinuz-{arch}"
    if vmlinuz_files:
        shutil.copy2(vmlinuz_files[0], vmlinuz_dst)
        logger.log(f"kernel: {vmlinuz_dst}")
    else:
        logger.warn(f"No kernel image found for {arch}")

    arch_files = [
        out / f"vmlinuz-{arch}",
        out / f"initramfs-{arch}.cpio.zst",
        out / f"captainos-{arch}.iso",
    ]
    checksums_path = out / f"sha256sums-{arch}.txt"
    artifacts.collect_checksums(arch_files, checksums_path, logger=logger)

    push_files = [*arch_files, checksums_path]
    for f in push_files:
        if not f.is_file():
            logger.err(f"Missing artifact: {f}")
            raise SystemExit(1)
    return push_files


def _push_platform_manifest(
    layer_tars: list[Path],
    temp_ref: str,
    platform: str,
    sha: str,
    repository: str,
    logger: StageLogger,
    *,
    created: str,
    tag: str,
    artifact_name: str,
) -> None:
    """Push artifact layers and set platform metadata on a temp manifest."""
    for i, tar_path in enumerate(layer_tars):
        crane.append(tar_path, temp_ref, base=temp_ref if i > 0 else None, logger=logger)
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
    crane.mutate(
        temp_ref,
        platform=platform,
        annotations=oci_metadata,
        labels=oci_metadata,
        logger=logger,
    )
    crane.set_created(temp_ref, created, logger=logger)


def publish(
    cfg: Config,
    *,
    target: str,
    registry: str,
    repository: str,
    artifact_name: str,
    tag: str,
    sha: str,
    logger: StageLogger | None = None,
) -> None:
    """Collect artifacts and publish a multi-arch OCI index.

    Each artifact file becomes its own layer.  Deterministic tar
    generation ensures byte-identical layers across publish runs,
    so OCI registries deduplicate blobs automatically.

    *target* selects which artifacts to include: ``"amd64"``,
    ``"arm64"``, or ``"both"``.
    """
    _log = logger or _default_log
    arches = list(_ARCHES) if target == "both" else [target]
    tag_suffix = "" if target == "both" else f"-{target}"
    full_tag = f"{tag}{tag_suffix}"
    final_ref = _image_ref(registry, repository, artifact_name, full_tag)
    out = ensure_dir(cfg.output_dir)
    image_base = f"{registry}/{repository}/{artifact_name}"
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Collect artifacts for every requested architecture.
    arch_files: dict[str, list[Path]] = {}
    for arch in arches:
        arch_files[arch] = _collect_arch_artifacts(
            cfg.project_dir,
            out,
            arch,
            _log,
        )

    # Create deterministic layer tars (shared across manifest pushes).
    arch_layer_tars: dict[str, list[Path]] = {}
    for arch, files in arch_files.items():
        arch_layer_tars[arch] = [_deterministic_tar(f, out) for f in files]

    try:
        digest_refs: list[str] = []

        if target == "both":
            # Combined image: native-first ordering per platform.
            for arch in _ARCHES:
                other = next(a for a in _ARCHES if a != arch)
                ordered = list(arch_layer_tars[arch]) + list(arch_layer_tars[other])
                _push_platform_manifest(
                    ordered,
                    final_ref,
                    f"linux/{arch}",
                    sha,
                    repository,
                    _log,
                    created=created,
                    tag=full_tag,
                    artifact_name=artifact_name,
                )
                d = crane.digest(final_ref, logger=_log)
                digest_refs.append(f"{image_base}@{d}")
        else:
            # Per-arch: same layers under both platforms.
            for arch in _ARCHES:
                _push_platform_manifest(
                    arch_layer_tars[target],
                    final_ref,
                    f"linux/{arch}",
                    sha,
                    repository,
                    _log,
                    created=created,
                    tag=full_tag,
                    artifact_name=artifact_name,
                )
                d = crane.digest(final_ref, logger=_log)
                digest_refs.append(f"{image_base}@{d}")

        # Create multi-arch index (overwrites the tag with the index)
        crane.index_append(final_ref, digest_refs, logger=_log)
    finally:
        for tars in arch_layer_tars.values():
            for t in tars:
                t.unlink(missing_ok=True)

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

    *target* may be ``"amd64"``, ``"arm64"``, or ``"both"``.  The tag
    suffix is ``-{target}`` for single architectures, or bare ``{tag}``
    for ``"both"``.
    """
    _log = logger or _default_log
    tag_suffix = "" if target == "both" else f"-{target}"
    ref = _image_ref(registry, repository, artifact_name, f"{tag}{tag_suffix}")
    crane.export_image(ref, output_dir, logger=_log)

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
    ref = _image_ref(registry, repository, artifact_name, src_tag)
    crane.tag(ref, new_tag, logger=_log)
    _log.log(f"Tagged {ref} → {new_tag}")


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
