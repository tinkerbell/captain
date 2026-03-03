"""CLI entry point — argparse subcommands mirroring build.sh interface."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from argparse import ArgumentParser
from pathlib import Path

from captain import artifacts, docker, iso, kernel, qemu, tools
from captain.config import Config
from captain.log import for_stage
from captain.util import check_kernel_dependencies, check_mkosi_dependencies, run


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

def _build_kernel_stage(cfg: Config) -> None:
    """Run the kernel build stage according to *cfg.kernel_mode*."""
    klog = for_stage("kernel")
    match cfg.kernel_mode:
        case "skip":
            klog.log("KERNEL_MODE=skip — skipping kernel build")
            return
        case "native":
            missing = check_kernel_dependencies(cfg.arch)
            if missing:
                klog.err(f"Missing kernel build tools: {', '.join(missing)}")
                klog.err("Install them or set KERNEL_MODE=docker.")
                raise SystemExit(1)
            modules_dir = cfg.extra_tree_output / "usr" / "lib" / "modules"
            vmlinuz_dir = cfg.vmlinuz_output
            has_vmlinuz = vmlinuz_dir.is_dir() and any(vmlinuz_dir.glob("vmlinuz-*"))
            if modules_dir.is_dir() and has_vmlinuz and not cfg.force_kernel:
                klog.log("Kernel already built (set FORCE_KERNEL=1 to rebuild)")
            else:
                if modules_dir.is_dir() and not has_vmlinuz:
                    klog.warn("Modules exist but vmlinuz is missing — rebuilding kernel")
                klog.log("Building kernel (native)...")
                kernel.build(cfg)
        case "docker":
            docker.build_builder(cfg, logger=klog)
            modules_dir = cfg.extra_tree_output / "usr" / "lib" / "modules"
            vmlinuz_dir = cfg.vmlinuz_output
            has_vmlinuz = vmlinuz_dir.is_dir() and any(vmlinuz_dir.glob("vmlinuz-*"))
            if modules_dir.is_dir() and has_vmlinuz and not cfg.force_kernel:
                klog.log("Kernel already built (set FORCE_KERNEL=1 to rebuild)")
            else:
                if modules_dir.is_dir() and not has_vmlinuz:
                    klog.warn("Modules exist but vmlinuz is missing — rebuilding kernel")
                klog.log("Building kernel (docker)...")
                docker.run_in_builder(
                    cfg,
                    "--entrypoint",
                    "python3",
                    cfg.builder_image,
                    "/work/scripts/build-kernel.py",
                )


def _build_tools_stage(cfg: Config) -> None:
    """Run the tools download stage according to *cfg.kernel_mode*."""
    tlog = for_stage("tools")
    match cfg.kernel_mode:
        case "skip":
            # When kernel_mode is skip we still download tools directly
            tlog.log("Downloading tools (nerdctl, containerd, etc.)...")
            tools.download_all(cfg)
        case "native":
            tlog.log("Downloading tools (nerdctl, containerd, etc.)...")
            tools.download_all(cfg)
        case "docker":
            docker.build_builder(cfg, logger=tlog)
            tlog.log("Downloading tools (nerdctl, containerd, etc.)...")
            docker.run_in_builder(
                cfg,
                "--entrypoint",
                "python3",
                cfg.builder_image,
                "/work/scripts/download-tools.py",
            )
            # The Docker container runs as root, so files it creates inside
            # the bind-mounted mkosi.output/ are owned by root.  If the next
            # stage runs natively (MKOSI_MODE=native), mkosi won't be able to
            # write to that directory.  Fix ownership now.
            if cfg.mkosi_mode == "native":
                uid = os.getuid()
                gid = os.getgid()
                tlog.log("Fixing ownership of mkosi.output/ for native mkosi...")
                run(
                    [
                        "docker", "run", "--rm",
                        "-v", f"{cfg.project_dir}:/work",
                        "-w", "/work",
                        "debian:trixie",
                        "chown", "-R", f"{uid}:{gid}", "/work/mkosi.output",
                    ],
                )


def _build_mkosi_stage(cfg: Config, extra_args: list[str]) -> None:
    """Run the mkosi image-assembly stage according to *cfg.mkosi_mode*."""
    ilog = for_stage("initramfs")
    match cfg.mkosi_mode:
        case "skip":
            ilog.log("MKOSI_MODE=skip — skipping image assembly")
            return
        case "native":
            missing = check_mkosi_dependencies()
            if missing:
                ilog.err(f"Missing mkosi tools: {', '.join(missing)}")
                ilog.err("Install them or set MKOSI_MODE=docker.")
                raise SystemExit(1)
            ilog.log("Building initrd with mkosi (native)...")
            mkosi_args = list(cfg.mkosi_args) + list(extra_args)
            extra_tree = str(cfg.extra_tree_output)
            output_dir = str(cfg.initramfs_output)
            run(
                [
                    "mkosi",
                    f"--architecture={cfg.arch_info.mkosi_arch}",
                    f"--extra-tree={extra_tree}",
                    f"--output-dir={output_dir}",
                    "build",
                    *mkosi_args,
                ],
                cwd=cfg.project_dir,
            )
        case "docker":
            if cfg.kernel_mode != "docker":
                # Builder image may not have been built yet
                docker.build_builder(cfg, logger=ilog)
            ilog.log("Building initrd with mkosi (docker)...")
            mkosi_args = list(cfg.mkosi_args) + list(extra_args)
            # Inside the container the project is mounted at /work
            extra_tree = f"/work/mkosi.output/extra-tree/{cfg.arch}"
            output_dir = f"/work/mkosi.output/initramfs/{cfg.arch}"
            docker.run_mkosi(cfg, f"--extra-tree={extra_tree}", f"--output-dir={output_dir}", "build", *mkosi_args, logger=ilog)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_kernel(cfg: Config, _extra_args: list[str]) -> None:
    """Build only the kernel (no tools, no mkosi)."""
    klog = for_stage("kernel")
    _build_kernel_stage(cfg)
    # Copy vmlinuz to the standard out/ directory.
    artifacts.collect_kernel(cfg, logger=klog)
    klog.log("Kernel build stage complete!")


def _cmd_tools(cfg: Config, _extra_args: list[str]) -> None:
    """Download tools (containerd, runc, nerdctl, CNI plugins)."""
    _build_tools_stage(cfg)
    tlog = for_stage("tools")
    tlog.log("Tools stage complete!")


def _check_kernel_modules(cfg: Config) -> None:
    """Verify kernel modules exist before building the initramfs.

    The initramfs depends on pre-built kernel modules in the ExtraTrees
    directory.  If they are missing (e.g. due to an artifact download
    issue) the build should fail immediately rather than silently
    producing an initramfs without modules.
    """
    ilog = for_stage("initramfs")
    modules_dir = cfg.extra_tree_output / "usr" / "lib" / "modules"
    if not modules_dir.is_dir():
        ilog.err(f"Kernel modules directory not found: {modules_dir}")
        ilog.err("Ensure the kernel build artifacts are downloaded correctly.")
        raise SystemExit(1)
    # Check that at least one module version directory exists with modules
    version_dirs = [d for d in modules_dir.iterdir() if d.is_dir()]
    if not version_dirs:
        ilog.err(f"No kernel version directories found in {modules_dir}")
        raise SystemExit(1)
    # Search all version directories for at least one kernel module
    for version_dir in version_dirs:
        if any(version_dir.rglob("*.ko*")):
            ilog.log(f"Kernel modules found in {version_dir} (version: {version_dir.name})")
            return
    searched = ", ".join(str(d) for d in version_dirs)
    ilog.err("No kernel modules (.ko/.ko.zst) found in any kernel version directory.")
    ilog.err(f"Searched directories: {searched}")
    raise SystemExit(1)


def _cmd_initramfs(cfg: Config, extra_args: list[str]) -> None:
    """Build only the initramfs via mkosi, then collect artifacts."""
    ilog = for_stage("initramfs")
    _check_kernel_modules(cfg)
    _build_mkosi_stage(cfg, extra_args)
    artifacts.collect(cfg, logger=ilog)
    ilog.log("Initramfs build complete!")


def _build_iso_stage(cfg: Config) -> None:
    """Run the ISO build stage according to *cfg.iso_mode*."""
    isolog = for_stage("iso")
    match cfg.iso_mode:
        case "skip":
            isolog.log("ISO_MODE=skip — skipping ISO build")
            return
        case "native":
            isolog.log("Building ISO (native)...")
            iso.build(cfg)
        case "docker":
            docker.build_builder(cfg, logger=isolog)
            isolog.log("Building ISO (docker)...")
            docker.run_in_builder(
                cfg,
                "--entrypoint",
                "python3",
                cfg.builder_image,
                "/work/scripts/build-iso.py",
            )
            # Fix ownership of Docker-created files (container runs as root)
            uid = os.getuid()
            gid = os.getgid()
            isolog.log("Fixing ownership of ISO output files...")
            run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{cfg.project_dir}:/work",
                    "-w", "/work",
                    "debian:trixie",
                    "chown", "-R", f"{uid}:{gid}",
                    f"/work/mkosi.output/iso",
                    f"/work/mkosi.output/iso-staging",
                ],
            )


def _cmd_iso(cfg: Config, _extra_args: list[str]) -> None:
    """Build only the ISO image."""
    isolog = for_stage("iso")
    _build_iso_stage(cfg)
    artifacts.collect(cfg, logger=isolog)
    isolog.log("ISO build complete!")


def _cmd_build(cfg: Config, extra_args: list[str]) -> None:
    """Full build: kernel → tools → initramfs → iso → artifacts."""
    blog = for_stage("build")
    _build_kernel_stage(cfg)
    _build_tools_stage(cfg)
    _build_mkosi_stage(cfg, extra_args)
    _build_iso_stage(cfg)
    artifacts.collect(cfg, logger=blog)
    blog.log("Build complete!")


def _cmd_shell(cfg: Config, _extra_args: list[str]) -> None:
    """Interactive shell inside the builder container."""
    slog = for_stage("shell")
    if not cfg.needs_docker:
        slog.err("'shell' requires at least one stage using Docker.")
        slog.err("Set KERNEL_MODE=docker or MKOSI_MODE=docker.")
        raise SystemExit(1)
    docker.build_builder(cfg, logger=slog)
    slog.log("Entering builder shell (type 'exit' to leave)...")
    docker.run_in_builder(cfg, "-it", "--entrypoint", "/bin/bash", cfg.builder_image)


def _cmd_clean(cfg: Config, _extra_args: list[str]) -> None:
    """Remove all build artifacts."""
    clog = for_stage("clean")
    clog.log("Cleaning build artifacts...")
    mkosi_output = cfg.mkosi_output
    mkosi_cache = cfg.project_dir / "mkosi.cache"

    has_docker = shutil.which("docker") is not None
    if has_docker:
        # Use Docker to remove root-owned files from mkosi
        if mkosi_output.exists() or mkosi_cache.exists():
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
                    "sh",
                    "-c",
                    "rm -rf /work/mkosi.output/image* /work/mkosi.output/initramfs /work/mkosi.output/vmlinuz /work/mkosi.output/extra-tree /work/mkosi.output/iso /work/mkosi.output/iso-staging /work/mkosi.cache",
                ],
            )
    else:
        # No Docker available — remove directly (may need sudo for root-owned mkosi files)
        for pattern in ("image*", "initramfs", "vmlinuz", "extra-tree", "iso", "iso-staging"):
            for p in mkosi_output.glob(pattern):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
        if mkosi_cache.exists():
            shutil.rmtree(mkosi_cache, ignore_errors=True)

    if cfg.output_dir.exists():
        shutil.rmtree(cfg.output_dir)
    clog.log("Clean complete.")


def _cmd_summary(cfg: Config, _extra_args: list[str]) -> None:
    """Print mkosi configuration summary."""
    slog = for_stage("summary")
    extra_tree = str(cfg.extra_tree_output)
    output_dir = str(cfg.initramfs_output)
    match cfg.mkosi_mode:
        case "docker":
            docker.build_builder(cfg, logger=slog)
            container_tree = f"/work/mkosi.output/extra-tree/{cfg.arch}"
            container_outdir = f"/work/mkosi.output/initramfs/{cfg.arch}"
            docker.run_mkosi(cfg, f"--extra-tree={container_tree}", f"--output-dir={container_outdir}", "summary", logger=slog)
        case "native":
            run(
                ["mkosi", f"--architecture={cfg.arch_info.mkosi_arch}", f"--extra-tree={extra_tree}", f"--output-dir={output_dir}", "summary"],
                cwd=cfg.project_dir,
            )
        case "skip":
            slog.err("Cannot show mkosi summary when MKOSI_MODE=skip.")
            raise SystemExit(1)


def _cmd_qemu_test(cfg: Config, _extra_args: list[str]) -> None:
    """Boot the image in QEMU for testing."""
    qemu.run_qemu(cfg)


def main(project_dir: Path | None = None) -> None:
    """Main CLI entry point."""
    # Require Python >= 3.10
    if sys.version_info < (3, 10):
        print("ERROR: Python >= 3.10 is required.", file=sys.stderr)
        sys.exit(1)

    env_help = """\

