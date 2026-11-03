"""Click CLI — main group with shared options and subcommand registration."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from trogon import tui

from captain import versions
from captain.config import (
    DEFAULT_FLAVOR_ID,
    DEFAULT_KERNEL_VERSION,
    DEFAULT_KUBERNETES_VERSION,
    VALID_CACHE_MODES,
    Config,
)
from captain.flavor import list_available_flavors
from captain.util import detect_current_machine_arch

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI context object — shared state for all subcommands
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CliContext:
    """Resolved common CLI options, passed to subcommands via ``@click.pass_obj``."""

    project_dir: Path
    arch: str
    flavor_id: str
    builder_registry: str | None
    builder_repository: str | None
    builder_image: str
    verbose_docker: bool
    cache_mode: str
    cache_push: bool
    cache_only: bool
    kernel_version: str | None  # None: resolve (see _resolve_versions)
    armbian_version: str | None  # None: resolve; "" means no cache-bust
    kubernetes_version: str

    def _resolve_versions(self) -> None:
        """Resolve unset version tokens like CI's prepare job does, so local cache keys match.

        Only on a local host: in CI the prepare job passes them explicitly, and the
        Docker-relaunched process gets the host's (Click reads an empty env var as
        unset, so there None means "").
        """
        local = (
            not os.environ.get("CAPTAIN_IN_DOCKER") and os.environ.get("GITHUB_ACTIONS") != "true"
        )
        if self.kernel_version is None:
            self.kernel_version = (
                versions.kernel_version(self.project_dir) if local else DEFAULT_KERNEL_VERSION
            )
        if self.armbian_version is None:
            self.armbian_version = versions.armbian_version(self.project_dir) if local else ""

    def make_config(self, **overrides: Any) -> Config:
        """Build a :class:`Config` from the common options plus per-command *overrides*."""
        self._resolve_versions()
        assert self.kernel_version is not None
        return Config(
            project_dir=self.project_dir,
            output_dir=self.project_dir / "out",
            arch=self.arch,
            flavor_id=self.flavor_id,
            builder_registry=self.builder_registry,
            builder_repository=self.builder_repository,
            builder_image=self.builder_image,
            verbose_docker=self.verbose_docker,
            cache_mode=self.cache_mode,
            cache_push=self.cache_push,
            cache_only=self.cache_only,
            kernel_version=self.kernel_version,
            armbian_version=self.armbian_version,
            kubernetes_version=self.kubernetes_version,
            **overrides,
        )


# ---------------------------------------------------------------------------
# Resolve project directory
# ---------------------------------------------------------------------------


def resolve_project_dir(project_dir: str | None) -> Path:
    """Return an absolute ``Path`` for the project root."""
    if project_dir is not None:
        return Path(project_dir)
    # Walk upward from this file until we find pyproject.toml.
    candidate = Path(__file__).resolve().parent.parent.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    click.echo("Error: cannot auto-detect project directory. Pass --project-dir.", err=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Top-level Click group
# ---------------------------------------------------------------------------
# Important: decorator order matters here.  The @tui() decorator must be outermost
#            to properly wrap the entire CLI, including subcommands.

CONTEXT_SETTINGS = dict(
    help_option_names=["-h", "--help"],
    max_content_width=120,
)


@tui()
@click.group(
    context_settings=CONTEXT_SETTINGS,
    invoke_without_command=True,
    help=(
        "CaptainOS build system.\n\n"
        "Run 'captain COMMAND --help' for details on each subcommand.\n\n"
        "Shell completion (bash/zsh):\n\n"
        '  eval "$(_CAPTAIN_COMPLETE=bash_source captain)"   # bash\n\n'
        '  eval "$(_CAPTAIN_COMPLETE=zsh_source captain)"    # zsh'
    ),
)
@click.version_option(package_name="captain")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    default=False,
    envvar="CAPTAIN_VERBOSE",
    help="Enable verbose (DEBUG-level) logging.",
)
@click.option(
    "--arch",
    envvar="ARCH",
    default=(detect_current_machine_arch()),
    show_default=True,
    type=click.Choice(["amd64", "arm64"], case_sensitive=False),
    metavar="ARCH",
    help="Target architecture (amd64, arm64).",
)
@click.option(
    "--flavor-id",
    "--flavor",
    "flavor_id",
    envvar="FLAVOR_ID",
    default=DEFAULT_FLAVOR_ID,
    show_default=True,
    type=click.Choice(list_available_flavors(), case_sensitive=False),
    help="Flavor (kernel/board config) to build.",
)
@click.option(
    "--project-dir",
    envvar="CAPTAIN_PROJECT_DIR",
    default=None,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Project root directory (auto-detected when omitted).",
)
@click.option(
    "--builder-registry",
    envvar="REGISTRY",
    default="ghcr.io",
    show_default=True,
    help="OCI registry hostname for the Docker builder image",
)
@click.option(
    "--builder-repository",
    envvar="GITHUB_REPOSITORY",
    default="tinkerbell/captain",
    show_default=True,
    help="Repository path (owner/name) for the Docker builder image",
)
@click.option(
    "--builder-image",
    envvar="BUILDER_IMAGE",
    default="captainos-builder",
    show_default=True,
    help="Local name/tag of Docker builder image name",
)
@click.option(
    "--cache",
    "cache_mode",
    envvar="CAPTAIN_CACHE",
    default="on",
    show_default=True,
    type=click.Choice(VALID_CACHE_MODES, case_sensitive=False),
    help=(
        "Remote build cache: 'on' (read; push where allowed), 'read' (never push), "
        "'off' (env: CAPTAIN_CACHE). See docs/build-caching.md."
    ),
)
@click.option(
    "--cache-push",
    envvar="CAPTAIN_CACHE_PUSH",
    is_flag=True,
    default=False,
    help="Local builds: push what was built to the downstream cache (env: CAPTAIN_CACHE_PUSH).",
)
@click.option(
    "--cache-only",
    envvar="CAPTAIN_CACHE_ONLY",
    is_flag=True,
    default=False,
    help="Fail instead of building anything that is not cached (env: CAPTAIN_CACHE_ONLY).",
)
@click.option(
    "--kernel-version",
    envvar="KERNEL_VERSION",
    default=None,
    help=(
        "Kernel version to build/reference. Must match an official tarball (env: KERNEL_VERSION). "
        "Default: the latest kernel.org release of the branch, cached 6h "
        f"(else {DEFAULT_KERNEL_VERSION})."
    ),
)
@click.option(
    "--armbian-version",
    envvar="ARMBIAN_VERSION",
    default=None,
    help=(
        "Armbian repo version token folded into armbian flavors' hash so they rebuild "
        "when armbian-next updates (env: ARMBIAN_VERSION). Default: resolved from the "
        "armbian-next repo, cached 6h."
    ),
)
@click.option(
    "--kubernetes-version",
    envvar="KUBERNETES_VERSION",
    default=DEFAULT_KUBERNETES_VERSION,
    show_default=True,
    help="Kubernetes minor version (pkgs.k8s.io) for k8s-role flavors (env: KUBERNETES_VERSION).",
)
@click.pass_context
def cli(
    ctx: click.Context,
    *,
    verbose: bool,
    arch: str,
    flavor_id: str,
    project_dir: str | None,
    builder_registry: str | None,
    builder_repository: str | None,
    builder_image: str,
    cache_mode: str,
    cache_push: bool,
    cache_only: bool,
    kernel_version: str | None,
    armbian_version: str | None,
    kubernetes_version: str,
) -> None:
    """CaptainOS build system — click CLI."""
    # Configure log level based on --verbose.
    # Configure the root logger (configged via basicLogging() in captain __init__)
    # since subcommands and imported modules will use it.
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger().setLevel(log_level)
    logging.getLogger("captain").setLevel(log_level)

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        return

    # Build the shared context object for subcommands.
    log.debug("Main CLI creating CliContext...")
    ctx.obj = CliContext(
        project_dir=resolve_project_dir(project_dir),
        arch=arch,
        flavor_id=flavor_id,
        builder_registry=builder_registry,
        builder_repository=builder_repository,
        builder_image=builder_image,
        verbose_docker=verbose,
        cache_mode=cache_mode.lower(),
        cache_push=cache_push,
        cache_only=cache_only,
        kernel_version=kernel_version,
        armbian_version=armbian_version,
        kubernetes_version=kubernetes_version,
    )


# ---------------------------------------------------------------------------
# Register subcommands (imported lazily to avoid circular imports)
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point."""
    # Import subcommand modules to register them on the group.
    from captain.cli import (  # noqa: F401
        _build,
        _builder,
        _cache,
        _flavors,
        _iso,
        _kernel,
        _prepare,
        _qemu,
        _release,
        _shell,
        _tools,
    )

    cli()
