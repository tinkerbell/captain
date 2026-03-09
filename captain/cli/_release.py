"""Release subcommand — publish, pull, tag."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import configargparse

from captain import docker, oci
from captain.config import Config
from captain.log import for_stage
from captain.util import check_release_dependencies

from ._parser import (
    _add_common_flags,
    _add_kernel_flags,
    _add_release_base_flags,
    _add_release_pull_output,
    _add_release_tag_version,
    _add_release_target_flag,
    _HelpFormatter,
)

_RELEASE_SUBCOMMANDS = ("publish", "pull", "tag")

_RELEASE_SUBCMD_INFO: dict[str, tuple[str, list]] = {
    "publish": (
        "Publish artifacts as a multi-arch OCI image",
        [_add_common_flags, _add_kernel_flags, _add_release_base_flags, _add_release_target_flag],
    ),
    "pull": (
        "Pull and extract artifacts (amd64, arm64, or combined)",
        [
            _add_common_flags,
            _add_kernel_flags,
            _add_release_base_flags,
            _add_release_target_flag,
            _add_release_pull_output,
        ],
    ),
    "tag": (
        "Tag all artifact images with a version",
        [_add_common_flags, _add_kernel_flags, _add_release_base_flags, _add_release_tag_version],
    ),
}


def _print_release_subcmd_help(sub: str, *, exit_code: int = 0) -> None:
    """Print help for a release subcommand and exit."""
    desc, adders = _RELEASE_SUBCMD_INFO[sub]
    columns = shutil.get_terminal_size().columns
    parser = configargparse.ArgParser(
        prog=f"build.py release {sub}",
        description=desc,
        add_env_var_help=False,
        formatter_class=lambda prog: _HelpFormatter(
            prog,
            max_help_position=38,
            width=columns,
        ),
    )
    for adder in adders:
        adder(parser)
    parser.print_help()
    raise SystemExit(exit_code)


def _resolve_git_sha(args: object, project_dir: Path) -> str:
    """Return the git SHA from args or by running git rev-parse."""
    sha = getattr(args, "git_sha", None)
    if sha:
        return sha

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=project_dir,
    )
    return result.stdout.strip()


def _cmd_release(cfg: Config, extra_args: list[str], args: object = None) -> None:
    """OCI artifact operations: publish, pull, tag."""
    rlog = for_stage("release")

    # Peel the release subcommand from extra_args.
    if not extra_args:
        rlog.err(
            f"Missing release subcommand.\n"
            f"  usage: build.py release {{{','.join(_RELEASE_SUBCOMMANDS)}}}\n"
        )
        raise SystemExit(2)

    sub = extra_args[0]
    rest = extra_args[1:]

    if sub not in _RELEASE_SUBCOMMANDS:
        rlog.err(
            f"Unknown release subcommand '{sub}'.\n  valid: {', '.join(_RELEASE_SUBCOMMANDS)}\n"
        )
        raise SystemExit(2)

    # Handle --help / -h for the subcommand.
    if "-h" in rest or "--help" in rest:
        _print_release_subcmd_help(sub)

    # --- validate required args early ---------------------------------
    if sub == "tag" and not rest:
        rlog.err("Missing version argument.")
        _print_release_subcmd_help(sub, exit_code=2)
    if sub == "pull" and not getattr(args, "pull_output", None):
        rlog.err("--pull-output is required for 'release pull'.")
        _print_release_subcmd_help(sub, exit_code=2)

    # --- skip ---------------------------------------------------------
    if cfg.release_mode == "skip":
        rlog.log("RELEASE_MODE=skip — skipping release operation")
        return

    # --- docker -------------------------------------------------------
    if cfg.release_mode == "docker":
        docker.build_release_image(cfg, logger=rlog)
        rlog.log(f"Running release {sub} (docker)...")
        # Forward release-specific env vars into the container.
        registry = getattr(args, "registry", "ghcr.io")
        repository = getattr(args, "repository", "tinkerbell/captain")
        artifact_name = getattr(args, "oci_artifact_name", "artifacts")
        sha = _resolve_git_sha(args, cfg.project_dir)
        env_args: list[str] = [
            "-e",
            f"KERNEL_VERSION={cfg.kernel_version}",
            "-e",
            f"REGISTRY={registry}",
            "-e",
            f"GITHUB_REPOSITORY={repository}",
            "-e",
            f"OCI_ARTIFACT_NAME={artifact_name}",
            "-e",
            f"GITHUB_SHA={sha}",
        ]
        exclude = getattr(args, "version_exclude", None)
        if exclude:
            env_args += ["-e", f"VERSION_EXCLUDE={exclude}"]
        if sub in ("publish", "pull"):
            target = getattr(args, "target", None) or cfg.arch
            env_args += ["-e", f"TARGET={target}"]
        if getattr(args, "force", False):
            env_args += ["-e", "FORCE=true"]
        pull_output = getattr(args, "pull_output", None)

        # Build the inner command.
        inner_cmd = ["/work/build.py", "release", sub]
        if pull_output:
            inner_cmd += ["--pull-output", pull_output]
        inner_cmd += list(rest)

        try:
            docker.run_in_release(
                cfg,
                *env_args,
                "--entrypoint",
                "python3",
                docker.RELEASE_IMAGE,
                *inner_cmd,
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(exc.returncode) from None
        paths_to_fix = ["/work/out"]
        if pull_output:
            container_pull_output = f"/work/{pull_output.lstrip('/')}"
            paths_to_fix.append(container_pull_output)
        docker.fix_docker_ownership(cfg, rlog, paths_to_fix)
        return

    # --- native -------------------------------------------------------
    if cfg.release_mode == "native":
        missing = check_release_dependencies()
        if missing:
            rlog.err(f"Missing release tools: {', '.join(missing)}")
            rlog.err("Install them or set --release-mode=docker.")
            raise SystemExit(1)
    # Common OCI parameters.
    registry = getattr(args, "registry", "ghcr.io")
    repository = getattr(args, "repository", "tinkerbell/captain")
    artifact_name = getattr(args, "oci_artifact_name", "artifacts")
    exclude = getattr(args, "version_exclude", None)
    sha = _resolve_git_sha(args, cfg.project_dir)
    tag = oci.compute_version_tag(cfg.project_dir, sha, exclude=exclude)

    if sub == "publish":
        target = getattr(args, "target", None) or cfg.arch
        force = getattr(args, "force", False)
        oci.publish(
            cfg,
            target=target,
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            tag=tag,
            sha=sha,
            force=force,
            logger=rlog,
        )

    elif sub == "pull":
        target = getattr(args, "target", None) or cfg.arch
        pull_output = getattr(args, "pull_output", None)
        if pull_output is None:
            rlog.err("--pull-output is required for 'release pull'.")
            raise SystemExit(2)
        oci.pull(
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            tag=tag,
            target=target,
            output_dir=Path(pull_output),
            logger=rlog,
        )

    elif sub == "tag":
        version = rest[0]
        oci.tag_all(
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            src_tag=tag,
            new_tag=version,
            logger=rlog,
        )