environment variables:
  ARCH            Target architecture: amd64 (default) or arm64
  KERNEL_MODE     Kernel build mode: docker (default), native, or skip
  MKOSI_MODE      mkosi build mode: docker (default), native, or skip
  ISO_MODE        ISO build mode: skip (default), docker, or native
  KERNEL_SRC      Path to local kernel source tree
  KERNEL_VERSION  Kernel version to build (default: 6.12.69)
  FORCE_KERNEL    Set to 1 to force kernel rebuild
  FORCE_TOOLS     Set to 1 to re-download tools
  NO_CACHE        Set to 1 to rebuild builder image without cache
  BUILDER_IMAGE   Override builder Docker image name
  QEMU_APPEND     Extra kernel cmdline args for qemu-test
  QEMU_MEM        QEMU RAM size (default: 2G)
  QEMU_SMP        QEMU CPU count (default: 2)

examples:
  ./build.py                                  Build with defaults (all Docker)
  ARCH=arm64 ./build.py                       Build for ARM64
  KERNEL_SRC=~/linux ./build.py               Use local kernel source
  FORCE_KERNEL=1 ./build.py                   Force kernel rebuild
  KERNEL_MODE=skip MKOSI_MODE=native build.py Skip kernel, native mkosi
  ISO_MODE=docker ./build.py iso               Build ISO in Docker
  KERNEL_MODE=native ./build.py               Native kernel, Docker mkosi
  ./build.py shell                            Debug inside builder
  ./build.py qemu-test                        Boot test with QEMU"""

    parser = ArgumentParser(
        prog="build.py",
        description="Build CaptainOS images. Stages: kernel → tools → initramfs → iso.",
        epilog=env_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "build",
        help="Run all build stages: kernel → tools → initramfs → iso (default)",
    )
    sub.add_parser("kernel", help="Build only the kernel + modules")
    sub.add_parser("tools", help="Download tools (containerd, runc, nerdctl, CNI)")
    sub.add_parser("initramfs", help="Build only the initramfs via mkosi")
    sub.add_parser("iso", help="Build a UEFI-bootable ISO image")
    sub.add_parser("shell", help="Interactive shell inside the builder container")
    sub.add_parser("clean", help="Remove all build artifacts")
    sub.add_parser("summary", help="Print mkosi configuration summary")
    qemu_env_help = """\
