"""Running captain and mkosi inside the Docker builder container.

The builder image itself is managed by :mod:`captain.builder`. This module's
hash is part of the mkosi build-cache key, so keep unrelated code out of it.
"""

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from subprocess import CalledProcessError

from rich.table import Table

import captain
from captain.config import Config
from captain.kernel import KERNEL_BUILD_BASE_PATH
from captain.util import run

log = logging.getLogger(__name__)


def run_in_builder(
    cfg: Config, command_and_args: list[str], extra_docker_args: list[str] | None = None
) -> None:
    """Run a command inside the Docker builder container.

    *extra_args* are appended after the docker run flags and image name.
    """

    docker_envs: dict[str, str] = {
        # common to all commands
        "ARCH": cfg.arch,
        "FLAVOR_ID": cfg.flavor_id,
        "KERNEL_VERSION": cfg.kernel_version,
        # folded into flavor hashes; must match for in-container stages (iso, release)
        "ARMBIAN_VERSION": cfg.armbian_version or "",
        "KUBERNETES_VERSION": cfg.kubernetes_version,
        "FORCE_TOOLS": str(int(cfg.force_tools)),
        "FORCE_ISO": str(int(cfg.force_iso)),
        "FORCE_KERNEL": f"{int(cfg.force_kernel)!s}",
        "FORCE_RELEASE": str(cfg.force_release),
        "CAPTAIN_VERBOSE": "1" if cfg.verbose_docker else "0",
        "CONFIG_KERNEL": "1" if cfg.kernel_menuconfig else "0",
        # publish-related env vars
        "BUILDAH_ISOLATION": "chroot",
        "REGISTRY_INSECURE": os.environ.get("REGISTRY_INSECURE", ""),
        "REGISTRY": str(cfg.release_registry),
        "GITHUB_REPOSITORY": str(cfg.release_repository),
        "OCI_ARTIFACT_NAME": str(cfg.release_oci_artifact_name),
        "TARGET": str(cfg.release_target),
        "GIT_SHA": str(cfg.release_git_sha),
        "SRC_TAG": str(cfg.release_src_tag),
        "PULL_OUTPUT": str(cfg.release_pull_output),
        "NEW_TAG": str(cfg.release_new_tag),
        # set all modes to native under Docker
        "KERNEL_MODE": "native",
        "RELEASE_MODE": "native",
        "TOOLS_MODE": "native",
        "MKOSI_MODE": "native",
        "ISO_MODE": "native",
        # terminal control / gha stuff etc
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "FORCE_COLOR": "1",
        "COLUMNS": str(captain.env_columns),
        "GITHUB_ACTIONS": os.environ.get("GITHUB_ACTIONS", ""),
        "CAPTAIN_IN_DOCKER": "docker",
        # Forward host registry credentials so buildah/skopeo can authenticate.
        # The caller sets these env vars on the host (e.g. via docker login or
        # CI secrets); they are passed through to the container as-is.
        "REGISTRY_AUTH_FILE": os.environ.get("REGISTRY_AUTH_FILE", ""),
        "REGISTRY_USERNAME": os.environ.get("REGISTRY_USERNAME", ""),
        "REGISTRY_PASSWORD": os.environ.get("REGISTRY_PASSWORD", ""),
    }

    docker_args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--privileged",  # yes, this is required for both buildah and mkosi in the container
        "-w",
        "/work",
    ]

    if extra_docker_args is not None:
        log.debug("Adding extra Docker args: %s", extra_docker_args)
        docker_args.extend(extra_docker_args)

    if log.isEnabledFor(logging.DEBUG):
        table = Table(
            title="Docker Environment Variables", show_header=True, header_style="bold cyan"
        )
        table.add_column("Environment Variable", style="green")
        table.add_column("Value", style="yellow")
        for key, value in sorted(docker_envs.items()):
            table.add_row(key, value)
        captain.console.print(table)

    for k, v in docker_envs.items():
        docker_args += ["-e", f"{k}={v}"]

    if os.environ.get("GITHUB_ACTIONS") == "true":
        log.debug("Running in GitHub Actions, adding bind mount for Docker credentials...")
        github_docker_config = Path.home() / ".docker" / "config.json"
        if github_docker_config.exists() and github_docker_config.is_file():
            log.debug(
                "Under GHA, Found Docker config file at %s, adding to bind mounts.",
                github_docker_config,
            )
            docker_args += ["-v", f"{github_docker_config}:/root/.docker/config.json"]

    if cfg.custom_dir.exists() and cfg.custom_dir.is_dir():
        docker_args += ["-v", f"{cfg.custom_dir}:/work/custom"]

    docker_args += ["-v", f"{cfg.project_dir}/kernel.configs:/work/kernel.configs"]
    docker_args += ["-v", f"{cfg.project_dir}/mkosi.output:/work/mkosi.output"]
    docker_args += ["-v", f"{cfg.project_dir}/mkosi.input:/work/mkosi.input"]
    docker_args += ["-v", f"{cfg.project_dir}/out:/work/out"]

    # The flavor's generated mkosi tree; create it first so Docker doesn't (as root).
    cfg.generated_dir.mkdir(parents=True, exist_ok=True)
    docker_args += ["-v", f"{cfg.generated_dir}:/work/mkosi.generated"]

    docker_args += ["-v", f"{cfg.project_dir}/captain:/work/captain"]
    docker_args += ["-v", f"{cfg.project_dir}/pyproject.toml:/work/pyproject.toml"]
    docker_args += ["-v", f"{cfg.project_dir}/build.py:/work/build.py"]
    # Build-cache keys hash the Dockerfile (toolchain); not part of the image itself.
    docker_args += ["-v", f"{cfg.project_dir}/Dockerfile:/work/Dockerfile:ro"]

    docker_args += ["--mount", "type=volume,source=captain-cache-packages,target=/cache/packages"]
    docker_args += [
        "--mount",
        f"type=volume,source=captain-kernel-build,target={KERNEL_BUILD_BASE_PATH}",
    ]
    docker_args += [cfg.builder_image]

    docker_args.extend(command_and_args)

    # Handle exceptions: when re-launching in Docker, the inner instance already showed
    # the full error with traceback, so we can suppress the redundant outer traceback.
    try:
        run(docker_args)
    except CalledProcessError as e:
        log.error(
            "Command '%s' failed with exit code %d.", " ".join(command_and_args), e.returncode
        )
        raise SystemExit(e.returncode) from None


