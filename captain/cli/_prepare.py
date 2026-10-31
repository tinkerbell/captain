"""``captain prepare-versions`` — resolve dynamic version tokens for CI.

Fetches the latest kernel.org point release of the configured branch and the
armbian-next repo Release ETag, then emits them as ``name=value`` lines on
stdout (logging goes to stderr) suitable for appending to ``$GITHUB_OUTPUT``:

    kernel_version=6.18.40
    armbian_version=<token-or-empty>
"""

from __future__ import annotations

import logging

import click

from captain import armbian, versions
from captain.cli._main import cli

log = logging.getLogger(__name__)


@cli.command(
    "prepare-versions",
    short_help="Emit latest kernel/armbian version tokens as name=value (for $GITHUB_OUTPUT).",
)
def prepare_versions_cmd() -> None:
    """Resolve dynamic version tokens and print them to stdout."""
    kernel_version = versions.latest_branch_version()
    armbian_version = armbian.release_etag()

    click.echo(f"kernel_version={kernel_version}")
    click.echo(f"armbian_version={armbian_version}")