environment variables (Tinkerbell kernel cmdline):
  TINK_GRPC_AUTHORITY       tink-server gRPC endpoint (host:port)
  TINK_DOCKER_REGISTRY      Registry host (triggers tink-agent services)
  TINK_WORKER_IMAGE         Full image ref (overrides TINK_DOCKER_REGISTRY)
  TINK_WORKER_ID            Machine / worker ID (auto-detected when empty)
  TINK_TLS                  Enable TLS to tink-server (default: false)
  TINK_INSECURE_TLS         Allow insecure TLS (default: true)
  TINK_INSECURE_REGISTRIES  Comma-separated insecure registries
  TINK_REGISTRY_USERNAME    Registry auth username
  TINK_REGISTRY_PASSWORD    Registry auth password
  TINK_SYSLOG_HOST          Remote syslog host
  TINK_FACILITY             Facility code

  QEMU_APPEND               Extra kernel cmdline args
  QEMU_MEM                  QEMU RAM size (default: 2G)
  QEMU_SMP                  QEMU CPU count (default: 2)

example:
  TINK_DOCKER_REGISTRY=10.0.2.2:5000 \\
  TINK_GRPC_AUTHORITY=10.0.2.2:42113 \\
  TINK_INSECURE_REGISTRIES=10.0.2.2:5000 \\
  ./build.py qemu-test"""
    sub.add_parser(
        "qemu-test",
        help="Boot the image in QEMU for testing",
        epilog=qemu_env_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Parse known args — anything unknown is passed through to mkosi
    args, extra = parser.parse_known_args()

    # Handle --force as a global flag (passed through to mkosi)
    mkosi_args: list[str] = []
    remaining: list[str] = []
    for a in extra:
        if a == "--force":
            mkosi_args.append("--force")
        elif a == "--force-kernel":
            # Treat as env-var equivalent
            import os

            os.environ["FORCE_KERNEL"] = "1"
        else:
            remaining.append(a)

    # Determine project directory
    if project_dir is None:
        project_dir = Path(__file__).resolve().parent.parent

    cfg = Config.from_env(project_dir)
    cfg.mkosi_args = mkosi_args

    command = args.command or "build"

    dispatch = {
        "build": _cmd_build,
        "kernel": _cmd_kernel,
        "tools": _cmd_tools,
        "initramfs": _cmd_initramfs,
        "iso": _cmd_iso,
        "shell": _cmd_shell,
        "clean": _cmd_clean,
        "summary": _cmd_summary,
        "qemu-test": _cmd_qemu_test,
    }

    handler = dispatch.get(command)
    if handler is not None:
        handler(cfg, remaining)
    else:
        # Pass through to mkosi
        mlog = for_stage("mkosi")
        extra_tree = str(cfg.extra_tree_output)
        output_dir = str(cfg.initramfs_output)
        match cfg.mkosi_mode:
            case "docker":
                docker.build_builder(cfg, logger=mlog)
                container_tree = f"/work/mkosi.output/extra-tree/{cfg.arch}"
                container_outdir = f"/work/mkosi.output/initramfs/{cfg.arch}"
                docker.run_mkosi(cfg, f"--extra-tree={container_tree}", f"--output-dir={container_outdir}", command, *remaining, logger=mlog)
            case "native":
                run(
                    ["mkosi", f"--architecture={cfg.arch_info.mkosi_arch}", f"--extra-tree={extra_tree}", f"--output-dir={output_dir}", command, *remaining],
                    cwd=cfg.project_dir,
                )
            case "skip":
                mlog.err(f"Cannot pass '{command}' to mkosi when MKOSI_MODE=skip.")
                raise SystemExit(1)
