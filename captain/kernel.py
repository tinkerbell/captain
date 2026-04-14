"""Kernel download, configuration, compilation, and installation.

Heavy lifting (make, strip) is still done via subprocess — only the
orchestration is in Python.  Called directly by ``cli._build_kernel_stage``
in both native and Docker modes (inside the container ``build.py kernel``
re-enters via the CLI with all modes forced to native).
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

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from captain import console
from captain.config import Config
from captain.util import ensure_dir, run, safe_extractall

log = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 60  # seconds


def _download_with_progress(url: str, filename: Path) -> None:
    """Download *url* to *filename* with a Rich progress bar."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length", 0)) or None
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("    Downloading", total=total)
            with open(filename, "wb") as out:
                while True:
                    buf = resp.read(8192)
                    if not buf:
                        break
                    out.write(buf)
                    progress.update(task, advance=len(buf))


def download_kernel(version: str, dest_dir: Path) -> Path:
    """Download and extract a kernel tarball.  Returns the source directory."""
    src_dir = dest_dir / f"linux-{version}"
    if src_dir.is_dir():
        log.info("Using cached kernel source at %s", src_dir)
        return src_dir

    major = version.split(".")[0]
    url = f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/linux-{version}.tar.xz"
    tarball = dest_dir / f"linux-{version}.tar.xz"

    log.info("Downloading kernel %s...", version)
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


def _find_defconfig(cfg: Config) -> Path:
    """Locate the defconfig for the current kernel version and architecture."""
    if cfg.kernel_config:
        explicit = Path(cfg.kernel_config)
        if not explicit.is_absolute():
            explicit = cfg.project_dir / explicit
        if explicit.is_file():
            return explicit
        log.error("Kernel config not found: %s", explicit)
        raise SystemExit(1)

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
    defconfig = _find_defconfig(cfg)

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    log.info("Using defconfig: %s", defconfig)
    shutil.copy2(defconfig, src_dir / ".config")
    run(["make", "olddefconfig"], env=make_env, cwd=src_dir)
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

    log.info("Building kernel with %d jobs...", nproc)
    run(
        ["make", f"-j{nproc}", ai.image_target, "modules"],
        env=make_env,
        cwd=src_dir,
    )

    result = run(
        ["make", "-s", "kernelrelease"],
        env={"ARCH": ai.kernel_arch},
        capture=True,
        cwd=src_dir,
    )
    built_kver = result.stdout.strip()
    log.info("Built kernel version: %s", built_kver)
    return built_kver


def install_kernel(cfg: Config, src_dir: Path, built_kver: str) -> None:
    """Install modules and vmlinuz into mkosi.output/kernel/{version}/{arch}/."""
    ai = cfg.arch_info
    modules_root = cfg.modules_output

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    log.info("Installing modules...")
    run(
        ["make", f"INSTALL_MOD_PATH={modules_root}", "modules_install"],
        env=make_env,
        cwd=src_dir,
    )

    log.info("Stripping debug symbols from modules...")
    strip_cmd = f"{ai.strip_prefix}strip"
    for ko in modules_root.rglob("*.ko"):
        run([strip_cmd, "--strip-unneeded", str(ko)], check=False)

    log.info("Compressing kernel modules with zstd...")
    for ko in modules_root.rglob("*.ko"):
        run(["zstd", "--rm", "-q", "-19", str(ko)], check=True)

    mod_base = modules_root / "lib" / "modules" / built_kver
    (mod_base / "build").unlink(missing_ok=True)
    (mod_base / "source").unlink(missing_ok=True)

    usr_moddir = ensure_dir(modules_root / "usr" / "lib" / "modules" / built_kver)
    if mod_base.is_dir():
        for item in mod_base.iterdir():
            dest = usr_moddir / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        shutil.rmtree(modules_root / "lib", ignore_errors=True)

    log.info("Running depmod for compressed modules...")
    run(
        ["depmod", "-a", "-b", str(modules_root / "usr"), built_kver],
        check=True,
    )

    kernel_image = src_dir / ai.kernel_image_path
    vmlinuz_dir = ensure_dir(cfg.kernel_output)

    for old in vmlinuz_dir.glob("vmlinuz-*"):
        old.unlink(missing_ok=True)

    shutil.copy2(kernel_image, vmlinuz_dir / f"vmlinuz-{built_kver}")

    log.info("Kernel build complete:")
    vmlinuz = vmlinuz_dir / f"vmlinuz-{built_kver}"
    vmlinuz_size = vmlinuz.stat().st_size / (1024 * 1024)
    log.info("    Image:   %s (%.1fM)", vmlinuz, vmlinuz_size)
    log.info("    Modules: %s/", usr_moddir)
    log.info("    Version: %s", built_kver)
    log.info("    Output:  %s", cfg.kernel_output)


def build(cfg: Config) -> None:
    """Full kernel build pipeline — download, configure, build, install."""
    if cfg.kernel_output.exists():
        shutil.rmtree(cfg.kernel_output)
    ensure_dir(cfg.kernel_output)

    build_dir = Path("/var/tmp/kernel-build")

    if cfg.kernel_src and Path(cfg.kernel_src).is_dir():
        log.info("Using provided kernel source at %s", cfg.kernel_src)
        src_dir = Path(cfg.kernel_src)
    else:
        src_dir = download_kernel(cfg.kernel_version, build_dir)

    configure_kernel(cfg, src_dir)
    built_kver = build_kernel(cfg, src_dir)
    install_kernel(cfg, src_dir, built_kver)
