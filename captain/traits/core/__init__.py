"""Core trait: content and packages shared by every flavor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from captain.traits import Axis, Trait

if TYPE_CHECKING:
    from captain.flavor import Flavor

# Logical (Debian) package names; the distro family maps them to its own names.
BASE_PACKAGES = {
    # systemd and core
    "systemd",
    "systemd-sysv",  # Provides poweroff, shutdown, reboot, halt, etc.
    "systemd-resolved",
    "systemd-timesyncd",
    "udev",
    "dbus",
    "kmod",
    "dmsetup",
    "polkitd",
    # Logging
    "rsyslog",
    # Shell and basic userspace
    "bash",
    "login",
    "less",
    "util-linux",
    "procps",
    "coreutils",
    "findutils",
    "grep",
    "sed",
    # Networking
    "iproute2",
    "iputils-ping",
    "iptables",
    "nftables",
    "ca-certificates",
    # Container runtime: containerd 2.x and runc come from the tools stage (captain/tools.py)
    # Compression
    "pigz",
    "fastfetch",
    "pciutils",  # for listing PCI devices
    "curl",
    "pv",
}


class Core(Trait):
    axis = Axis.CORE
    content = "core"

    def packages(self, flavor: Flavor) -> set[str]:
        packages = set(BASE_PACKAGES)
        if flavor.keep_package_manager():
            packages.add("apt")
        return packages
