"""High-level OCI artifact operations for publishing and retrieving releases.

Each artifact file is pushed as its own layer so that OCI registries
can deduplicate blobs between per-arch and combined images.  Every
image is a multi-arch OCI index (linux/amd64 + linux/arm64 entries
pointing to the same content) so that any platform can pull it.

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
) -> None:
    """Push artifact layers and set platform metadata on a temp manifest."""
    for i, tar_path in enumerate(layer_tars):
        crane.append(tar_path, temp_ref, base=temp_ref if i > 0 else None, logger=logger)
    crane.mutate(
        temp_ref,
        platform=platform,
        annotations={
            "org.opencontainers.image.source": f"https://github.com/{repository}",
            "org.opencontainers.image.revision": sha,
        },
        logger=logger,
    )


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

    Each artifact file becomes its own layer so that OCI registries
    deduplicate shared blobs between per-arch and combined images.

    *target* selects which artifacts to include: ``"amd64"``,
    ``"arm64"``, or ``"both"`` (all artifacts from both arches).
    """
    _log = logger or _default_log
    arches = list(_ARCHES) if target == "both" else [target]
    tag_suffix = "" if target == "both" else f"-{target}"
    final_ref = _image_ref(registry, repository, artifact_name, f"{tag}{tag_suffix}")
    out = ensure_dir(cfg.output_dir)

    all_files: list[Path] = []
    for a in arches:
        files = _collect_arch_artifacts(cfg.project_dir, out, a, _log)
        all_files.extend(files)

    # Create deterministic per-file tars for layer dedup
    layer_tars: list[Path] = []
    try:
        for f in all_files:
            layer_tars.append(_deterministic_tar(f, out))

        # Push platform manifests and capture their digests.
        # Each push overwrites the same tag; the digest is captured before
        # the next overwrite.  This avoids leftover intermediate tags.
        image_base = f"{registry}/{repository}/{artifact_name}"
        platforms = ["linux/amd64", "linux/arm64"]
        digest_refs: list[str] = []
        for platform in platforms:
            _push_platform_manifest(layer_tars, final_ref, platform, sha, repository, _log)
            d = crane.digest(final_ref, logger=_log)
            digest_refs.append(f"{image_base}@{d}")

        # Create multi-arch index (overwrites the tag with the index)
        crane.index_append(final_ref, digest_refs, logger=_log)
    finally:
        for t in layer_tars:
            t.unlink(missing_ok=True)

    # Recap
    artifact_names = [f.name for f in all_files]
    _log.log("")
    _log.log("Publish complete")
    _log.log(f"  Image:     {final_ref}")
    _log.log(f"  Target:    {target}")
    _log.log(f"  Platforms: {', '.join(platforms)}")
    _log.log(f"  Layers:    {len(layer_tars)}")
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
