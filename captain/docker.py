"""Docker builder image management and container execution."""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path

from captain.config import Config
from captain.log import StageLogger, err, for_stage
from captain.util import run

_default_log = for_stage("docker")


def _image_exists(image: str) -> bool:
    """Check if a Docker image exists locally."""
    result = run(
        ["docker", "image", "inspect", image],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _dockerfile_hash(cfg: Config) -> str:
    """Return the SHA-256 hex digest of the Dockerfile content.

    This is used as an image tag so that Dockerfile changes are detected
    automatically.  The value intentionally matches what GitHub Actions
    ``hashFiles('Dockerfile')`` produces, allowing the CI
    ``docker/build-push-action`` step to pre-load an image with the same
    tag that ``build_builder`` will look for.
    """
    dockerfile = cfg.project_dir / "Dockerfile"
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()


def build_builder(cfg: Config, logger: StageLogger | None = None) -> None:
    """Build the Docker builder image when the Dockerfile has changed.

    The image is tagged with a content hash of the Dockerfile so that
    changes are detected even when the base image name stays the same.
    When the matching tag already exists locally (e.g. pre-loaded by a CI
    ``docker/build-push-action`` step with ``load: true``), we skip the
    build entirely.  Use ``NO_CACHE=1`` to force a full rebuild.
    """
    _log = logger or _default_log
    tag = _dockerfile_hash(cfg)
    tagged_image = f"{cfg.builder_image}:{tag}"

    if not cfg.no_cache and _image_exists(tagged_image):
        _log.log(f"Docker image '{cfg.builder_image}' is up to date.")
        # Ensure the un-hashed tag exists so later docker-run calls that
        # reference cfg.builder_image (without the hash suffix) succeed.
        # This matters when the hashed tag was pre-loaded by CI.
        run(["docker", "tag", tagged_image, cfg.builder_image], check=False)
        return

    _log.log(f"Building Docker image '{cfg.builder_image}'...")
    cmd = ["docker", "build"]
    if cfg.no_cache:
        cmd.append("--no-cache")
    cmd.extend(["-t", tagged_image, "-t", cfg.builder_image, str(cfg.project_dir)])
    run(cmd)


RELEASE_IMAGE = "captainos-release"


def _release_dockerfile_hash(cfg: Config) -> str:
    """Return the SHA-256 hex digest of the Dockerfile.release content."""
    dockerfile = cfg.project_dir / "Dockerfile.release"
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()


def build_release_image(cfg: Config, logger: StageLogger | None = None) -> None:
    """Build the release Docker image from ``Dockerfile.release``."""
    _log = logger or _default_log
    tag = _release_dockerfile_hash(cfg)
    tagged_image = f"{RELEASE_IMAGE}:{tag}"

    if not cfg.no_cache and _image_exists(tagged_image):
        _log.log(f"Docker image '{RELEASE_IMAGE}' is up to date.")
        run(["docker", "tag", tagged_image, RELEASE_IMAGE])
        return

    _log.log(f"Building Docker image '{RELEASE_IMAGE}'...")
    cmd = ["docker", "build", "-f", str(cfg.project_dir / "Dockerfile.release")]
    if cfg.no_cache:
        cmd.append("--no-cache")
    cmd.extend(["-t", tagged_image, "-t", RELEASE_IMAGE, str(cfg.project_dir)])
    run(cmd)


def run_in_release(cfg: Config, *extra_args: str) -> None:
    """Run a command inside the release container.

    Similar to :func:`run_in_builder` but uses the lightweight release
    image which has crane, Python, and git.
    """
    docker_args: list[str] = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{cfg.project_dir}:/work",
        "-w",
        "/work",
        "-e",
        f"ARCH={cfg.arch}",
        "-e",
        "RELEASE_MODE=native",
    ]
    docker_args.extend(extra_args)
    run(docker_args)


def run_in_builder(cfg: Config, *extra_args: str) -> None:
    """Run a command inside the Docker builder container.

    *extra_args* are appended after the docker run flags and image name.
    """
    docker_args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "-v",
        f"{cfg.project_dir}:/work",
        "-w",
        "/work",
        "-e",
        f"ARCH={cfg.arch}",
        "-e",
        f"KERNEL_VERSION={cfg.kernel_version}",
        "-e",
        f"FORCE_TOOLS={int(cfg.force_tools)}",
        "-e",
        f"FORCE_KERNEL={int(cfg.force_kernel)}",
        "-e",
        f"FORCE_ISO={int(cfg.force_iso)}",
        # Force all stage modes to native inside the container so
        # build.py never tries to launch Docker recursively.
        "-e",
        "KERNEL_MODE=native",
        "-e",
        "TOOLS_MODE=native",
        "-e",
        "MKOSI_MODE=native",
        "-e",
        "ISO_MODE=native",
        "-e",
        "RELEASE_MODE=native",
    ]

    # Mount kernel source if provided
    if cfg.kernel_src is not None:
        kernel_src_path = Path(cfg.kernel_src).resolve()
        if not kernel_src_path.is_dir():
            err(f"KERNEL_SRC={cfg.kernel_src} does not exist")
            raise SystemExit(1)
        docker_args.extend(["-v", f"{kernel_src_path}:/work/kernel-src:ro"])
        docker_args.extend(["-e", "KERNEL_SRC=/work/kernel-src"])

    docker_args.extend(extra_args)
    run(docker_args)


def run_mkosi(cfg: Config, *mkosi_args: str, logger: StageLogger | None = None) -> None:
    """Run mkosi inside the builder container."""
    ensure_binfmt(cfg, logger=logger)
    run_in_builder(
        cfg,
        cfg.builder_image,
        f"--architecture={cfg.arch_info.mkosi_arch}",
        *mkosi_args,
    )


def ensure_binfmt(cfg: Config, logger: StageLogger | None = None) -> None:
    """Register binfmt_misc handlers if doing a cross-architecture build."""
    _log = logger or _default_log
    host_arch = platform.machine()  # e.g. "x86_64" or "aarch64"
    need_binfmt = False

    match (host_arch, cfg.arch):
        case ("x86_64", "arm64" | "aarch64"):
            need_binfmt = True
        case ("aarch64", "amd64" | "x86_64"):
            need_binfmt = True

    if not need_binfmt:
        return

    _log.log(
        f"Registering binfmt_misc handlers for cross-arch build ({host_arch} -> {cfg.arch})..."
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
        _log.warn("Could not auto-register binfmt handlers.")
        _log.warn("Run manually: docker run --privileged --rm tonistiigi/binfmt --install all")


def fix_docker_ownership(cfg: Config, logger, paths: list[str]) -> None:
    """Fix ownership of Docker-created files (container runs as root).

    Spawns a lightweight container to ``chown -R`` the given paths
    back to the calling user so that subsequent native-mode stages
    and the host user can read/write them.

    Idempotent: skips the chown if every path either does not exist
    or is already owned by the current user.
    """
    uid = os.getuid()
    gid = os.getgid()

    # *paths* use the container mount prefix /work — translate to host.
    # Check the path itself **and** every child — the top-level directory
    # may already be owned by the host user while files inside it were
    # created by the container (root).
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

    logger.log("Fixing ownership of Docker-created files...")
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
