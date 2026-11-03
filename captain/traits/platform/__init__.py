"""Platform traits: firmware interface (ACPI/DeviceTree) and kernel source."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from captain.traits import Axis, Trait

if TYPE_CHECKING:
    from captain.flavor import Flavor


class Platform(Trait):
    axis = Axis.PLATFORM
    # Can boot a generic (UEFI) ISO image.
    iso_capable: ClassVar[bool] = False
    # The kernel ships DTBs worth exporting (arm64 only).
    ships_dtbs: ClassVar[bool] = True
    # How disk images boot: "uefi" (ESP + systemd-boot), "extlinux" (U-Boot) or "rpi" (RPi firmware)
    boot_scheme: ClassVar[str]

    def kernel_dtb_glob(self, flavor: Flavor) -> str:
        """Where the kernel installs its DTBs, relative to the root (a single-match glob)."""
        return flavor.distro.kernel_dtb_glob

    def kernel_names(self) -> list[str]:
        """Dir names this platform's kernel matches under custom/by-kernel/."""
        raise NotImplementedError

    def kernel_cmdline_extra(self) -> list[str]:
        """Extra kernel command line arguments for disk images."""
        return []


class Acpi(Platform):
    """UEFI+ACPI machines."""

    content = "platform/acpi"
    iso_capable = True
    boot_scheme = "uefi"

    def custom_names(self) -> list[str]:
        return ["acpi"]

    def packages(self, flavor: Flavor) -> set[str]:
        return {"acpid", "efibootmgr"}


class AcpiDistroKernel(Acpi):
    """UEFI+ACPI with the distro's own generic kernel and a full userland."""

    def kernel_names(self) -> list[str]:
        return ["distro"]

    def packages(self, flavor: Flavor) -> set[str]:
        return (
            super().packages(flavor)
            | flavor.distro.generic_kernel_packages()
            | flavor.output.kernel_initramfs_packages(flavor)
        )

    def keep_package_manager(self, flavor: Flavor) -> bool:
        return True

    def keep_hwdb(self, flavor: Flavor) -> bool:
        return True


class AcpiCaptainKernel(Acpi):
    """UEFI+ACPI with the slim CaptainOS kernel, built from source (deb family only)."""

    ships_dtbs = False

    def kernel_names(self) -> list[str]:
        return ["captainos"]

    def pre_mkosi(self, flavor: Flavor) -> None:
        from captain.cli import stages

        # ensure the kernel .deb is present in mkosi.input/kernel/<arch>
        stages.build_kernel_stage(flavor.cfg)

    def packages(self, flavor: Flavor) -> set[str]:
        return super().packages(flavor) | {f"linux-image-{flavor.cfg.kernel_version}-captainos"}

    def package_directories(self, flavor: Flavor) -> set[str]:
        cfg = flavor.cfg
        return {str(cfg.kernel_output.relative_to(cfg.project_dir))}

    def cache_inputs(self, flavor: Flavor) -> dict[str, str]:
        from captain.cache import kernel_key

        return {"kernel": kernel_key(flavor.cfg).digest}


class Armbian(Platform):
    """DeviceTree arm64 machines with an Armbian kernel from armbian-next."""

    content = "platform/armbian"
    supported_architectures = frozenset(["arm64"])
    boot_scheme = "extlinux"
    # Releases with their own armbian-<release> suite; kernels are in the shared "armbian" suite.
    release_suites: ClassVar[frozenset[str]] = frozenset(["bookworm", "trixie", "forky"])

    def __init__(self, kernel_package: str) -> None:
        self.kernel_package = kernel_package

    @property
    def name(self) -> str:
        return f"armbian-{self.kernel_package.removeprefix('linux-image-')}"

    def custom_names(self) -> list[str]:
        return ["armbian"]

    def kernel_names(self) -> list[str]:
        return [self.name]

    def packages(self, flavor: Flavor) -> set[str]:
        return {self.kernel_package} | flavor.output.kernel_initramfs_packages(flavor)

    def keep_package_manager(self, flavor: Flavor) -> bool:
        return True

    def keep_hwdb(self, flavor: Flavor) -> bool:
        return True

    def kernel_dtb_glob(self, flavor: Flavor) -> str:
        # Armbian's kernel .debs use Debian's layout, whatever the distro.
        return "usr/lib/linux-image-*"

    def cache_bust_token(self, flavor: Flavor) -> str:
        # Fold the armbian-next repo version token in so these flavors rebuild
        # when armbian publishes a newer kernel under the same package name.
        return flavor.cfg.armbian_version or ""
