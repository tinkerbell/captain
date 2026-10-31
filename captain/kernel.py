"""Kernel download, configuration, compilation, and packaging.

Heavy lifting (make) is still done via subprocess — only the
orchestration is in Python.  Called directly by ``stages.build_kernel_stage``
in both native and Docker modes (inside the container we re-enter
the CLI with all modes forced to native).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from captain import progress
from captain.config import Config
from captain.util import ensure_dir, run, safe_extractall

KERNEL_BUILD_BASE_PATH = "/work/kernel-build"

log = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 60  # seconds


def _download_with_progress(url: str, filename: Path) -> None:
    """Download *url* to *filename* with a Rich progress bar."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length", 0)) or None
        with progress.transfer("    Downloading", total) as advance, open(filename, "wb") as out:
            while buf := resp.read(1 << 20):
                out.write(buf)
                advance(len(buf))


def download_kernel(cfg: Config, dest_dir: Path) -> Path:
    """Download and extract a kernel tarball.  Returns the source directory."""
    src_dir = dest_dir / f"linux-{cfg.kernel_version}"  # predict kernel tarball 1st-level dir name
    if src_dir.is_dir():
        log.info("Using cached kernel source at %s", src_dir)
        return src_dir

    major = cfg.kernel_version.split(".")[0]
    url = f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/linux-{cfg.kernel_version}.tar.xz"
    tarball = dest_dir / f"linux-{cfg.kernel_version}.tar.xz"

    log.info("Downloading kernel %s...", cfg.kernel_version)
    log.info("  URL: %s", url)
    ensure_dir(dest_dir)
    try:
        _download_with_progress(url, tarball)
    except urllib.error.HTTPError as exc:
        log.error("Download failed: %s — %s", exc, url)
        raise SystemExit(1) from None
    except urllib.error.URLError as exc:
        log.error("Download failed: %s — %s", exc.reason, url)
        raise SystemExit(1) from None

    log.info("Extracting kernel source...")
    with tarfile.open(tarball, "r:xz") as tf:
        safe_extractall(tf, path=dest_dir)
    tarball.unlink()

    return src_dir


def _kernel_branch(version: str) -> str:
    """Derive the stable branch prefix from a full kernel version."""
    parts = version.split(".")
    if len(parts) < 2:
        log.error("Invalid kernel version format: %s", version)
        raise SystemExit(1)
    return f"{parts[0]}.{parts[1]}.y"


def find_defconfig(cfg: Config) -> Path:
    """Locate the defconfig for the current kernel version and architecture."""
    ai = cfg.arch_info
    branch = _kernel_branch(cfg.kernel_version)
    defconfig = cfg.project_dir / "kernel.configs" / f"{branch}.{ai.arch}"
    if defconfig.is_file():
        return defconfig

    configs_dir = cfg.project_dir / "kernel.configs"
    available = sorted(
        {
            p.name.rsplit(".", 1)[0]
            for p in configs_dir.glob(f"*.{ai.arch}")
            if not p.name.startswith(".")
        }
    )
    avail_str = ", ".join(available) if available else "(none)"
    log.error(
        "No kernel config found for %s on %s\n    Expected: %s\n    Available branches for %s: %s",
        branch,
        ai.arch,
        defconfig,
        ai.arch,
        avail_str,
    )
    raise SystemExit(1)


def configure_kernel(cfg: Config, src_dir: Path) -> None:
    """Apply defconfig and run olddefconfig."""
    ai = cfg.arch_info
    defconfig = find_defconfig(cfg)

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    log.info("Using defconfig: %s", defconfig)
    shutil.copy2(defconfig, src_dir / ".config")
    run(["make", "olddefconfig"], env=make_env, cwd=src_dir)

    log.debug(
        "Considering whether to launch menuconfig for manual config tweaks... %s",
        cfg.kernel_menuconfig,
    )

    if cfg.kernel_menuconfig:
        log.info("Launching menuconfig...")
        run(["make", "menuconfig"], env=make_env, cwd=src_dir)

        # run `make savedefconfig`
        log.info("Saving modified config...")
        run(["make", "savedefconfig"], env=make_env, cwd=src_dir)

        # copy the 'defconfig' back to the source - ready to commit
        shutil.copy(src_dir / "defconfig", defconfig)
        log.info("Updated defconfig saved to %s", defconfig)

        raise SystemExit(0)  # exit after menuconfig - no build

    branch = _kernel_branch(cfg.kernel_version)
    resolved = cfg.project_dir / "kernel.configs" / f".config.resolved.{branch}.{ai.arch}"
    shutil.copy2(src_dir / ".config", resolved)
    log.info("Resolved config saved to kernel.configs/.config.resolved.%s.%s", branch, ai.arch)

    if ai.kernel_arch == "x86_64":
        log.info("Increasing COMMAND_LINE_SIZE to 4096 (x86_64)...")
        setup_h = src_dir / "arch" / "x86" / "include" / "asm" / "setup.h"
        text = setup_h.read_text()
        new_text = re.sub(
            r"#define COMMAND_LINE_SIZE\s+2048",
            "#define COMMAND_LINE_SIZE 4096",
            text,
        )
        if new_text == text:
            log.warning(
                "COMMAND_LINE_SIZE patch did not match — the kernel default may have changed"
            )
        setup_h.write_text(new_text)


