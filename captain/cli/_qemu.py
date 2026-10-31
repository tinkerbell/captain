"""``captain qemu`` — boot the built flavor (kernel+initramfs, or disk image) in QEMU."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from click import Context

import captain.flavor
from captain.cli._main import CliContext, cli
from captain.qemu import run_qemu, run_qemu_disk
from captain.traits.output import Img, disk_image_name

log = logging.getLogger(__name__)


@cli.command(
    "qemu",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
    short_help="Run the built captainos kernel+initramfs in QEMU.",
)
@click.option(
    "--mem",
    "qemu_mem",
    envvar="QEMU_MEM",
    default="2G",
    show_default=True,
    help="Guest memory (env: QEMU_MEM).",
)
@click.option(
    "--smp",
    "qemu_smp",
    envvar="QEMU_SMP",
    default="2",
    show_default=True,
    help="Guest CPUs (env: QEMU_SMP).",
)
@click.option(
    "--ssh-port",
    default=2222,
    show_default=True,
    help="Disk images: host port forwarded to the guest's ssh.",
)
@click.option(
    "--ssh-key",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Disk images: ssh public key for cloud-init's 'captain' user [default: ~/.ssh/id_*.pub]",
)
@click.option(
    "--disk-size",
    default="10G",
    show_default=True,
    help="Disk images: size the scratch copy of the disk is grown to.",
)
@click.pass_obj
@click.pass_context
def shell_cmd(
    ctx: Context,
    cli_ctx: CliContext,
    qemu_mem: str,
    qemu_smp: str,
    ssh_port: int,
    ssh_key: Path | None,
    disk_size: str,
) -> None:
    """Boot the built flavor in QEMU.

    In-memory flavors boot kernel+initramfs directly; extra args go to the kernel cmdline.
    Disk-image (img-) flavors boot the image under UEFI, with cloud-init seeded over NoCloud.
    """
    log.debug("Running captainos in QEMU...")

    cfg = cli_ctx.make_config(qemu_mem=qemu_mem, qemu_smp=qemu_smp)

    flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, cfg)
    if isinstance(flavor.output, Img):
        if ssh_key is None:
            ssh_key = next(iter(sorted(Path.home().glob(".ssh/id_*.pub"))), None)
        image = cfg.output_dir / disk_image_name(cfg.flavor_id, cfg.arch_info.output_arch)
        run_qemu_disk(cfg, image, ssh_port, ssh_key, disk_size)
        log.info("qemu exited.")
        return

    extra_args: list[str] = ctx.args
    log.debug(f"Extra args passed to qemu: {extra_args}")

    run_qemu(cfg, extra_args)

    log.info("qemu exited.")
