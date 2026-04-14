"""Shared constants and helpers for the OCI package."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
