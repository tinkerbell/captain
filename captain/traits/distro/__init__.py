"""Distro traits: distribution/release, package family, package-name mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from captain.traits import Axis, Trait

if TYPE_CHECKING:
    from captain.flavor import Flavor


class Distro(Trait):
    axis = Axis.DISTRO
    content = "distro"
    family: ClassVar[str]
    mkosi_distribution: ClassVar[str]
    # Logical (Debian) package name -> this distro's name; None drops the package.
    package_map: ClassVar[dict[str, str | None]] = {}
    # Where the distro's own kernel installs its DTBs, relative to the root (a single-match glob).
    kernel_dtb_glob: ClassVar[str]
    # Packages providing switch_root(8), when not already in util-linux.
    switch_root_packages: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, release: str, cache_epoch: int = 0) -> None:
        self.release = release
        # Emergency knob: bump to rebuild every flavor of this distro despite
        # unchanged inputs, eg. after a CVE fix lands in its package repos.
        self.cache_epoch = cache_epoch

    @property
    def name(self) -> str:
        return f"{self.mkosi_distribution}-{self.release}"

    def custom_names(self) -> list[str]:
        return [self.mkosi_distribution, self.name]

    def cache_bust_token(self, flavor: Flavor) -> str:
        return f"{self.name}-epoch-{self.cache_epoch}" if self.cache_epoch else ""

    def map_packages(self, packages: set[str]) -> set[str]:
        mapped = {self.package_map.get(p, p) for p in packages}
        return {p for p in mapped if p is not None}

    def generic_kernel_packages(self) -> set[str]:
        """The distro's standard, generic kernel."""
        raise NotImplementedError

    def noop_initramfs_packages(self) -> set[str]:
        """Whatever satisfies the kernel's initramfs-generator dependency, cheaply.

        Used for in-memory outputs, where the image itself is the initramfs.
        """
        raise NotImplementedError

    # The distro's default initramfs generator, for disk images: "initramfs-tools" or "dracut".
    initramfs_generator: ClassVar[str] = "dracut"

    def default_initramfs_packages(self) -> set[str]:
        return {self.initramfs_generator}


class DebFamily(Distro):
    family = "deb"
    content = "distro/deb"
    kernel_dtb_glob = "usr/lib/linux-image-*"

    def noop_initramfs_packages(self) -> set[str]:
        return {"tiny-initramfs"}


class Debian(DebFamily):
    mkosi_distribution = "debian"
    initramfs_generator = "initramfs-tools"

    def generic_kernel_packages(self) -> set[str]:
        return {"linux-image-generic"}


class Ubuntu(DebFamily):
    """Ubuntu; needs universe (tiny-initramfs, fastfetch)."""

    content = "distro/ubuntu"
    mkosi_distribution = "ubuntu"
    kernel_dtb_glob = "usr/lib/firmware/*/device-tree"
    switch_root_packages = frozenset(["util-linux-extra"])

    def generic_kernel_packages(self) -> set[str]:
        # linux-image-generic depends on linux-firmware (~770M of every firmware there is);
        # the empty linux-firmware-minimal provides it, matching Debian's firmware-less kernel.
        return {"linux-image-generic", "linux-firmware-minimal"}


class RpmFamily(Distro):
    family = "rpm"
    content = "distro/rpm"
    kernel_dtb_glob = "usr/lib/modules/*/dtb"
    package_map: ClassVar[dict[str, str | None]] = {
        "apt": "dnf",
        "cloud-guest-utils": "cloud-utils-growpart",
        "systemd-boot-efi": "systemd-boot-unsigned",
        "conntrack": "conntrack-tools",
        "dmsetup": "device-mapper",
        "ebtables": None,  # gone in EL10; iptables-nft covers kubeadm's needs
        "iproute2": "iproute",
        "iptables": "iptables-nft",
        "iputils-ping": "iputils",
        "login": None,  # in util-linux
        "lsb-release": None,  # redhat-lsb is gone in EL10; cloud-init reads os-release there
        "polkitd": "polkit",
        "procps": "procps-ng",
        "systemd-sysv": None,  # in systemd
        "udev": "systemd-udev",
    }

    def packages(self, flavor: Flavor) -> set[str]:
        # Debian's systemd ships networkd; RPM distros split it out.
        return {"systemd-networkd"}

    def noop_initramfs_packages(self) -> set[str]:
        # The kernel hard-requires dracut, which it gets anyway; nothing to add.
        return set()


class Rocky(RpmFamily):
    """Rocky Linux; needs EPEL (systemd-networkd, systemd-timesyncd, fastfetch, pv)."""

    content = "distro/rocky"
    mkosi_distribution = "rocky"

    def packages(self, flavor: Flavor) -> set[str]:
        return super().packages(flavor) | {"rocky-release", "epel-release"}

    def generic_kernel_packages(self) -> set[str]:
        # EL10 moved nft_compat and the xt_* matches/targets to kernel-modules-extra;
        # CNI's iptables(-nft) rules need them, as does kube-proxy.
        return {"kernel", "kernel-modules-extra"}
