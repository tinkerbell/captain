"""``captain cache`` — inspect the build cache: detected scope, keys, hits."""

from __future__ import annotations

import logging

import click
from rich.table import Table

import captain
import captain.flavor
from captain import cache
from captain.cli._main import CliContext, cli
from captain.traits.platform import AcpiCaptainKernel

log = logging.getLogger(__name__)


@cli.group("cache", short_help="Inspect the build cache (OCI registries).")
def cache_group() -> None:
    """Inspect the content-hashed build cache. See docs/build-caching.md."""


@cache_group.command("info", short_help="Show cache scope, and keys/hits for the flavor.")
@click.option("--github-output", is_flag=True, help="Print name=value lines for $GITHUB_OUTPUT.")
@click.option("--check/--no-check", default=True, help="Look the keys up in the registries.")
@click.pass_obj
def info_cmd(cli_ctx: CliContext, *, github_output: bool, check: bool) -> None:
    """Show where the cache is read from / written to, and the keys of every piece
    of the current flavor/arch, with the location holding each (if any).

    \b
    Examples
    --------
      captain cache info
      captain --flavor-id trixie-slim --arch arm64 cache info
      captain cache info --github-output >> "$GITHUB_OUTPUT"
    """
    cfg = cli_ctx.make_config()
    scope = cache.scope(cfg)
    if github_output:
        click.echo(f"cache_push={'true' if scope.write is not None else 'false'}")
        return

    flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, cfg)
    flavor.generate(hash_only=True)
    keys = [cache.tools_key(cfg)]
    if isinstance(flavor.platform, AcpiCaptainKernel):
        keys.append(cache.kernel_key(cfg))
    keys.append(cache.mkosi_key(cfg, flavor))
    if flavor.has_iso():
        keys.append(cache.iso_key(cfg, flavor))

    table = Table(title=f"Cache keys: {flavor.id} ({cfg.arch})", header_style="bold magenta")
    table.add_column("kind")
    table.add_column("tag")
    table.add_column("found in")
    for key in keys:
        found = "-"
        if check:
            location = cache.lookup(cfg, key)
            found = str(location) if location else "[red]miss[/red]"
        table.add_row(key.kind, key.tag, found)
    captain.console.print(table)
    for key in keys:
        log.debug("%s inputs: %s", key, key.canonical)
