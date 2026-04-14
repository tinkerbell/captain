"""Build stage orchestration — kernel, tools, mkosi, ISO."""

from __future__ import annotations

import logging

from captain import docker, iso, kernel, tools
from captain.config import Config
from captain.util import check_kernel_dependencies, check_mkosi_dependencies, run

log = logging.getLogger(__name__)


def _build_kernel_stage(cfg: Config) -> None:
    """Run the kernel build stage according to *cfg.kernel_mode*."""

    # --- skip ---------------------------------------------------------
    if cfg.kernel_mode == "skip":
        log.info("KERNEL_MODE=skip — skipping kernel build")
        return

    # --- idempotency --------------------------------------------------
    modules_dir = cfg.modules_output / "usr" / "lib" / "modules"
    vmlinuz_dir = cfg.kernel_output
    has_vmlinuz = vmlinuz_dir.is_dir() and any(vmlinuz_dir.glob("vmlinuz-*"))

    if modules_dir.is_dir() and has_vmlinuz and not cfg.force_kernel:
        log.info("Kernel already built (use --force-kernel to rebuild)")
        return

    if modules_dir.is_dir() and not has_vmlinuz:
        log.warning("Modules exist but vmlinuz is missing — rebuilding kernel")

    # --- native -------------------------------------------------------
    if cfg.kernel_mode == "native":
        missing = check_kernel_dependencies(cfg.arch)
        if missing:
            log.error("Missing kernel build tools: %s", ", ".join(missing))
            log.error("Install them or set --kernel-mode=docker.")
            raise SystemExit(1)
        log.info("Building kernel (native)...")
        kernel.build(cfg)
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg)
    log.info("Building kernel (docker)...")
    docker.run_in_builder(
        cfg,
        "--entrypoint",
        "/usr/bin/uv",
        cfg.builder_image,
        *(["--verbose"] if logging.getLogger().isEnabledFor(logging.DEBUG) else []),
        "run",
        "/work/build.py",
        "kernel",
    )
    docker.fix_docker_ownership(
        cfg,
        [
            f"/work/mkosi.output/kernel/{cfg.kernel_version}/{cfg.arch}",
            "/work/out",
        ],
    )


def _build_tools_stage(cfg: Config) -> None:
    """Run the tools download stage according to *cfg.tools_mode*."""

    # --- skip ---------------------------------------------------------
    if cfg.tools_mode == "skip":
        log.info("TOOLS_MODE=skip — skipping tools download")
        return

    # --- native -------------------------------------------------------
    if cfg.tools_mode == "native":
        log.info("Downloading tools (nerdctl, containerd, etc.)...")
        tools.download_all(cfg)
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg)
    log.info("Downloading tools (nerdctl, containerd, etc.)...")
    docker.run_in_builder(
        cfg,
        "--entrypoint",
        "/usr/bin/uv",
        cfg.builder_image,
        *(["--verbose"] if logging.getLogger().isEnabledFor(logging.DEBUG) else []),
        "run",
        "/work/build.py",
        "tools",
    )
    docker.fix_docker_ownership(cfg, ["/work/mkosi.output"])


def _build_mkosi_stage(cfg: Config, extra_args: list[str]) -> None:
    """Run the mkosi image-assembly stage according to *cfg.mkosi_mode*."""

    # --- skip ---------------------------------------------------------
    if cfg.mkosi_mode == "skip":
        log.info("MKOSI_MODE=skip — skipping image assembly")
        return

    mkosi_args = list(cfg.mkosi_args) + list(extra_args)

    # --- native -------------------------------------------------------
    if cfg.mkosi_mode == "native":
        missing = check_mkosi_dependencies()
        if missing:
            log.error("Missing mkosi tools: %s", ", ".join(missing))
            log.error("Install them or set --mkosi-mode=docker.")
            raise SystemExit(1)
        log.info("Building initrd with mkosi (native)...")
        tools_tree = str(cfg.tools_output)
        modules_tree = str(cfg.modules_output)
        output_dir = str(cfg.initramfs_output)
        run(
            [
                "mkosi",
                f"--architecture={cfg.arch_info.mkosi_arch}",
                f"--extra-tree={tools_tree}",
                f"--extra-tree={modules_tree}",
                f"--output-dir={output_dir}",
                "build",
                *mkosi_args,
            ],
            cwd=cfg.project_dir,
        )
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg)
    log.info("Building initrd with mkosi (docker)...")
    tools_tree = f"/work/mkosi.output/tools/{cfg.arch}"
    modules_tree = f"/work/mkosi.output/kernel/{cfg.kernel_version}/{cfg.arch}/modules"
    output_dir = f"/work/mkosi.output/initramfs/{cfg.kernel_version}/{cfg.arch}"
    docker.run_mkosi(
        cfg,
        f"--extra-tree={tools_tree}",
        f"--extra-tree={modules_tree}",
        f"--output-dir={output_dir}",
        "--package-cache-dir=/cache/packages",
        "build",
        *mkosi_args,
    )
    docker.fix_docker_ownership(
        cfg,
        [
            f"/work/mkosi.output/initramfs/{cfg.kernel_version}/{cfg.arch}",
            "/work/out",
        ],
    )


def _build_iso_stage(cfg: Config) -> None:
    """Run the ISO build stage according to *cfg.iso_mode*."""

    # --- skip ---------------------------------------------------------
    if cfg.iso_mode == "skip":
        log.info("ISO_MODE=skip — skipping ISO build")
        return

    # --- idempotency --------------------------------------------------
    iso_path = cfg.iso_output / f"captainos-{cfg.kernel_version}-{cfg.arch_info.output_arch}.iso"
    if iso_path.is_file() and not cfg.force_iso:
        log.info("ISO already built: %s (use --force-iso to rebuild)", iso_path)
        return

    # --- native -------------------------------------------------------
    if cfg.iso_mode == "native":
        log.info("Building ISO (native)...")
        iso.build(cfg)
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg)
    log.info("Building ISO (docker)...")
    docker.run_in_builder(
        cfg,
        "--entrypoint",
        "/usr/bin/uv",
        cfg.builder_image,
        *(["--verbose"] if logging.getLogger().isEnabledFor(logging.DEBUG) else []),
        "run",
        "/work/build.py",
        "iso",
    )
    docker.fix_docker_ownership(
        cfg,
        [
            "/work/mkosi.output/iso",
            "/work/out",
        ],
    )
