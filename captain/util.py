"""Shared utilities: subprocess wrapper, path helpers, architecture mapping."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ArchInfo:
    """Architecture-specific build parameters."""

    arch: str  # canonical name: amd64 | arm64
    output_arch: str  # user-facing name in artifact filenames: x86_64 | aarch64
    kernel_arch: str  # kernel ARCH value
    cross_compile: str  # CROSS_COMPILE prefix (empty for native)
    image_target: str  # kernel image make target
    kernel_image_path: str  # relative path to built kernel image
    dl_arch: str  # architecture name in download URLs
    mkosi_arch: str  # mkosi --architecture value
    qemu_binary: str  # QEMU system emulator binary
    strip_prefix: str  # prefix for strip command


def get_arch_info(arch: str) -> ArchInfo:
    """Return architecture-specific parameters for the given arch string."""
    match arch:
        case "amd64" | "x86_64":
            return ArchInfo(
                arch="amd64",
                output_arch="x86_64",
                kernel_arch="x86_64",
                cross_compile="",
                image_target="bzImage",
                kernel_image_path="arch/x86/boot/bzImage",
                dl_arch="amd64",
                mkosi_arch="x86-64",
                qemu_binary="qemu-system-x86_64",
                strip_prefix="",
            )
        case "arm64" | "aarch64":
            return ArchInfo(
                arch="arm64",
                output_arch="aarch64",
                kernel_arch="arm64",
                cross_compile="aarch64-linux-gnu-",
                image_target="Image",
                kernel_image_path="arch/arm64/boot/Image",
                dl_arch="arm64",
                mkosi_arch="arm64",
                qemu_binary="qemu-system-aarch64",
                strip_prefix="aarch64-linux-gnu-",
            )
        case _:
            log.error("Unsupported architecture: %s", arch)
            sys.exit(1)


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally merging extra env vars with the current environment."""
    run_env: dict[str, str] | None = None
    if env is not None:
        run_env = {**os.environ, **env}
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        env=run_env,
        cwd=cwd,
    )


def ensure_dir(path: Path) -> Path:
    """Create a directory (and parents) if it doesn't exist, return the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_extractall(
    tf: tarfile.TarFile,
    path: Path | str,
    members: list[tarfile.TarInfo] | None = None,
) -> None:
    """Extract a tarball safely, compatible with Python 3.10+.

    On Python >= 3.12 this delegates to the built-in ``filter='data'``
    parameter.  On older versions it manually sanitises each member to
    prevent path-traversal and other tar-related attacks.
    """
    if sys.version_info >= (3, 12):
        tf.extractall(path=path, members=members, filter="data")  # type: ignore[call-arg]
    else:
        dest = Path(path).resolve()
        items = members if members is not None else tf.getmembers()
        safe: list[tarfile.TarInfo] = []
        for m in items:
            # Block symlinks, hardlinks, and device nodes — they can be
            # used for write-outside-destination attacks.  This mirrors
            # the behaviour of Python 3.12's filter="data".
            if m.issym() or m.islnk():
                continue
            if m.isdev() or m.isblk() or m.ischr() or m.isfifo():
                continue
            # Normalise the name and reject absolute / traversal paths
            m.name = os.path.normpath(m.name)
            if m.name.startswith(("/", "..")) or "/../" in m.name:
                continue
            # Verify the resolved target stays within the destination
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                continue
            # Reset ownership info (mirrors filter="data" behaviour)
            m.uid = m.gid = 0
            m.uname = m.gname = ""
            safe.append(m)
        tf.extractall(path=path, members=safe)


def _missing(cmds: list[str]) -> list[str]:
    """Return command names from *cmds* that are not found on ``$PATH``."""
    import shutil as _shutil

    return [cmd for cmd in cmds if _shutil.which(cmd) is None]


def check_kernel_dependencies(arch: str) -> list[str]:
    """Check host tools required for a native kernel build.

    Returns a list of missing command names (empty if all found).
    """
    required = ["make", "gcc", "flex", "bison", "bc", "rsync", "strip", "zstd", "depmod"]
    if arch in ("arm64", "aarch64"):
        required += ["aarch64-linux-gnu-gcc", "aarch64-linux-gnu-strip"]
    return _missing(required)


def check_mkosi_dependencies() -> list[str]:
    """Check host tools required for a native mkosi image build.

    Returns a list of missing command names (empty if all found).
    """
    return _missing(
        [
            "mkosi",
            "zstd",
            "cpio",
            "bwrap",  # bubblewrap — used by mkosi
            "mksquashfs",  # squashfs-tools — used by mkosi
            "kmod",
        ]
    )


def check_release_dependencies() -> list[str]:
    """Check host tools required for a native release operation.

    Returns a list of missing command names (empty if all found).
    """
    return _missing(["buildah", "skopeo", "git"])


def check_dependencies(arch: str) -> list[str]:
    """Check *all* host tools for a fully native build (kernel + mkosi).

    Returns a list of missing command names (empty if all found).
    """
    return check_kernel_dependencies(arch) + check_mkosi_dependencies()
