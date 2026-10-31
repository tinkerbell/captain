"""Raspberry Pi platform: Armbian bcm2711 kernel plus ready-to-netboot RPi firmware."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from captain.artifacts import OutputArchArtifact, OutputArchArtifactType
from captain.config import Config
from captain.tools import _download_binary
from captain.traits.platform import Armbian
from captain.util import ensure_dir, symlink_relative

if TYPE_CHECKING:
    from captain.flavor import Flavor

log: logging.Logger = logging.getLogger(__name__)


class ArmbianRpi(Armbian):
    """Raspberry Pi 4B and newer. Netboot (mem): Tinkerbell serves config.txt/cmdline.txt.

    Disk images (img) boot natively from a FAT partition with the firmware, kernel, initrd,
    DTBs and a generic config.txt/cmdline.txt (no U-Boot); workflows may customize them.
    """

    content = "platform/rpi"
    boot_scheme = "rpi"
    rpi_fw_tag = "1.20260408"

    def __init__(self) -> None:
        super().__init__("linux-image-current-bcm2711")

    def custom_names(self) -> list[str]:
        return [*super().custom_names(), "rpi"]

    def kernel_cmdline_extra(self) -> list[str]:
        return [
            "rootwait",
            # The last console= is /dev/console (systemd, cloud-init...): serial, as HDMI is
            # usually unplugged; tty1 still gets kernel messages.
            "console=tty1",
            "console=serial0,115200",
            # The firmware prepends cgroup_disable=memory; kubelet/kubeadm need the memory
            # controller. The RPi kernel honours a later cgroup_enable=.
            "cgroup_enable=memory",
            "cgroup_memory=1",
        ]

    def cache_inputs(self, flavor: Flavor) -> dict[str, str]:
        return super().cache_inputs(flavor) | {"rpi_firmware": self.rpi_fw_tag}

    def _netboot(self, flavor: Flavor) -> bool:
        return flavor.output.output_kind == "initramfs"

    def add_arch_artifacts(
        self, flavor: Flavor, output_arch: str, artifacts: list[OutputArchArtifact]
    ) -> None:
        if not self._netboot(flavor):
            return
        artifacts.append(
            OutputArchArtifact(
                type=OutputArchArtifactType.DIRECTORY, name=self.firmware_out_dirname()
            )
        )

    def pre_mkosi(self, flavor: Flavor) -> None:
        # Disk images carry the firmware in their boot partition, via ExtraTrees (mkosi.conf.j2).
        if not self._netboot(flavor):
            self._download_firmware(flavor.cfg.project_dir / self.firmware_input_relative())

    def post_mkosi(self, flavor: Flavor) -> None:
        if self._netboot(flavor):
            self._download_firmware(self.firmware_output(flavor.cfg))

    def post_artifact_collect(self, flavor: Flavor) -> None:
        if not self._netboot(flavor):
            return
        cfg = flavor.cfg
        out: Path = ensure_dir(cfg.output_dir)
        log.debug("Copying firmware directory from %s to %s", self.firmware_output(cfg), out)
        target_fw_dir = out / self.firmware_out_dirname()
        if target_fw_dir.exists():
            shutil.rmtree(target_fw_dir)
        shutil.copytree(self.firmware_output(cfg), target_fw_dir)
        log.info("Copied firmware directory: %s", target_fw_dir)

        ## Symlink all dtbs directly in the fw dir
        out_dtbs = out / f"dtb-{cfg.flavor_id}-{cfg.arch_info.output_arch}"
        broadcom_dtbs = out_dtbs / "broadcom"
        if not broadcom_dtbs.is_dir():
            log.error(
                "Expected dtb directory %s does not exist, skipping dtb symlinks", broadcom_dtbs
            )
            raise ValueError(f"Expected dtb directory {broadcom_dtbs} does not exist")
        for dtb in broadcom_dtbs.glob("*.dtb"):
            target_dtb = target_fw_dir / dtb.name
            log.debug("Symlinking dtb: %s to %s", dtb, target_dtb)
            symlink_relative(target_dtb, dtb)

        # Symlink all overlays (which are in the same level as "broadcom") into an "overlays" subdir
        broadcom_overlays = out_dtbs / "overlays"
        if not broadcom_overlays.is_dir():
            log.error("Expected dtb overlays directory %s does not exist", broadcom_overlays)
            raise ValueError(f"Expected dtb overlays directory {broadcom_overlays} does not exist")
        target_overlays = ensure_dir(target_fw_dir / "overlays")
        for overlay in broadcom_overlays.glob("*.dtbo"):
            log.debug("Symlinking dtb overlay %s to firmware overlays directory", overlay)
            target_overlay = target_overlays / overlay.name
            symlink_relative(target_overlay, overlay)

        ## Symlink the kernel and initramfs
        # kernel as kernel8.img
        # see https://www.raspberrypi.com/documentation/computers/config_txt.html#kernel
        kernel_src = out / f"vmlinuz-{cfg.flavor_id}-{cfg.arch_info.output_arch}"
        if not kernel_src.is_file():
            log.error(
                "Expected kernel image %s does not exist, cannot symlink to firmware dir",
                kernel_src,
            )
            raise ValueError(f"Expected kernel image {kernel_src} does not exist")
        kernel_dst = target_fw_dir / "kernel8.img"
        log.debug("Symlinking kernel from %s to %s", kernel_src, kernel_dst)
        symlink_relative(kernel_dst, kernel_src)

        # initramfs as initramfs8
        # see https://www.raspberrypi.com/documentation/computers/config_txt.html#initramfs
        initramfs_src = out / f"initramfs-{cfg.flavor_id}-{cfg.arch_info.output_arch}"
        if not initramfs_src.is_file():
            log.error(
                "Expected initramfs image %s does not exist, cannot symlink to firmware dir",
                initramfs_src,
            )
            raise ValueError(f"Expected initramfs image {initramfs_src} does not exist")
        initramfs_dst = target_fw_dir / "initramfs8"
        log.debug("Symlinking initramfs from %s to %s", initramfs_src, initramfs_dst)
        symlink_relative(initramfs_dst, initramfs_src)

    def firmware_out_dirname(self) -> str:
        # return f"firmware-{cfg.flavor_id}-{cfg.arch_info.output_arch}"
        return "firmware-rpi"

    def firmware_output(self, cfg: Config) -> Path:
        return ensure_dir(cfg.initramfs_output / "firmware")

    def firmware_input_relative(self) -> str:
        """Firmware for disk images, relative to the project dir (merged into /efi by mkosi)."""
        return f"mkosi.input/rpi-firmware/{self.rpi_fw_tag}"

    def rpi_firmware_base_url(self) -> str:
        return (
            "https://raw.githubusercontent.com/raspberrypi/firmware/refs/tags/"
            + self.rpi_fw_tag
            + "/boot/"
        )

    def _download_firmware(self, dest: Path) -> None:
        rpi_fw_files = [
            "bootcode.bin",
            "fixup4cd.dat",
            "fixup4.dat",
            "fixup4db.dat",
            "fixup4x.dat",
            "fixup_cd.dat",
            "fixup.dat",
            "fixup_db.dat",
            "fixup_x.dat",
            "LICENCE.broadcom",
            "start4cd.elf",
            "start4db.elf",
            "start4.elf",
            "start4x.elf",
            "start_cd.elf",
            "start_db.elf",
            "start.elf",
            "start_x.elf",
        ]
        for filename in rpi_fw_files:
            url = self.rpi_firmware_base_url() + filename
            output_path = dest / filename
            if output_path.is_file():
                log.debug(
                    "Raspberry Pi firmware file %s already exists at %s, skipping download",
                    url,
                    output_path,
                )
                continue
            _download_binary(url, output_path)  # logs the URL
