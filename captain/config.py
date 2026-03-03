"""Build configuration populated from environment variables."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from captain.util import ArchInfo, get_arch_info

# Valid values for KERNEL_MODE and MKOSI_MODE.
VALID_MODES = ("docker", "native", "skip")


@dataclass(slots=True)
class Config:
    """All build configuration, loaded once from environment variables."""

    # Paths
    project_dir: Path
    output_dir: Path

    # Target
    arch: str = "amd64"
    kernel_version: str = "6.12.69"
    kernel_src: str | None = None

    # Docker
    builder_image: str = "captainos-builder"
    no_cache: bool = False

    # Per-stage mode: "docker" | "native" | "skip"
    kernel_mode: str = "docker"
    mkosi_mode: str = "docker"
    iso_mode: str = "skip"

    # Force flags
    force_kernel: bool = False
    force_tools: bool = False

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
        for name, value in (
            ("KERNEL_MODE", self.kernel_mode),
            ("MKOSI_MODE", self.mkosi_mode),
            ("ISO_MODE", self.iso_mode),
        ):
            if value not in VALID_MODES:
                print(
                    f"ERROR: {name}={value!r} is invalid. "
                    f"Valid values: {', '.join(VALID_MODES)}",
                    file=sys.stderr,
                )
                sys.exit(1)

    @property
    def needs_docker(self) -> bool:
        """True if any stage requires Docker."""
        return (
            self.kernel_mode == "docker"
            or self.mkosi_mode == "docker"
            or self.iso_mode == "docker"
        )

    @classmethod
    def from_env(cls, project_dir: Path) -> Config:
        """Create a Config from environment variables.

        KERNEL_MODE / MKOSI_MODE accept "docker" (default), "native", or "skip".
        """
        return cls(
            project_dir=project_dir,
            output_dir=project_dir / "out",
            arch=os.environ.get("ARCH", "amd64"),
            kernel_version=os.environ.get("KERNEL_VERSION", "6.12.69"),
            kernel_src=os.environ.get("KERNEL_SRC") or None,
            builder_image=os.environ.get("BUILDER_IMAGE", "captainos-builder"),
            no_cache=os.environ.get("NO_CACHE") == "1",
            kernel_mode=os.environ.get("KERNEL_MODE", "docker"),
            mkosi_mode=os.environ.get("MKOSI_MODE", "docker"),
            iso_mode=os.environ.get("ISO_MODE", "skip"),
            force_kernel=os.environ.get("FORCE_KERNEL") == "1",
            force_tools=os.environ.get("FORCE_TOOLS") == "1",
            qemu_append=os.environ.get("QEMU_APPEND", ""),
            qemu_mem=os.environ.get("QEMU_MEM", "2G"),
            qemu_smp=os.environ.get("QEMU_SMP", "2"),
        )

    @property
    def extra_tree_output(self) -> Path:
        """Per-arch mkosi ExtraTrees staging directory.

        Everything placed here (kernel modules, tools, etc.) is merged
        into the initramfs CPIO by mkosi via ``--extra-tree=``.
        """
        return self.project_dir / "mkosi.output" / "extra-tree" / self.arch

    @property
    def vmlinuz_output(self) -> Path:
        """Per-arch directory for the vmlinuz kernel image.

        Kept outside ExtraTrees so the kernel binary is not packed into
        the initramfs CPIO — iPXE loads it separately.
        """
        return self.project_dir / "mkosi.output" / "vmlinuz" / self.arch

    @property
    def mkosi_output(self) -> Path:
        return self.project_dir / "mkosi.output"

    @property
    def initramfs_output(self) -> Path:
        """Per-arch directory for mkosi initramfs output (image.cpio.zst)."""
        return self.project_dir / "mkosi.output" / "initramfs" / self.arch

    @property
    def iso_output(self) -> Path:
        """Per-arch directory for the built ISO image."""
        return self.project_dir / "mkosi.output" / "iso" / self.arch

    @property
    def iso_staging(self) -> Path:
        """Per-arch staging directory for assembling the ISO filesystem."""
        return self.project_dir / "mkosi.output" / "iso-staging" / self.arch
