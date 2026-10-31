"""Download pinned binary tools (containerd, runc, nerdctl, CNI plugins).

Downloads use urllib; extraction uses the tarfile module — no curl or tar
dependency required.  Called directly by ``stages._build_tools_stage`` in
both native and Docker modes.
"""

from __future__ import annotations

import io
import logging
import stat
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from captain import progress
from captain.config import Config
from captain.util import ensure_dir, safe_extractall

log = logging.getLogger(__name__)

_TIMEOUT = 60  # seconds


@dataclass(slots=True)
class ToolSpec:
    """Specification for a single downloadable tool."""

    name: str
    version: str
    # URL template — {version} and {arch} will be substituted
    url_template: str
    # Destination directory relative to mkosi.output/tools/{arch}/
    dest: str
    # Members to extract from tarball (None = single binary download)
    members: list[str] | None = None
    # Filename for single binary downloads (used when members is None)
    binary_name: str | None = None
    # Files to remove after extraction (cleanup from previous builds)
    cleanup: list[str] = field(default_factory=list)


# Tool manifest — pinned versions matching the original download-tools.sh
TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="containerd",
        version="2.2.1",
        url_template="https://github.com/containerd/containerd/releases/download/v{version}/containerd-{version}-linux-{arch}.tar.gz",
        dest="usr/local",
        members=["bin/containerd", "bin/containerd-shim-runc-v2"],
        cleanup=["bin/ctr", "bin/containerd-stress"],
    ),
    ToolSpec(
        name="runc",
        version="1.4.0",
        url_template="https://github.com/opencontainers/runc/releases/download/v{version}/runc.{arch}",
        dest="usr/local/bin",
        binary_name="runc",
    ),
    ToolSpec(
        name="nerdctl",
        version="2.2.1",
        url_template="https://github.com/containerd/nerdctl/releases/download/v{version}/nerdctl-{version}-linux-{arch}.tar.gz",
        dest="usr/local/bin",
        members=["nerdctl"],
        cleanup=["dockerd", "docker", "docker-init", "docker-proxy"],
    ),
    ToolSpec(
        name="cni-plugins",
        version="1.6.0",
        url_template="https://github.com/containernetworking/plugins/releases/download/v{version}/cni-plugins-linux-{arch}-v{version}.tgz",
        dest="opt/cni/bin",
        members=["bridge", "host-local", "loopback", "portmap", "firewall", "tuning"],
    ),
]


def _fetch(url: str, out: BinaryIO) -> None:
    """Stream *url* into *out*, with a progress bar on a terminal."""
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length", 0)) or None
        with progress.transfer(f"    {url.rsplit('/', 1)[-1]}", total) as advance:
            while chunk := resp.read(1 << 16):
                out.write(chunk)
                advance(len(chunk))


def _download_tarball(url: str, dest_dir: Path, members: list[str]) -> None:
    """Download a gzipped tarball and extract specific members."""
    log.info("    Downloading %s", url)
    data = io.BytesIO()
    _fetch(url, data)
    data.seek(0)

    with tarfile.open(fileobj=data, mode="r:gz") as tf:
        # Build a set of acceptable member names (with and without ./ prefix)
        wanted = set()
        for m in members:
            wanted.add(m)
            wanted.add(f"./{m}")

        to_extract = [mi for mi in tf.getmembers() if mi.name in wanted]
        safe_extractall(tf, path=dest_dir, members=to_extract)

    # Make extracted files executable
    for m in members:
        p = dest_dir / m
        if p.exists():
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _download_binary(url: str, dest: Path) -> None:
    """Download a single binary file."""
    log.info("    Downloading %s", url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        _fetch(url, f)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def download_tool(tool: ToolSpec, arch: str, output_base: Path) -> None:
    """Download and install a single tool.

    Up-to-dateness is the build cache's job (see ``stages._build_tools_stage``).
    """
    dest_dir = ensure_dir(output_base / tool.dest)

    url = tool.url_template.format(version=tool.version, arch=arch)
    log.info("Installing %s %s (%s)...", tool.name, tool.version, arch)

    if tool.members is not None:
        # Tarball with selective extraction
        _download_tarball(url, dest_dir, tool.members)
    elif tool.binary_name is not None:
        # Single binary download
        _download_binary(url, dest_dir / tool.binary_name)

    # Cleanup leftover files from previous builds
    for name in tool.cleanup:
        p = dest_dir / name
        if p.exists():
            p.unlink()
            log.info("    Removed leftover: %s", p.name)

    # Report installed files
    if tool.members:
        for m in tool.members:
            p = dest_dir / m
            if p.exists():
                log.info("    %s: %s", tool.name, p)
    elif tool.binary_name:
        log.info("    %s: %s", tool.name, dest_dir / tool.binary_name)


def download_all(cfg: Config) -> None:
    """Download all tools into mkosi.input/tools/{arch}/."""
    arch = cfg.arch_info.dl_arch
    output_base = cfg.tools_output

    for tool in TOOLS:
        download_tool(tool, arch, output_base)

    log.info("Tool download complete.")
    log.info("All tools ready.")
