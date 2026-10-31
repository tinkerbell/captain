"""``captain build`` — full build pipeline (tools → initramfs → iso → artifacts)."""

from __future__ import annotations

import logging

import click

import captain.flavor
from captain import artifacts, cache
from captain.cli._main import CliContext, cli
from captain.cli.stages import (
    _build_iso_stage,
    _build_mkosi_stage,
    _build_tools_stage,
)

log = logging.getLogger(__name__)


@cli.command(
    "build",
    short_help="Run the full build pipeline via mkosi.",
)
@click.option(
    "--mkosi-mode",
    envvar="MKOSI_MODE",
    default="docker",
    show_default=True,
    type=click.Choice(["docker", "native", "skip"], case_sensitive=False),
    metavar="MODE",
    help="Mkosi stage execution mode (docker, native, skip).",
)
@click.option(
    "--tools-mode",
    envvar="TOOLS_MODE",
    default="native",
    show_default=True,
    type=click.Choice(["docker", "native", "skip"], case_sensitive=False),
    metavar="MODE",
    help="Tools download stage execution mode (docker, native, skip).",
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
    "--force",
    "force_mkosi",
    is_flag=True,
    default=False,
    help="Pass --force through to mkosi.",
)
@click.option(
    "--force-tools",
    envvar="FORCE_TOOLS",
    is_flag=True,
    default=False,
    help="Re-download tools even if outputs already exist.",
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
    mkosi_mode: str,
    tools_mode: str,
    iso_mode: str,
    force_mkosi: bool,
    force_tools: bool,
    force_iso: bool,
) -> None:
    """Run the full CaptainOS build pipeline.

    Stages executed in order: tools → initramfs (mkosi) → ISO → artifact
    collection.  Each stage can be independently set to run inside Docker,
    natively on the host, or skipped entirely.

    \b
    Examples
    --------
      captain build
      captain build --arch arm64
      captain build --flavor-id trixie-meson64 --arch arm64
      captain build --mkosi-mode native --tools-mode native
      captain build --force --force-tools
    """

    cfg = cli_ctx.make_config(
        tools_mode=tools_mode,
        mkosi_mode=mkosi_mode,
        iso_mode=iso_mode,
        force_tools=force_tools,
        force_iso=force_iso,
    )

    if force_mkosi:
        cfg.mkosi_args = ["--force"]

    # Instantiate and generate the flavor.
    flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, cfg)
    flavor.generate()
    cache.announce(cfg)

    if cfg.mkosi_mode == "skip":
        log.info("MKOSI_MODE=skip — skipping image assembly")
        _build_tools_stage(cfg)
        return

    # A cached mkosi output (initramfs or disk image) makes tools and kernel unnecessary.
    mkosi_key = cache.mkosi_key(cfg, flavor)
    if cache.restore(cfg, mkosi_key, cfg.initramfs_output, force=force_mkosi):
        log.info("mkosi output restored; skipping tools, kernel and mkosi stages.")
    else:
        cache.require_build(cfg, mkosi_key)

        # Tools stage.
        _build_tools_stage(cfg)

        # Delegate to flavor, for eg obtaining it's own dependencies.
        flavor.pre_mkosi_stage()

        # mkosi stage (initramfs or disk image, per the flavor's output).
        _build_mkosi_stage(cfg, list(cfg.mkosi_args))
        cache.save(cfg, mkosi_key, cfg.initramfs_output)

    flavor.output.collect_artifacts(flavor)
    log.info("mkosi build complete.")

    flavor.post_mkosi_stage()

    # ISO stage (if the flavor supports it).
    if flavor.has_iso():
        _build_iso_stage(cfg, flavor)
        artifacts.collect_iso(cfg)
    else:
        log.info("Flavor '%s' does not produce an ISO — skipping.", flavor.id)

    # Flavor-driven artifact collection.
    flavor.post_artifact_collect()

    log.info("Build complete!")
