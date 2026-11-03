"""Build stage orchestration — tools, mkosi, ISO."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from captain import builder, cache, docker, iso, kernel, tools
from captain.config import Config
from captain.flavor import Flavor
from captain.util import check_kernel_dependencies, check_mkosi_dependencies, run

log = logging.getLogger(__name__)


def build_kernel_stage(cfg: Config) -> None:
    """Run the kernel build stage according to *cfg.kernel_mode*."""
    log.debug("Building kernel stage in mode %s", cfg.kernel_mode)

    # --- skip ---------------------------------------------------------
    if cfg.kernel_mode == "skip":
        log.info("KERNEL_MODE=skip — skipping kernel build")
        return

    # --- cache --------------------------------------------------------
    # menuconfig is interactive and never produces a build; keep it out of the cache.
    key = cache.kernel_key(cfg)
    if cfg.kernel_menuconfig:
        if cfg.kernel_output.is_dir():
            shutil.rmtree(cfg.kernel_output)
    elif cache.restore(cfg, key, cfg.kernel_output, force=cfg.force_kernel):
        return
    else:
        cache.require_build(cfg, key)

    # --- native -------------------------------------------------------
    if cfg.kernel_mode == "native":
        missing = check_kernel_dependencies(cfg.arch)
        if missing:
            log.error("Missing kernel build tools: %s", ", ".join(missing))
            log.error("Install them or set --kernel-mode=docker.")
            raise SystemExit(1)
        log.info("Building kernel (native)...")
        kernel.build(cfg)
        cache.save(cfg, key, cfg.kernel_output)
        return

    # --- docker -------------------------------------------------------
    builder.obtain_builder(cfg)
    log.info("Building kernel (docker relaunch)...")
    if cfg.kernel_menuconfig:
        log.info("Launching interactive kernel configuration (menuconfig)...")
        docker.run_captain_in_builder(cfg, ["kernel"], extra_docker_args=["-i", "-t"])
    else:
        docker.run_captain_in_builder(cfg, ["kernel"])
    docker.fix_docker_ownership(
        cfg,
        [
            f"/work/mkosi.input/kernel/{cfg.arch}",
            "/work/out",
        ],
    )
    if not cfg.kernel_menuconfig:
        cache.save(cfg, key, cfg.kernel_output)


def _build_tools_stage(cfg: Config) -> None:
    """Run the tools download stage according to *cfg.tools_mode*."""
    log.debug("Building tools stage in mode %s", cfg.tools_mode)

    # --- skip ---------------------------------------------------------
    if cfg.tools_mode == "skip":
        log.info("TOOLS_MODE=skip — skipping tools download")
        return

    # --- cache --------------------------------------------------------
    key = cache.tools_key(cfg)
    if cache.restore(cfg, key, cfg.tools_output, force=cfg.force_tools):
        return
    cache.require_build(cfg, key)

    # --- native -------------------------------------------------------
    if cfg.tools_mode == "native":
        log.info("Downloading tools (nerdctl, containerd, etc.) native...")
        tools.download_all(cfg)
    # --- docker -------------------------------------------------------
    else:
        builder.obtain_builder(cfg)
        log.info("Downloading tools (nerdctl, containerd, etc.) docker...")
        docker.run_captain_in_builder(cfg, ["tools"])
        docker.fix_docker_ownership(cfg, ["/work/mkosi.input"])
    cache.save(cfg, key, cfg.tools_output)


def _build_mkosi_stage(cfg: Config, extra_args: list[str]) -> None:
    """Run the mkosi image-assembly stage according to *cfg.mkosi_mode*.

    Always builds: the caller deals with skip mode and the build cache, since a
    cache hit also makes the tools and kernel stages unnecessary.
    """
    mkosi_args = list(cfg.mkosi_args) + list(extra_args)

    # Use a Rich Syntax and Rich Panel to display mkosi.conf before running it if debugging:
    if log.isEnabledFor(logging.DEBUG):
        from rich import print
        from rich.panel import Panel
        from rich.syntax import Syntax

        for mkosi_conf_path in sorted(cfg.generated_dir.glob("**/mkosi.conf")):
            syntax = Syntax(
                mkosi_conf_path.read_text(),
                "ini",
                theme="monokai",
                line_numbers=True,
                background_color="default",
            )
            title = str(mkosi_conf_path.relative_to(cfg.generated_dir))
            print(Panel(syntax, title=title, border_style="blue"))

    # --- native -------------------------------------------------------
    if cfg.mkosi_mode == "native":
        missing = check_mkosi_dependencies()
        if missing:
            log.error("Missing mkosi tools: %s", ", ".join(missing))
            log.error("Install them or set --mkosi-mode=docker.")
            raise SystemExit(1)
        log.info("Building initrd with mkosi (native)...")
        tools_tree = str(cfg.tools_output)
        output_dir = str(cfg.initramfs_output)
        run(
            [
                "mkosi",
                f"--directory={cfg.generated_dir}",
                f"--architecture={cfg.arch_info.mkosi_arch}",
                f"--extra-tree={tools_tree}",
                f"--output-dir={output_dir}",
                "build",
                *mkosi_args,
            ],
            cwd=cfg.project_dir,
        )
        return

    # --- docker -------------------------------------------------------
    builder.obtain_builder(cfg)
    log.info("Building initrd with mkosi (docker)...")
    tools_tree = f"/work/mkosi.input/tools/{cfg.arch}"  # @TODO ooops
    output_dir = cfg.calc_initramfs_output(Path("/work"), cfg.output_kind)
    docker.run_mkosi_in_builder(
        cfg,
        "--directory=/work/mkosi.generated",
        f"--extra-tree={tools_tree}",
        f"--output-dir={output_dir}",
        "--package-cache-dir=/cache/packages",
        "build",
        *mkosi_args,
    )
    docker.fix_docker_ownership(
        cfg,
        [
            f"/work/mkosi.output/{cfg.output_kind}/{cfg.flavor_id}/{cfg.arch}",
            "/work/out",
        ],
    )


def _build_iso_stage(cfg: Config, flavor: Flavor) -> None:
    """Run the ISO build stage according to *cfg.iso_mode*."""

    # --- skip ---------------------------------------------------------
    if cfg.iso_mode == "skip":
        log.info("ISO_MODE=skip — skipping ISO build")
        return

    # --- cache --------------------------------------------------------
    key = cache.iso_key(cfg, flavor)
    if cache.restore(cfg, key, cfg.iso_output, force=cfg.force_iso):
        return
    cache.require_build(cfg, key)

    # --- native -------------------------------------------------------
    if cfg.iso_mode == "native":
        log.info("Building ISO (native)...")
        iso.build(cfg)
    # --- docker -------------------------------------------------------
    else:
        builder.obtain_builder(cfg)
        log.info("Building ISO (docker)...")
        docker.run_captain_in_builder(cfg, ["iso"])
        docker.fix_docker_ownership(
            cfg,
            [
                "/work/mkosi.output/iso",
                "/work/out",
            ],
        )
    # staging/ is just grub-mkrescue's input
    cache.save(cfg, key, cfg.iso_output, exclude=["staging"])
