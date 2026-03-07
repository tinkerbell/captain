"""Thin wrapper around the ``skopeo`` CLI for OCI image read operations.

Provides digest inspection, image copying (retagging), and downloading
images to a local directory for artifact extraction.  All operations
are rootless and require no container runtime.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from captain.log import StageLogger, for_stage
from captain.util import run, safe_extractall

_default_log = for_stage("skopeo")


def image_exists(
    image_ref: str,
    *,
    logger: StageLogger | None = None,
) -> bool:
    """Return ``True`` if *image_ref* exists in the remote registry."""
    _log = logger or _default_log
    _log.log(f"Checking registry for {image_ref}")
    result = run(
        ["skopeo", "inspect", f"docker://{image_ref}"],
        capture=True,
        check=False,
    )
    return result.returncode == 0


def inspect_digest(
    image_ref: str,
    *,
    logger: StageLogger | None = None,
) -> str:
    """Return the manifest digest (``sha256:…``) of *image_ref*."""
    _log = logger or _default_log
    _log.log(f"skopeo inspect digest {image_ref}")
    result = run(
        [
            "skopeo",
            "inspect",
            "--format",
            "{{.Digest}}",
            f"docker://{image_ref}",
        ],
        capture=True,
    )
    return result.stdout.strip()


def copy(
    src: str,
    dest: str,
    *,
    logger: StageLogger | None = None,
) -> None:
    """Copy an image from *src* to *dest*.

    *src* and *dest* are plain image references (e.g.
    ``ghcr.io/org/repo:tag``); the ``docker://`` transport prefix is
    added automatically.  Typically used for retagging: the source and
    destination differ only in the tag component.
    """
    _log = logger or _default_log
    _log.log(f"skopeo copy {src} → {dest}")
    run(["skopeo", "copy", "--all", f"docker://{src}", f"docker://{dest}"])


def copy_to_dir(
    image_ref: str,
    output_dir: Path,
    *,
    platform: str | None = None,
    logger: StageLogger | None = None,
) -> Path:
    """Download *image_ref* to a local directory.

    Uses ``skopeo copy docker://<ref> dir:<output_dir>``.  The directory
    will contain ``manifest.json`` and layer blob files.

    Returns *output_dir*.
    """
    _log = logger or _default_log
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = ["skopeo", "copy"]
    if platform:
        parts = platform.split("/")
        if len(parts) == 2:
            cmd += ["--override-os", parts[0], "--override-arch", parts[1]]
    cmd += [f"docker://{image_ref}", f"dir:{output_dir}"]
    _log.log(f"skopeo copy {image_ref} → dir:{output_dir}")
    run(cmd)
    return output_dir


def export_image(
    image_ref: str,
    output_dir: Path,
    *,
    platform: str | None = None,
    logger: StageLogger | None = None,
) -> None:
    """Download and extract all layers from *image_ref* into *output_dir*.

    Uses ``skopeo copy`` to download the image to a temporary directory,
    parses the manifest to find layer blobs, and extracts each layer tar
    with path-traversal protection.
    """
    import tempfile

    _log = logger or _default_log
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="skopeo-export-") as tmp:
        tmp_dir = Path(tmp)
        copy_to_dir(image_ref, tmp_dir, platform=platform, logger=_log)

        # Parse manifest to find layer blob digests.
        manifest_path = tmp_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        layers = manifest.get("layers", [])

        for layer in layers:
            digest_str = layer["digest"]  # e.g. "sha256:abc123..."
            # skopeo stores blobs under several possible filenames.
            blob_file = tmp_dir / digest_str
            if not blob_file.exists():
                blob_file = tmp_dir / digest_str.replace(":", "-")
            if not blob_file.exists():
                blob_file = tmp_dir / digest_str.split(":")[-1]
            if not blob_file.exists():
                raise FileNotFoundError(f"Layer blob not found: {digest_str}")

            _log.log(f"Extracting layer {digest_str[:20]}…")
            with tarfile.open(blob_file, "r:*") as tf:
                safe_extractall(tf, output_dir)
