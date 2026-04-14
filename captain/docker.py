"""Docker builder image management and container execution."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import sys
from pathlib import Path

from captain.config import Config
from captain.util import run

log = logging.getLogger(__name__)


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


def build_builder(cfg: Config) -> None:
    """Build the Docker builder image when the Dockerfile has changed.

    The image is tagged with a content hash of the Dockerfile so that
    changes are detected even when the base image name stays the same.
    When the matching tag already exists locally (e.g. pre-loaded by a CI
    ``docker/build-push-action`` step with ``load: true``), we skip the
    build entirely.  Use ``NO_CACHE=1`` to force a full rebuild.
    """
    tag = _dockerfile_hash(cfg)
    tagged_image = f"{cfg.builder_image}:{tag}"

    if not cfg.no_cache and _image_exists(tagged_image):
        log.info("Docker image '%s' is up to date.", cfg.builder_image)
        # Ensure the un-hashed tag exists so later docker-run calls that
        # reference cfg.builder_image (without the hash suffix) succeed.
        # This matters when the hashed tag was pre-loaded by CI.
        run(["docker", "tag", tagged_image, cfg.builder_image], check=False)
        return

    log.info("Building Docker image '%s'...", cfg.builder_image)
    cmd = ["docker", "buildx", "build"]
    if cfg.no_cache:
        cmd.append("--no-cache")
    cmd.extend(
        ["--progress=plain", "-t", tagged_image, "-t", cfg.builder_image, str(cfg.project_dir)]
    )
    run(cmd)


RELEASE_IMAGE = "captainos-release"


def _release_dockerfile_hash(cfg: Config) -> str:
    """Return the SHA-256 hex digest of the Dockerfile.release content."""
    dockerfile = cfg.project_dir / "Dockerfile.release"
    return hashlib.sha256(dockerfile.read_bytes()).hexdigest()


def build_release_image(cfg: Config) -> None:
    """Build the release Docker image from ``Dockerfile.release``."""
    tag = _release_dockerfile_hash(cfg)
    tagged_image = f"{RELEASE_IMAGE}:{tag}"

    if not cfg.no_cache and _image_exists(tagged_image):
        log.info("Docker image '%s' is up to date.", RELEASE_IMAGE)
        run(["docker", "tag", tagged_image, RELEASE_IMAGE])
        return

    log.info("Building Docker image '%s'...", RELEASE_IMAGE)
    cmd = ["docker", "buildx", "build", "-f", str(cfg.project_dir / "Dockerfile.release")]
    if cfg.no_cache:
        cmd.append("--no-cache")
    cmd.extend(["--progress=plain"])
    cmd.extend(["-t", tagged_image, "-t", RELEASE_IMAGE, str(cfg.project_dir)])
    run(cmd)


def run_in_release(cfg: Config, *extra_args: str) -> None:
    """Run a command inside the release container.

    Similar to :func:`run_in_builder` but uses the lightweight release
    image which has buildah, skopeo, Python, and git.
    """
    docker_args: list[str] = [
        "docker",
        "run",
        "--rm",
        # Buildah needs mount/remount capabilities for layer operations.
        "--privileged",
        # interactive if running in a terminal
        *(["-i"] if sys.stdout.isatty() and sys.stdin.isatty() else []),
        "-t",  # terminal
        "-v",
        f"{cfg.project_dir}:/work",
        "-w",
        "/work",
        "-e",
        f"ARCH={cfg.arch}",
        "-e",
        "RELEASE_MODE=native",
        # Chroot isolation lets buildah work inside an unprivileged container
        # (no user namespaces needed — we only assemble scratch images).
        "-e",
        "BUILDAH_ISOLATION=chroot",
        "-e",
        f"TERM={os.environ.get('TERM', 'xterm-256color')}",
        "-e",
        f"COLUMNS={os.environ.get('COLUMNS', '200')}",
        "-e",
        f"GITHUB_ACTIONS={os.environ.get('GITHUB_ACTIONS', '')}",
    ]
    # Forward host registry credentials so buildah/skopeo can authenticate.
    # The caller sets these env vars on the host (e.g. via docker login or
    # CI secrets); they are passed through to the container as-is.
    for var in ("REGISTRY_AUTH_FILE", "REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
        val = os.environ.get(var)
        if val:
            docker_args += ["-e", f"{var}={val}"]
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
        # interactive if running in a terminal
        *(["-i"] if sys.stdout.isatty() and sys.stdin.isatty() else []),
        "-t",  # terminal
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
        "-e",
        f"TERM={os.environ.get('TERM', 'xterm-256color')}",
        "-e",
        f"COLUMNS={os.environ.get('COLUMNS', '200')}",
        "-e",
        f"GITHUB_ACTIONS={os.environ.get('GITHUB_ACTIONS', '')}",
    ]

    docker_args += ["-v", f"{cfg.project_dir}/mkosi.output:/work/mkosi.output"]
    docker_args += ["-v", f"{cfg.project_dir}/mkosi.extra:/work/mkosi.extra"]
    docker_args += ["-v", f"{cfg.project_dir}/out:/work/out"]

    docker_args += ["-v", f"{cfg.project_dir}/mkosi.conf:/work/mkosi.conf"]
    docker_args += ["-v", f"{cfg.project_dir}/mkosi.finalize:/work/mkosi.finalize"]
    docker_args += ["-v", f"{cfg.project_dir}/mkosi.postinst:/work/mkosi.postinst"]

    docker_args += ["-v", f"{cfg.project_dir}/captain:/work/captain"]
    docker_args += ["-v", f"{cfg.project_dir}/pyproject.toml:/work/pyproject.toml"]
    docker_args += ["-v", f"{cfg.project_dir}/build.py:/work/build.py"]

    docker_args += ["-v", f"{cfg.project_dir}/kernel.configs:/work/kernel.configs"]

    docker_args += ["--mount", "type=volume,source=captain-cache-packages,target=/cache/packages"]

    # Mount kernel source if provided
    if cfg.kernel_src is not None:
        kernel_src_path = Path(cfg.kernel_src).resolve()
        if not kernel_src_path.is_dir():
            log.error("KERNEL_SRC=%s does not exist", cfg.kernel_src)
            raise SystemExit(1)
        docker_args.extend(["-v", f"{kernel_src_path}:/work/kernel-src:ro"])
        docker_args.extend(["-e", "KERNEL_SRC=/work/kernel-src"])

    # Mount kernel config override and point KERNEL_CONFIG to the container path
    if cfg.kernel_config is not None:
        kernel_cfg_path = Path(cfg.kernel_config)
        if not kernel_cfg_path.is_absolute():
            kernel_cfg_path = (cfg.project_dir / kernel_cfg_path).resolve()
        else:
            kernel_cfg_path = kernel_cfg_path.resolve()
        if not kernel_cfg_path.is_file():
            log.error("KERNEL_CONFIG=%s does not exist", cfg.kernel_config)
            raise SystemExit(1)
        docker_args.extend(["-v", f"{kernel_cfg_path}:/work/kernel-config:ro"])
        docker_args.extend(["-e", "KERNEL_CONFIG=/work/kernel-config"])

    docker_args.extend(extra_args)
    log.debug("Docker args (builder): %s", docker_args)
    run(docker_args)


def run_mkosi(cfg: Config, *mkosi_args: str) -> None:
    """Run mkosi inside the builder container."""
    ensure_binfmt(cfg)
    run_in_builder(
        cfg,
        cfg.builder_image,
        f"--architecture={cfg.arch_info.mkosi_arch}",
        *mkosi_args,
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
