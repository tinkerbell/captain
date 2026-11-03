"""Output traits: what mkosi produces (in-memory initramfs, disk image...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from captain.artifacts import (
    OutputArchArtifact,
    OutputArchArtifactType,
    collect_disk_image,
    collect_dtbs,
    collect_initramfs,
    collect_kernel,
)
from captain.traits import Axis, Trait

if TYPE_CHECKING:
    from captain.flavor import Flavor


class Output(Trait):
    axis = Axis.OUTPUT
    supports_iso: ClassVar[bool] = False
    # mkosi.output/<output_kind>/<flavor>/<arch>/<hash>/
    output_kind: ClassVar[str]
    # For humans: banner, motd, /etc/issue and /etc/os-release.
    label: ClassVar[str]

    def kernel_initramfs_packages(self, flavor: Flavor) -> set[str]:
        """Packages providing the kernel's initramfs generator, for this output."""
        raise NotImplementedError

    def collect_artifacts(self, flavor: Flavor) -> None:
        """Copy mkosi's outputs into out/."""
        raise NotImplementedError


class Mem(Output):
    """In-memory: kernel + initramfs (cpio) holding the whole OS."""

    content = "output/mem"
    supports_iso = True
    output_kind = "initramfs"
    label = "in-memory"

    def collect_artifacts(self, flavor: Flavor) -> None:
        collect_initramfs(flavor.cfg)
        collect_kernel(flavor.cfg)
        collect_dtbs(flavor.cfg)

    def packages(self, flavor: Flavor) -> set[str]:
        # /init switch_root's from rootfs to a tmpfs before starting systemd
        return set(flavor.distro.switch_root_packages)

    def kernel_initramfs_packages(self, flavor: Flavor) -> set[str]:
        return flavor.distro.noop_initramfs_packages()

    def add_arch_artifacts(
        self, flavor: Flavor, output_arch: str, artifacts: list[OutputArchArtifact]
    ) -> None:
        flavor_id = flavor.cfg.flavor_id
        artifacts += [
            OutputArchArtifact(
                type=OutputArchArtifactType.FILE, name=f"vmlinuz-{flavor_id}-{output_arch}"
            ),
            OutputArchArtifact(
                type=OutputArchArtifactType.FILE, name=f"initramfs-{flavor_id}-{output_arch}"
            ),
        ]
        if flavor.has_iso():
            artifacts.append(
                OutputArchArtifact(
                    type=OutputArchArtifactType.FILE,
                    name=f"captainos-{flavor_id}-{output_arch}.iso",
                )
            )
        if output_arch == "aarch64" and flavor.platform.ships_dtbs:
            artifacts.append(
                OutputArchArtifact(
                    type=OutputArchArtifactType.DIRECTORY, name=f"dtb-{flavor_id}-{output_arch}"
                )
            )


class Img(Output):
    """Disk image for final deployment: GPT with the distro's own initrd and bootloader setup.

    UEFI platforms get an ESP with systemd-boot and a BLS entry; DeviceTree platforms get
    an extlinux.conf for U-Boot (assumed to live on SPI/eMMC boot partitions).
    """

    content = "output/img"
    output_kind = "image"
    label = "disk image"

    def kernel_initramfs_packages(self, flavor: Flavor) -> set[str]:
        return flavor.distro.default_initramfs_packages()

    def packages(self, flavor: Flavor) -> set[str]:
        # sudo: for cloud-init's users; cloud-guest-utils: growpart, for cloud-init's growpart
        packages = {"openssh-server", "sudo", "cloud-guest-utils"}
        if flavor.platform.boot_scheme == "uefi":
            packages.add("systemd-boot-efi")
        return packages

    def kernel_cmdline(self, flavor: Flavor) -> str:
        cmdline = ["root=PARTLABEL=root", "rw"]
        if flavor.platform.boot_scheme == "uefi" and flavor.cfg.arch == "amd64":
            # arm64 UEFI machines advertise their serial console via ACPI SPCR
            cmdline += ["console=tty0", "console=ttyS0,115200"]
        cmdline += flavor.platform.kernel_cmdline_extra()
        return " ".join(cmdline)

    def collect_artifacts(self, flavor: Flavor) -> None:
        collect_disk_image(flavor.cfg)

    def add_arch_artifacts(
        self, flavor: Flavor, output_arch: str, artifacts: list[OutputArchArtifact]
    ) -> None:
        artifacts.append(
            OutputArchArtifact(
                type=OutputArchArtifactType.FILE,
                name=disk_image_name(flavor.cfg.flavor_id, output_arch),
            )
        )


def disk_image_name(flavor_id: str, output_arch: str) -> str:
    return f"captainos-{flavor_id}-{output_arch}.img.zst"
