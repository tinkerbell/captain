"""``captain list-flavors`` — the flavor registry, for humans and for CI.

``--github-output`` emits ``name=value`` lines for ``$GITHUB_OUTPUT``: the build matrix
and the multi-arch "combined" publish matrix.
``--release`` emits one ``<flavor-id> <target> <arch> <tag-suffix>`` line per flavor, for the
release workflow (``while read -r id target arch suffix``); the tag suffix may be empty.
Both follow ``custom/builds.txt``, which selects the flavors CI builds (empty: all of them).
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path

import click
from rich.table import Table

import captain
from captain.cli._main import CliContext, cli
from captain.flavor import Flavor
from captain.flavors import FLAVORS
from captain.util import get_arch_info

log = logging.getLogger(__name__)


def _is_combined(flavor: Flavor) -> bool:
    """Published as one multi-arch image, built from both arches' artifacts (see ci.yml)."""
    return flavor.supported_architectures == {"amd64", "arm64"}


def _selected(project_dir: Path) -> list[Flavor]:
    """The flavors custom/builds.txt selects for CI: ids or globs, one per line; none means all."""
    builds_file = project_dir / "custom" / "builds.txt"
    patterns = []
    if builds_file.exists():
        patterns = [
            line.strip()
            for line in builds_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if not patterns:
        return list(FLAVORS)
    for pattern in patterns:
        if not any(fnmatch.fnmatchcase(f.id, pattern) for f in FLAVORS):
            log.error(
                "custom/builds.txt: '%s' matches no flavor (see `captain list-flavors`)", pattern
            )
            raise SystemExit(1)
    selected = [f for f in FLAVORS if any(fnmatch.fnmatchcase(f.id, p) for p in patterns)]
    log.info("custom/builds.txt selects %d of %d flavors", len(selected), len(FLAVORS))
    return selected


def _matrix_entries(flavor: Flavor) -> list[dict[str, str | bool]]:
    return [
        {
            "arch": arch,
            "output_arch": get_arch_info(arch).output_arch,
            "iso": flavor.has_iso(),
            "FLAVOR_ID": flavor.id,
        }
        for arch in sorted(flavor.supported_architectures)
    ]


@cli.command("list-flavors", short_help="List flavors (table, JSON, or GitHub Actions matrices).")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON object per flavor.")
@click.option("--github-output", is_flag=True, help="Print name=value matrices for $GITHUB_OUTPUT.")
@click.option("--release", is_flag=True, help="Print '<id> <target> <arch> <tag-suffix>' lines.")
@click.pass_obj
def list_flavors_cmd(
    cli_ctx: CliContext, *, as_json: bool, github_output: bool, release: bool
) -> None:
    """List the flavors in the registry (captain/flavors.py); CI builds those custom/builds.txt
    selects (all, when it lists none)."""
    selected = _selected(cli_ctx.project_dir)
    selected_ids = {f.id for f in selected}
    if github_output:
        entries = [e for f in selected for e in _matrix_entries(f)]
        combined = [
            {"combined_flavor": f.id, "iso": f.has_iso()} for f in selected if _is_combined(f)
        ]
        click.echo(f"matrix={json.dumps({'include': entries})}")
        click.echo(f"matrix_combined={json.dumps({'include': combined})}")
        return

    if release:
        for f in selected:
            target = "combined" if _is_combined(f) else sorted(f.supported_architectures)[0]
            arch = "amd64" if target == "combined" else target
            click.echo(f"{f.id} {target} {arch} {f.tag_suffix}".rstrip())
        return

    if as_json:
        for f in FLAVORS:
            entry = {
                "id": f.id,
                "description": f.description,
                "arches": sorted(f.supported_architectures),
                "distro": f.distro.name,
                "platform": f.platform.name,
                "role": f.role.name,
                "output": f.output.name,
                "iso": f.has_iso(),
                "combined": _is_combined(f),
                "tag_suffix": f.tag_suffix,
                "ci": f.id in selected_ids,
            }
            click.echo(json.dumps(entry))
        return

    narrowed = len(selected) < len(FLAVORS)
    table = Table(title="Flavors", show_header=True, header_style="bold magenta")
    for column in ("id", "arches", "distro", "platform", "role", "output", "iso"):
        table.add_column(column)
    if narrowed:
        table.add_column("ci")
    for f in FLAVORS:
        row = [
            f.id,
            ",".join(sorted(f.supported_architectures)),
            f.distro.name,
            f.platform.name,
            f.role.name,
            f.output.name,
            "yes" if f.has_iso() else "no",
        ]
        if narrowed:
            row.append("yes" if f.id in selected_ids else "no")
        table.add_row(*row)
    captain.console.print(table)