def build_kernel(cfg: Config, src_dir: Path) -> str:
    """Compile the kernel image and modules.  Returns the built kernel version string."""
    ai = cfg.arch_info
    nproc = os.cpu_count() or 1

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile
        make_env["DPKG_DEB_COMPRESSOR_TYPE"] = "none"  # don't waste time compressing .deb

    if cfg.kernel_clean:
        log.info("Cleaning kernel...")
        run(
            ["make", f"-j{nproc}", "clean"],
            env=make_env,
            cwd=src_dir,
        )

    built_kver = run(
        ["make", "-s", "kernelrelease"],
        env={"ARCH": ai.kernel_arch},
        capture=True,
        cwd=src_dir,
    ).stdout.strip()

    log.info("Building kernel '%s' with %d jobs...", built_kver, nproc)
    run(
        ["make", f"-j{nproc}", "bindeb-pkg"],
        env=make_env,
        cwd=src_dir,
    )

    log.info("Built kernel version: %s", built_kver)
    return built_kver


def obtain_target_artifact_path(cfg, ensure_parent: bool = False) -> Path:
    from captain.cache import kernel_key

    if ensure_parent:
        ensure_dir(cfg.kernel_output)

    # The build-cache key covers defconfig, this module and the toolchain (Dockerfile).
    version_hash_shorten = kernel_key(cfg).digest[:8]

    return (
        cfg.kernel_output / f"linux-image-{cfg.kernel_version}-captainos"
        f"_{cfg.kernel_version}-1-{version_hash_shorten}_"
        f"{cfg.arch_info.kernel_arch}.deb"
    )


def deploy_deb_to_output(cfg: Config, build_dir: Path, built_kver: str) -> None:
    # Find the newest file in build_dir matching linux-image-{built_kver}*.deb
    image_deb_package = build_dir.glob(f"linux-image-{built_kver}*.deb")
    image_deb_package = sorted(image_deb_package, key=lambda p: p.stat().st_mtime, reverse=True)
    if not image_deb_package:
        log.error("No linux-image-*.deb package found in %s", build_dir)
        raise SystemExit(1)
    image_deb_package = image_deb_package[0]
    log.info("Found built kernel package: %s", image_deb_package)

    # copy the deb package to the output directory
    target_deb_output = obtain_target_artifact_path(cfg, ensure_parent=True)
    log.debug("Copying built kernel package from %s to %s", image_deb_package, target_deb_output)
    shutil.copy2(image_deb_package, target_deb_output)

    # Show dpkg info for the copied package
    log.info("Kernel package info:")
    run(["dpkg", "-I", str(target_deb_output)])

    log.info("Kernel build complete:")
    log.info(
        "    linux-image:   %s (%.1fM)",
        target_deb_output,
        (target_deb_output.stat().st_size / (1024 * 1024)),
    )


def build(cfg: Config) -> None:
    """Full kernel build pipeline — download, configure, build, install."""
    if cfg.kernel_output.exists():
        shutil.rmtree(cfg.kernel_output)
    ensure_dir(cfg.kernel_output)

    log.info("Preparing kernel source...")
    # We've a 2-deep because make bindeb-pkg outputs to parent directory of source proper
    # Also because one can't share cache between architectures -- full rebuild everytime otherwise
    build_dir = Path(KERNEL_BUILD_BASE_PATH) / f"build-{cfg.arch_info.kernel_arch}"
    src_dir = download_kernel(cfg, build_dir)  # this is one down from build_dir

    log.debug("Kernel source directory: %s", src_dir)

    log.info("Configuring kernel...")
    configure_kernel(cfg, src_dir)

    log.info("Building kernel...")
    built_kver = build_kernel(cfg, src_dir)
    log.debug("Built kernel version: %s", built_kver)

    log.info("Installing kernel...")
    deploy_deb_to_output(cfg, build_dir, built_kver)
