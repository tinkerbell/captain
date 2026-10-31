"""``captain iso`` — build a bootable ISO image for the specified flavor and architecture."""

from __future__ import annotations

import logging

import click

import captain.flavor
from captain import artifacts
from captain.cli._main import CliContext, cli
from captain.cli.stages import _build_iso_stage

log = logging.getLogger(__name__)


@cli.command(
    "iso",
    short_help="Build ISO image only. Part of build.",
)
@click.option(
    "--iso-mode",
    envvar="ISO_MODE",
    default="docker",
    show_default=True,
    type=click.Choice(["docker", "native", "skip"], case_sensitive=False),
    metavar="MODE",
    help="ISO build stage execution mode (docker, native, skip).",
)
@click.option(
    "--force-iso",
    envvar="FORCE_ISO",
    is_flag=True,
    default=False,
    help="Force ISO rebuild even if outputs already exist.",
)
@click.pass_obj
def build_cmd(
    cli_ctx: CliContext,
    *,
    iso_mode: str,
    force_iso: bool,
) -> None:
    """Run the CaptainOS ISO build."""

    cfg = cli_ctx.make_config(
        iso_mode=iso_mode,
        force_iso=force_iso,
    )

    # Instantiate the flavor
    flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, cfg)
    flavor.generate(hash_only=True)  # Just populates cfg.flavor_hash so we can find the initramfs

    _build_iso_stage(cfg, flavor)
    artifacts.collect_iso(cfg)
    log.info("ISO build complete!!!")
