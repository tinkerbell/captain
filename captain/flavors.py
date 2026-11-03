"""The flavor registry: every buildable flavor, composed from traits.

Existing IDs (e.g. ``trixie-full``) are implicitly in-memory + tink-agent.
"""

from __future__ import annotations

from captain.flavor import Flavor
from captain.traits.distro import Debian, Rocky, Ubuntu
from captain.traits.output import Img, Mem
from captain.traits.platform import AcpiCaptainKernel, AcpiDistroKernel, Armbian
from captain.traits.platform.rpi import ArmbianRpi
from captain.traits.role import KubernetesCapt, Tink

TRIXIE = Debian("trixie")
RESOLUTE = Ubuntu("resolute")
ROCKY10 = Rocky("10")

FLAVORS: list[Flavor] = [
    Flavor(
        id="trixie-full",
        release_tag_suffix="",
        name="Trixie Full",
        description="Debian Trixie based with linux-image-generic standard Debian kernel",
        distro=TRIXIE,
        platform=AcpiDistroKernel(),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="trixie-slim",
        release_tag_suffix="-slim",
        name="Trixie Slim",
        description="Debian Trixie based with captainos slim kernel",
        distro=TRIXIE,
        platform=AcpiCaptainKernel(),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="trixie-armbian-rpi",
        release_tag_suffix="-rpi",
        name="Trixie for Raspberry Pi - Armbian bcm2711-current Kernel",
        description="Debian Trixie based on Armbian's bcm2711-current kernel",
        distro=TRIXIE,
        platform=ArmbianRpi(),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="trixie-genio",
        release_tag_suffix="-genio",
        name="Trixie for Mediatek Genio 64-bit ARM machines",
        description="Debian Trixie with Armbian's genio-edge kernel",
        distro=TRIXIE,
        platform=Armbian("linux-image-edge-genio"),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="trixie-meson64",
        release_tag_suffix="-meson64",
        name="Trixie for Meson (Amlogic) 64-bit ARM machines",
        description="Debian Trixie based with Armbian's meson64-edge kernel",
        distro=TRIXIE,
        platform=Armbian("linux-image-edge-meson64"),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="trixie-rockchip64",
        release_tag_suffix="-rockchip64",
        name="Trixie for Rockchip 64-bit ARM machines",
        description="Debian Trixie based with Armbian's rockchip64-edge kernel",
        distro=TRIXIE,
        platform=Armbian("linux-image-edge-rockchip64"),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="trixie-rockchip64-vendor",
        release_tag_suffix="-rockchip64-vendor",
        name="Trixie for Rockchip 64-bit ARM machines - Rockchip Vendor Kernel",
        description="Debian Trixie based with Armbian's rk35xx-vendor kernel",
        distro=TRIXIE,
        platform=Armbian("linux-image-vendor-rk35xx"),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="rockylinux10-full",
        name="Rocky Linux 10 Full",
        description="Rocky Linux 10 (+EPEL) with the standard Rocky kernel",
        distro=ROCKY10,
        platform=AcpiDistroKernel(),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="resolute-full",
        name="Resolute Full",
        description="Ubuntu 26.04 Resolute with the standard linux-image-generic Ubuntu kernel",
        distro=RESOLUTE,
        platform=AcpiDistroKernel(),
        role=Tink(),
        output=Mem(),
    ),
    Flavor(
        id="resolute-rockchip64",
        name="Resolute for Rockchip 64-bit ARM machines",
        description="Ubuntu 26.04 Resolute with Armbian's rockchip64-edge kernel",
        distro=RESOLUTE,
        platform=Armbian("linux-image-edge-rockchip64"),
        role=Tink(),
        output=Mem(),
    ),
]

# Kubernetes (CAPT) nodes, from the same bases as the tink flavors above: as disk images
# for final deployment (img-), and in-memory (mem-) for machines without a proper disk.
for _base_id in (
    "trixie-full",
    "resolute-full",
    "rockylinux10-full",
    "trixie-rockchip64",
    "trixie-armbian-rpi",
):
    _base = next(f for f in FLAVORS if f.id == _base_id)
    FLAVORS += [
        Flavor(
            id=f"img-k8s-capt-{_base_id}",
            name=f"Kubernetes (CAPT) node disk image, {_base.name}",
            description=f"Kubernetes node disk image for CAPT; {_base.description}",
            distro=_base.distro,
            platform=_base.platform,
            role=KubernetesCapt(),
            output=Img(),
        ),
        Flavor(
            id=f"mem-k8s-capt-{_base_id}",
            name=f"In-memory Kubernetes (CAPT) node, {_base.name}",
            description=f"In-memory Kubernetes node for CAPT; {_base.description}",
            distro=_base.distro,
            platform=_base.platform,
            role=KubernetesCapt(),
            output=Mem(),
        ),
    ]
