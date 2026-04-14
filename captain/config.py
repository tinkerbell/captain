"""Build configuration populated from CLI args / environment variables."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from captain.util import ArchInfo, get_arch_info

log = logging.getLogger(__name__)

# Valid values for KERNEL_MODE and MKOSI_MODE.
VALID_MODES = ("docker", "native", "skip")

# The single source of truth for the default kernel version.
# Override at runtime via --kernel-version or KERNEL_VERSION env var.
DEFAULT_KERNEL_VERSION = "6.18.16"


@dataclass(slots=True)
class Config:
    """All build configuration, loaded once from environment variables."""

    # Paths
    project_dir: Path
    output_dir: Path

    # Target
    arch: str = "amd64"
    kernel_version: str = DEFAULT_KERNEL_VERSION
    kernel_config: str | None = None
    kernel_src: str | None = None

    # Docker
    builder_image: str = "captainos-builder"
    no_cache: bool = False

    # Per-stage mode: "docker" | "native" | "skip"
    kernel_mode: str = "docker"
    tools_mode: str = "docker"
    mkosi_mode: str = "docker"
    iso_mode: str = "docker"
    release_mode: str = "docker"

    # Force flags
    force_kernel: bool = False
    force_tools: bool = False
    force_iso: bool = False

    # QEMU
    qemu_append: str = ""
    qemu_mem: str = "2G"
    qemu_smp: str = "2"

    # mkosi passthrough
    mkosi_args: list[str] = field(default_factory=list)

    # Derived (set in __post_init__)
    arch_info: ArchInfo = field(init=False)

    def __post_init__(self) -> None:
        self.arch_info = get_arch_info(self.arch)
        self.arch = self.arch_info.arch  # normalise aliases (x86_64 → amd64, etc.)
        for name, value in (
            ("KERNEL_MODE", self.kernel_mode),
            ("TOOLS_MODE", self.tools_mode),
            ("MKOSI_MODE", self.mkosi_mode),
            ("ISO_MODE", self.iso_mode),
            ("RELEASE_MODE", self.release_mode),
        ):
            if value not in VALID_MODES:
                log.error("%s=%r is invalid. Valid values: %s", name, value, ", ".join(VALID_MODES))
                sys.exit(1)

    @property
    def needs_docker(self) -> bool:
        """True if any stage requires Docker."""
        return (
            self.kernel_mode == "docker"
            or self.tools_mode == "docker"
            or self.mkosi_mode == "docker"
            or self.iso_mode == "docker"
            or self.release_mode == "docker"
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace, project_dir: Path) -> Config:
        """Create a Config from a parsed :class:`argparse.Namespace`.

        The *args* namespace is produced by :mod:`configargparse` which
        has already resolved the priority chain:
        CLI flags > environment variables > defaults.

        ``getattr`` with fallbacks is used because per-subcommand
        parsers only define the flags relevant to that subcommand.
        """
        return cls(
            project_dir=project_dir,
            output_dir=project_dir / "out",
            arch=getattr(args, "arch", "amd64"),
            kernel_version=getattr(args, "kernel_version", DEFAULT_KERNEL_VERSION),
            kernel_config=getattr(args, "kernel_config", None) or None,
            kernel_src=getattr(args, "kernel_src", None) or None,
            builder_image=getattr(args, "builder_image", "captainos-builder"),
            no_cache=getattr(args, "no_cache", False),
            kernel_mode=getattr(args, "kernel_mode", "docker"),
            tools_mode=getattr(args, "tools_mode", "docker"),
            mkosi_mode=getattr(args, "mkosi_mode", "docker"),
            iso_mode=getattr(args, "iso_mode", "docker"),
            release_mode=getattr(args, "release_mode", "docker"),
            force_kernel=getattr(args, "force_kernel", False),
            force_tools=getattr(args, "force_tools", False),
            force_iso=getattr(args, "force_iso", False),
            qemu_append=getattr(args, "qemu_append", ""),
            qemu_mem=getattr(args, "qemu_mem", "2G"),
            qemu_smp=getattr(args, "qemu_smp", "2"),
        )

    @classmethod
    def from_env(cls, project_dir: Path) -> Config:
        """Create a Config from environment variables (legacy helper).

        Prefer :meth:`from_args` in the CLI path.  This method remains
        for any non-CLI callers (e.g. tests, scripts) that need a
        ``Config`` without going through argparse.
        """
        return cls(
            project_dir=project_dir,
            output_dir=project_dir / "out",
            arch=os.environ.get("ARCH", "amd64"),
            kernel_version=os.environ.get("KERNEL_VERSION", DEFAULT_KERNEL_VERSION),
            kernel_config=os.environ.get("KERNEL_CONFIG") or None,
            kernel_src=os.environ.get("KERNEL_SRC") or None,
            builder_image=os.environ.get("BUILDER_IMAGE", "captainos-builder"),
            no_cache=os.environ.get("NO_CACHE") == "1",
            kernel_mode=os.environ.get("KERNEL_MODE", "docker"),
            tools_mode=os.environ.get("TOOLS_MODE", "docker"),
            mkosi_mode=os.environ.get("MKOSI_MODE", "docker"),
            iso_mode=os.environ.get("ISO_MODE", "docker"),
            release_mode=os.environ.get("RELEASE_MODE", "docker"),
            force_kernel=os.environ.get("FORCE_KERNEL") == "1",
            force_tools=os.environ.get("FORCE_TOOLS") == "1",
            force_iso=os.environ.get("FORCE_ISO") == "1",
            qemu_append=os.environ.get("QEMU_APPEND", ""),
            qemu_mem=os.environ.get("QEMU_MEM", "2G"),
            qemu_smp=os.environ.get("QEMU_SMP", "2"),
        )

    @property
    def tools_output(self) -> Path:
        """Per-arch staging directory for downloaded tools.

        Contains containerd, runc, nerdctl, and CNI plugins.
        Passed to mkosi via ``--extra-tree=`` to merge into the initramfs.
        Kernel modules are stored separately under :attr:`kernel_output`.
        """
        return self.project_dir / "mkosi.output" / "tools" / self.arch

    @property
    def kernel_output(self) -> Path:
        """Per-version, per-arch directory for all kernel build artifacts.

        Contains the vmlinuz image (loaded separately by iPXE) and
        a ``modules/`` subtree that mirrors a root filesystem layout
        (``usr/lib/modules/{kver}/``) so it can be passed directly
        as an ``--extra-tree=`` to mkosi.
        """
        return self.project_dir / "mkosi.output" / "kernel" / self.kernel_version / self.arch

    @property
    def modules_output(self) -> Path:
        """Per-version, per-arch root for kernel modules.

        Returns ``kernel/{version}/{arch}/modules`` which contains a
        merged-usr tree (``usr/lib/modules/{kver}/``) suitable for
        passing as ``--extra-tree=`` to mkosi.
        """
        return self.kernel_output / "modules"

    @property
    def mkosi_output(self) -> Path:
        return self.project_dir / "mkosi.output"

    @property
    def initramfs_output(self) -> Path:
        """Per-version, per-arch directory for mkosi initramfs output."""
        return self.project_dir / "mkosi.output" / "initramfs" / self.kernel_version / self.arch

    @property
    def iso_output(self) -> Path:
        """Per-version, per-arch directory for the built ISO image."""
        return self.project_dir / "mkosi.output" / "iso" / self.kernel_version / self.arch

    @property
    def iso_staging(self) -> Path:
        """Per-version, per-arch staging directory for assembling the ISO filesystem."""
        return self.iso_output / "staging"