def run_captain_in_builder(
    cfg: Config, command_and_args: list[str], extra_docker_args: list[str] | None = None
):
    log.debug("Running 'captain %s' in builder container...", command_and_args)
    run_in_builder(
        cfg,
        [
            "/usr/bin/uv",
            *(["--verbose"] if cfg.verbose_uv else ["--quiet"]),
            "run",
            "captain",
            *command_and_args,
        ],
        extra_docker_args=extra_docker_args,
    )


def run_mkosi_in_builder(cfg: Config, *mkosi_args: str) -> None:
    """Run mkosi inside the builder container."""
    ensure_binfmt(cfg)
    run_in_builder(
        cfg,
        command_and_args=[
            "/usr/local/bin/mkosi",
            f"--architecture={cfg.arch_info.mkosi_arch}",
            *mkosi_args,
        ],
    )


def ensure_binfmt(cfg: Config) -> None:
    """Register binfmt_misc handlers if doing a cross-architecture build."""
    host_arch = platform.machine()
    need_binfmt = False

    match (host_arch, cfg.arch):
        case ("x86_64", "arm64" | "aarch64"):
            need_binfmt = True
        case ("aarch64", "amd64" | "x86_64"):
            need_binfmt = True

    if not need_binfmt:
        return

    log.info(
        "Registering binfmt_misc handlers for cross-arch build (%s -> %s)...",
        host_arch,
        cfg.arch,
    )
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--privileged",
            "tonistiigi/binfmt",
            "--install",
            "all",
        ],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        log.warning("Could not auto-register binfmt handlers.")
        log.warning("Run manually: docker run --privileged --rm tonistiigi/binfmt --install all")


def fix_docker_ownership(cfg: Config, paths: list[str]) -> None:
    """Fix ownership of Docker-created files (container runs as root).

    Spawns a lightweight container to ``chown -R`` the given paths
    back to the calling user so that subsequent native-mode stages
    and the host user can read/write them.

    Idempotent: skips the chown if every path either does not exist
    or is already owned by the current user.
    """
    uid = os.getuid()
    gid = os.getgid()

    needs_fix: list[str] = []
    for p in paths:
        host_path = Path(p.replace("/work", str(cfg.project_dir), 1))
        if not host_path.exists():
            continue
        check_paths = [host_path]
        if host_path.is_dir():
            check_paths.extend(host_path.rglob("*"))
        for cp in check_paths:
            try:
                st = cp.stat()
            except OSError:
                continue
            if st.st_uid != uid or st.st_gid != gid:
                needs_fix.append(p)
                break

    if not needs_fix:
        return

    log.info("Fixing ownership of Docker-created files...")
    run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{cfg.project_dir}:/work",
            "-w",
            "/work",
            "debian:trixie",
            "chown",
            "-R",
            f"{uid}:{gid}",
            *needs_fix,
        ],
    )
