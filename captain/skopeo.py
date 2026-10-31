"""Thin wrapper around the ``skopeo`` CLI for OCI image read operations.

Provides digest inspection and downloading
images to a local directory for artifact extraction.  All operations
are rootless and require no container runtime.
"""

from __future__ import annotations

import json
import logging
import os
import tarfile
from pathlib import Path

from captain.util import run, safe_extractall

log = logging.getLogger(__name__)


def image_exists(image_ref: str) -> bool:
    """Return ``True`` if *image_ref* exists in the remote registry."""
    log.info("Checking registry for %s", image_ref)
    result = run(
        ["skopeo", "inspect", f"docker://{image_ref}"],
        capture=True,
        check=False,
    )
    return result.returncode == 0


def inspect_digest(image_ref: str) -> str:
    """Return the manifest digest (``sha256:…``) of *image_ref*."""
    log.info("skopeo inspect digest %s", image_ref)
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


def copy_to_dir(
    image_ref: str,
    output_dir: Path,
    *,
    platform: str | None = None,
) -> Path:
    """Download *image_ref* to a local directory.

    Uses ``skopeo copy docker://<ref> dir:<output_dir>``.  The directory
    will contain ``manifest.json`` and layer blob files.

    Returns *output_dir*.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        "skopeo",
        "copy",
        *(["--src-tls-verify=false"] if os.environ.get("REGISTRY_INSECURE") == "1" else []),
    ]
    if platform:
        parts = platform.split("/")
        if len(parts) == 2:
            cmd += ["--override-os", parts[0], "--override-arch", parts[1]]
    cmd += [f"docker://{image_ref}", f"dir:{output_dir}"]
    log.info("skopeo copy %s → dir:%s", image_ref, output_dir)
    run(cmd)
    return output_dir


def export_image(
    image_ref: str,
    output_dir: Path,
    *,
    platform: str | None = None,
) -> list[str]:
    """Download and extract all layers from *image_ref* into *output_dir*.

    Uses ``skopeo copy`` to download the image to a temporary directory,
    parses the manifest to find layer blobs, and extracts each layer tar
    with path-traversal protection.

    Returns the sorted list of top-level entries (files and directories)
    that the pulled image's layers contained.  This reflects the image
    contents, not whatever happens to be on disk in *output_dir*.
    """
    import tempfile

    output_dir.mkdir(parents=True, exist_ok=True)

    extracted: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="skopeo-export-") as tmp:
        tmp_dir = Path(tmp)
        copy_to_dir(image_ref, tmp_dir, platform=platform)

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

            log.info("Extracting layer %s…", digest_str[:20])
            with tarfile.open(blob_file, "r:*") as tf:
                members = tf.getmembers()
                for m in members:
                    parts = Path(m.name).parts
                    if parts and parts[0] not in (".", ""):
                        extracted.add(parts[0])
                safe_extractall(tf, output_dir, members=members)

    return sorted(extracted)
