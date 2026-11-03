"""``captain release`` — publish, pull and tag artifacts as OCI images (via buildah/skopeo)."""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
from pathlib import Path

import click

import captain.flavor
from captain import cache, oci
from captain.cache._registry import Registry
from captain.cli._main import CliContext, cli
from captain.config import Config
from captain.util import check_release_dependencies

log = logging.getLogger(__name__)


@cli.group()
@click.option(
    "--release-mode",
    envvar="RELEASE_MODE",
    default="docker",
    show_default=True,
    type=click.Choice(["docker", "native", "skip"], case_sensitive=False),
    metavar="MODE",
    help="Release stage execution mode (docker, native, skip).",
)
@click.option(
    "--registry",
    envvar="REGISTRY",
    default="ghcr.io",
    show_default=True,
    help="OCI registry hostname.",
)
@click.option(
    "--repository",
    envvar="GITHUB_REPOSITORY",
    default="tinkerbell/captain",
    show_default=True,
    help="Repository path (owner/name).",
)
@click.option(
    "--oci-artifact-name",
    envvar="OCI_ARTIFACT_NAME",
    default="artifacts",
    show_default=True,
    help="OCI artifact image name.",
)
@click.option(
    "--target",
    envvar="TARGET",
    default=None,
    type=click.Choice(["amd64", "arm64", "combined"], case_sensitive=False),
    metavar="TARGET",
    help="Artifact target: 'amd64', 'arm64', or 'combined' (default: value of --arch); "
    "'combined' requires an ACPI flavor with both arch's outputs present.",
)
@click.option(
    "--git-sha",
    envvar="GIT_SHA",
    default=None,
    help="Git commit SHA (default: auto-detected via git rev-parse HEAD).",
)
@click.option(
    "--force-release",
    envvar="FORCE_RELEASE",
    is_flag=True,
    default=False,
    help="Publish even if the image already exists in the registry.",
)
@click.option(
    "--src-tag",
    envvar="SRC_TAG",
    default=None,
    help="Override specific tag to use; auto-determined if omitted.",
)
@click.pass_context
def release_group(
    ctx: click.Context,
    *,
    release_mode: str,
    registry: str,
    repository: str,
    oci_artifact_name: str,
    target: str | None,
    git_sha: str | None,
    force_release: bool,
    src_tag: str | None,
) -> None:
    """Release-related operations: publish, pull, tag. Operates on OCI images in a registry."""
    cli_ctx: CliContext = ctx.obj

    if target is None:
        target = cli_ctx.arch
    assert isinstance(target, str)

    log.debug("Creating Release group Config and putting into Context...")

    cfg = cli_ctx.make_config(
        release_mode=release_mode,
        release_registry=registry,
        release_repository=repository,
        release_oci_artifact_name=oci_artifact_name,
        release_target=target,
        release_git_sha=git_sha,
        force_release=force_release,
        release_src_tag=src_tag,
    )

    # resolve the SHA before launching Docker, as we've the .git here and not there.
    if cfg.release_git_sha is None:
        log.debug(
            "Auto-detecting git SHA for release since not provided via --git-sha or GITHUB_SHA..."
        )
        cfg.release_git_sha = _autodetect_git_sha(cfg.project_dir)
    log.debug("Git SHA at group level: %s", cfg.release_git_sha)

    if cfg.release_src_tag is None:
        log.debug(
            "Auto-computing version tag for release since not provided via --src-tag or SRC_TAG..."
        )
        tag = f"v0.0.0-{str(cfg.release_git_sha)[:7]}"
        cfg.release_src_tag = f"{tag}-{cfg.flavor_id}"
    log.debug("Tag at group level: %s", cfg.release_src_tag)

    # pass it down via Context // pass_obj
    ctx.obj = cfg


@release_group.command("pull")
@click.option(
    "--pull-output",
    envvar="PULL_OUTPUT",
    required=True,
    type=click.Path(),
    help="Output directory for pulled artifacts, relative to 'out/'.",
)
@click.pass_obj
def pull_command(cfg: Config, pull_output: Path) -> None:
    """Pull release artifacts as OCI images from a registry and extract to disk."""
    log.debug("Release Pull with Config %s and pull_output %s", cfg, pull_output)

    cfg.release_pull_output = pull_output

    if skip_or_relaunch_in_docker(cfg, "pull"):
        return

    oci.pull(
        registry=str(cfg.release_registry),
        repository=str(cfg.release_repository),
        artifact_name=str(cfg.release_oci_artifact_name),
        tag=str(cfg.release_src_tag),
        target=str(cfg.release_target),
        output_dir=cfg.output_dir / cfg.release_pull_output,
    )


@release_group.command("tag")
@click.option(
    "--new-tag",
    envvar="NEW_TAG",
    required=True,
    help="New tag to apply to the existing release image (e.g. v1.0.0).",
)
@click.pass_obj
def tag_command(cfg: Config, new_tag: str) -> None:
    """Tag an existing release image with a new version.

    A manifest re-tag within the repository, done by captain's own OCI client:
    no Docker (or builder image) needed, whatever the release mode.

    *new_tag* is the version plus the flavor's release suffix (``list-flavors
    --release``); the image is also tagged ``<version>-<flavor id>``.
    """
    log.debug("Release tag with Config: %s and new_tag %s", cfg, new_tag)

    cfg.release_new_tag = new_tag

    if cfg.release_mode == "skip":
        log.info("RELEASE_MODE=skip — nothing to do.")
        return

    flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, cfg)

    # *new_tag* carries the flavor's release suffix (eg. "-rpi"). Also tag the image with
    # the flavor id instead, the naming of the source tags (v0.0.0-<sha7>-<flavor id>), so
    # consumers switching between per-commit and version tags only change the version.
    suffix = flavor.tag_suffix
    version = new_tag.removesuffix(suffix) if suffix else new_tag
    for tag in dict.fromkeys([new_tag, f"{version}-{flavor.id}"]):
        oci.tag_all(
            registry=str(cfg.release_registry),
            repository=str(cfg.release_repository),
            artifact_name=str(cfg.release_oci_artifact_name),
            src_tag=str(cfg.release_src_tag),
            new_tag=tag,
            arches=list(flavor.supported_architectures),
        )


