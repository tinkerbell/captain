"""Build and utility command handlers."""

from __future__ import annotations

import shutil
from pathlib import Path

from captain import artifacts, docker, qemu
from captain.config import Config
from captain.log import StageLogger, for_stage
from captain.util import run

from ._stages import (
    _build_iso_stage,
    _build_kernel_stage,
    _build_mkosi_stage,
    _build_tools_stage,
)


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
    modules_dir = cfg.modules_output / "usr" / "lib" / "modules"
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
    artifacts.collect_initramfs(cfg, logger=ilog)
    artifacts.collect_kernel(cfg, logger=ilog)
    ilog.log("Initramfs build complete!")


def _cmd_iso(cfg: Config, _extra_args: list[str]) -> None:
    """Build only the ISO image."""
    isolog = for_stage("iso")
    _build_iso_stage(cfg)
    artifacts.collect_iso(cfg, logger=isolog)
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
    docker.build_builder(cfg, logger=slog)
    slog.log("Entering builder shell (type 'exit' to leave)...")
    docker.run_in_builder(cfg, "-it", "--entrypoint", "/bin/bash", cfg.builder_image)


def _cmd_clean(cfg: Config, _extra_args: list[str], args: object = None) -> None:
    """Remove build artifacts for the selected kernel version, or all."""
    clog = for_stage("clean")
    clean_all = getattr(args, "clean_all", False)

    if clean_all:
        _clean_all(cfg, clog)
    else:
        _clean_version(cfg, clog)


def _clean_version(cfg: Config, clog: StageLogger) -> None:
    """Remove build artifacts for a single kernel version."""
    kver = cfg.kernel_version
    clog.log(f"Cleaning build artifacts for kernel {kver} ({cfg.arch})...")
    mkosi_output = cfg.mkosi_output

    # Version-specific directories under mkosi.output/{stage}/{version}/{arch}
    version_dirs = [
        mkosi_output / "kernel" / kver / cfg.arch,
        mkosi_output / "initramfs" / kver / cfg.arch,
        mkosi_output / "iso" / kver / cfg.arch,
    ]

    has_docker = shutil.which("docker") is not None
    existing = [d for d in version_dirs if d.exists()]
    if existing and has_docker:
        # Use Docker to remove root-owned files from mkosi.
        # Invoke rm directly (no shell) to avoid injection via path components.
        container_path_args = [
            f"/work/mkosi.output/{d.relative_to(mkosi_output)}" for d in existing
        ]
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
                "rm",
                "-rf",
                "--",
                *container_path_args,
            ],
        )
    elif existing:
        for d in existing:
            shutil.rmtree(d, ignore_errors=True)

    # Remove versioned artifacts from out/
    if cfg.output_dir.exists():
        for pattern in (
            f"vmlinuz-{kver}-*",
            f"initramfs-{kver}-*",
            f"captainos-{kver}-*",
            f"sha256sums-{kver}-*",
        ):
            for p in cfg.output_dir.glob(pattern):
                p.unlink(missing_ok=True)

    clog.log(f"Clean complete for kernel {kver}.")


def _clean_all(cfg: Config, clog: StageLogger) -> None:
    """Remove all build artifacts (all kernel versions)."""
    clog.log("Cleaning ALL build artifacts...")
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
                    "rm -rf /work/mkosi.output/image*"
                    " /work/mkosi.output/initramfs"
                    " /work/mkosi.output/kernel"
                    " /work/mkosi.output/tools"
                    " /work/mkosi.output/iso"
                    " /work/mkosi.cache",
                ],
            )
    else:
        # No Docker available — remove directly (may need sudo for root-owned mkosi files)
        for pattern in ("image*", "initramfs", "kernel", "tools", "iso"):
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
    tools_tree = str(cfg.tools_output)
    modules_tree = str(cfg.modules_output)
    output_dir = str(cfg.initramfs_output)
    match cfg.mkosi_mode:
        case "docker":
            docker.build_builder(cfg, logger=slog)
            container_tree = f"/work/mkosi.output/tools/{cfg.arch}"
            container_modules = f"/work/mkosi.output/kernel/{cfg.kernel_version}/{cfg.arch}/modules"
            container_outdir = f"/work/mkosi.output/initramfs/{cfg.kernel_version}/{cfg.arch}"
            docker.run_mkosi(
                cfg,
                f"--extra-tree={container_tree}",
                f"--extra-tree={container_modules}",
                f"--output-dir={container_outdir}",
                "summary",
                logger=slog,
            )
        case "native":
            run(
                [
                    "mkosi",
                    f"--architecture={cfg.arch_info.mkosi_arch}",
                    f"--extra-tree={tools_tree}",
                    f"--extra-tree={modules_tree}",
                    f"--output-dir={output_dir}",
                    "summary",
                ],
                cwd=cfg.project_dir,
            )
        case "skip":
            slog.err("Cannot show mkosi summary when MKOSI_MODE=skip.")
            raise SystemExit(1)


def _cmd_checksums(cfg: Config, _extra_args: list[str], args: object = None) -> None:
    """Compute SHA-256 checksums for the specified files."""
    clog = for_stage("checksums")
    files = getattr(args, "files", None) or []
    output = getattr(args, "output", None)

    if files:
        # Explicit mode: user provided specific files and output.
        if not output:
            clog.err("--output is required when specifying files explicitly.")
            raise SystemExit(1)
        artifacts.collect_checksums(
            [Path(f) for f in files],
            Path(output),
            logger=clog,
        )
    else:
        # Default mode: produce checksums for the selected architecture.
        out = cfg.output_dir
        oarch = cfg.arch_info.output_arch
        kver = cfg.kernel_version
        arch_files = [
            out / f"vmlinuz-{kver}-{oarch}",
            out / f"initramfs-{kver}-{oarch}",
            out / f"captainos-{kver}-{oarch}.iso",
        ]
        existing = [f for f in arch_files if f.is_file()]
        if not existing:
            clog.err(f"No artifacts found for {kver}-{oarch} in {out}")
            raise SystemExit(1)
        dest = Path(output) if output else out / f"sha256sums-{kver}-{oarch}.txt"
        artifacts.collect_checksums(existing, dest, logger=clog)
    clog.log("Checksums complete!")


def _cmd_qemu_test(cfg: Config, _extra_args: list[str], args: object = None) -> None:
    """Boot the image in QEMU for testing."""
    qemu.run_qemu(cfg, args=args)  # type: ignore[arg-type]
