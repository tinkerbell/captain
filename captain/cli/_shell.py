"""``captain tools`` — download tools (containerd, runc, nerdctl, CNI plugins)."""

from __future__ import annotations

import logging

import click

import captain.docker as docker
from captain.builder import obtain_builder
from captain.cli._main import CliContext, cli

log = logging.getLogger(__name__)


@cli.command(
    "shell",
    short_help="Get a shell in the builder environment.",
)
@click.pass_obj
def shell_cmd(cli_ctx: CliContext) -> None:
    log.debug("Starting shell in docker...")

    cfg = cli_ctx.make_config()

    obtain_builder(cfg)
    docker.run_in_builder(cfg, command_and_args=["bash"], extra_docker_args=["-i", "-t"])

    log.info("Shell stage complete!")