@release_group.command("status")
@click.option("--github-output", is_flag=True, help="Print name=value lines for $GITHUB_OUTPUT.")
@click.pass_obj
def status_command(cfg: Config, github_output: bool) -> None:
    """Whether the release image of these exact outputs is already published.

    Needs no build outputs: the image's content tag is computed from the build-cache keys.
    """
    content_tag, _ = _content_tags(cfg)
    published = _registry(cfg).manifest_exists(_repo(cfg), content_tag)
    if github_output:
        click.echo(f"published={'true' if published else 'false'}")
        click.echo(f"content_tag={content_tag}")
        return
    log.info("%s:%s is %s", _repo(cfg), content_tag, "published" if published else "not published")


@release_group.command("publish")
@click.pass_obj
def publish_command(cfg: Config) -> None:
    """Publish build artifacts as a multi-arch OCI image to a registry.

    Uses buildah to construct OCI images from the build artifacts (kernel,
    initramfs, ISO, DTBs) and pushes them to the specified registry.

    Each artifact file becomes its own layer.  Deterministic tar generation
    ensures byte-identical layers across runs so that registries can
    deduplicate blobs.

    Images are published under a content tag (from the build-cache keys of
    what they contain), so unchanged outputs are never published twice, and
    then tagged with the source tag (``v0.0.0-<sha7>-<flavor>[-<arch>]``).
    """
    log.debug("Got into release publish with Config: %s", cfg)

    if cfg.release_mode == "skip":
        log.info("RELEASE_MODE=skip — nothing to do.")
        return

    content_tag, arch_tags = _content_tags(cfg)

    if os.environ.get("CAPTAIN_IN_DOCKER"):
        # Relaunched by the host below, which then adds the source tag.
        _publish(cfg, content_tag, arch_tags)
        return

    if not cfg.force_release and _registry(cfg).manifest_exists(_repo(cfg), content_tag):
        log.info("%s:%s already published; only tagging it.", _repo(cfg), content_tag)
    elif not skip_or_relaunch_in_docker(cfg, "publish"):
        _publish(cfg, content_tag, arch_tags)

    suffix = "" if cfg.release_target == "combined" else f"-{cfg.release_target}"
    source_tag = f"{cfg.release_src_tag}{suffix}"
    _registry(cfg).retag(_repo(cfg), content_tag, source_tag)
    log.info("Tagged %s:%s → %s", _repo(cfg), content_tag, source_tag)


def _publish(cfg: Config, content_tag: str, arch_tags: dict[str, str]) -> None:
    flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, cfg)
    oci.publish(
        cfg,
        flavor,
        target=str(cfg.release_target),
        registry=str(cfg.release_registry),
        repository=str(cfg.release_repository),
        artifact_name=str(cfg.release_oci_artifact_name),
        tag=content_tag,
        arch_tags=arch_tags,
        force=cfg.force_release,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def skip_or_relaunch_in_docker(cfg: Config, subcommand: str) -> bool:
    # --- skip mode --------------------------------------------------------
    if cfg.release_mode == "skip":
        log.info("RELEASE_MODE=skip — nothing to do.")
        return True

    # --- docker mode ------------------------------------------------------
    if cfg.release_mode == "docker":
        from captain import builder, docker

        builder.obtain_builder(cfg)
        docker.run_captain_in_builder(cfg, ["release", subcommand])
        docker.fix_docker_ownership(cfg, ["/work/out"])
        return True

    # --- native mode ------------------------------------------------------
    missing = check_release_dependencies()
    if missing:
        log.error("Missing release tools: %s", ", ".join(missing))
        log.error("Install them or set --release-mode=docker.")
        raise SystemExit(1)

    return False


def _repo(cfg: Config) -> str:
    return f"{cfg.release_repository}/{cfg.release_oci_artifact_name}"


def _registry(cfg: Config) -> Registry:
    return Registry(str(cfg.release_registry))


def _content_tags(cfg: Config) -> tuple[str, dict[str, str]]:
    """Content tag of the release image for *cfg*'s target, and those of its arches."""
    combined = cfg.release_target == "combined"
    arches = ["amd64", "arm64"] if combined else [str(cfg.release_target)]
    arch_keys: dict[str, cache.CacheKey] = {}
    for arch in arches:
        arch_cfg = dataclasses.replace(cfg, arch=arch)
        flavor = captain.flavor.create_and_setup_flavor_for_id(cfg.flavor_id, arch_cfg)
        flavor.generate(hash_only=True)
        arch_keys[arch] = cache.release_key(arch_cfg, flavor)
    arch_tags = {arch: f"c-{key.tag}" for arch, key in arch_keys.items()}
    if combined:
        return f"c-{cache.combined_release_key(cfg.flavor_id, arch_keys).tag}", arch_tags
    return arch_tags[arches[0]], arch_tags


def _autodetect_git_sha(project_dir: Path) -> str:
    """Auto-detect via ``git rev-parse HEAD``."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=project_dir,
    ).stdout.strip()
