"""Kernel download, configuration, compilation, and installation.

Heavy lifting (make, strip) is still done via subprocess — only the
orchestration is in Python.  Called directly by ``cli._build_kernel_stage``
in both native and Docker modes (inside the container ``build.py kernel``
re-enters via the CLI with all modes forced to native).
"""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import urllib.request
from pathlib import Path

from captain.config import Config
from captain.log import for_stage
from captain.util import ensure_dir, run, safe_extractall

_log = for_stage("kernel")

_DOWNLOAD_TIMEOUT = 60  # seconds


def _urlretrieve_with_timeout(
    url: str,
    filename: Path | str,
    *,
    reporthook: object = None,
    timeout: int = _DOWNLOAD_TIMEOUT,
) -> None:
    """Like urllib.request.urlretrieve but with a socket timeout."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = resp.info()
        total = int(headers.get("Content-Length", -1))
        block_size = 8192
        block_num = 0
        with open(filename, "wb") as out:
            while True:
                buf = resp.read(block_size)
                if not buf:
                    break
                out.write(buf)
                block_num += 1
                if reporthook is not None:
                    reporthook(block_num, block_size, total)  # type: ignore[operator]


def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    """Simple download progress indicator."""
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb = downloaded / (1024 * 1024)
        total_mb = total_size / (1024 * 1024)
        print(f"\r    {mb:.1f}/{total_mb:.1f} MB ({pct}%)", end="", flush=True)
    else:
        mb = downloaded / (1024 * 1024)
        print(f"\r    {mb:.1f} MB", end="", flush=True)


def download_kernel(version: str, dest_dir: Path) -> Path:
    """Download and extract a kernel tarball.  Returns the source directory."""
    src_dir = dest_dir / f"linux-{version}"
    if src_dir.is_dir():
        _log.log(f"Using cached kernel source at {src_dir}")
        return src_dir

    major = version.split(".")[0]
    url = f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/linux-{version}.tar.xz"
    tarball = dest_dir / f"linux-{version}.tar.xz"

    _log.log(f"Downloading kernel {version}...")
    ensure_dir(dest_dir)
    _urlretrieve_with_timeout(url, tarball, reporthook=_progress_hook)
    print()  # newline after progress

    _log.log("Extracting kernel source...")
    with tarfile.open(tarball, "r:xz") as tf:
        safe_extractall(tf, path=dest_dir)
    tarball.unlink()

    return src_dir


def configure_kernel(cfg: Config, src_dir: Path) -> None:
    """Apply defconfig and run olddefconfig."""
    ai = cfg.arch_info
    defconfig = cfg.project_dir / "config" / f"defconfig.{ai.arch}"

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    if defconfig.is_file():
        _log.log(f"Using defconfig: {defconfig}")
        shutil.copy2(defconfig, src_dir / ".config")
        run(["make", "olddefconfig"], env=make_env, cwd=src_dir)
        # Save the resolved config for debugging
        resolved = cfg.project_dir / "config" / f".config.resolved.{ai.arch}"
        shutil.copy2(src_dir / ".config", resolved)
        _log.log(f"Resolved config saved to config/.config.resolved.{ai.arch}")
    else:
        _log.log(f"No defconfig found at {defconfig}, using default")
        run(["make", "defconfig"], env=make_env, cwd=src_dir)

    # Increase COMMAND_LINE_SIZE on x86_64 (Tinkerbell needs large cmdlines)
    if ai.kernel_arch == "x86_64":
        _log.log("Increasing COMMAND_LINE_SIZE to 4096 (x86_64)...")
        setup_h = src_dir / "arch" / "x86" / "include" / "asm" / "setup.h"
        text = setup_h.read_text()
        text = re.sub(
            r"#define COMMAND_LINE_SIZE\s+2048",
            "#define COMMAND_LINE_SIZE 4096",
            text,
        )
        setup_h.write_text(text)


def build_kernel(cfg: Config, src_dir: Path) -> str:
    """Compile the kernel image and modules.  Returns the built kernel version string."""
    ai = cfg.arch_info
    nproc = os.cpu_count() or 1

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    _log.log(f"Building kernel with {nproc} jobs...")
    run(
        ["make", f"-j{nproc}", ai.image_target, "modules"],
        env=make_env,
        cwd=src_dir,
    )

    # Determine actual kernel version from build
    result = run(
        ["make", "-s", "kernelrelease"],
        env={"ARCH": ai.kernel_arch},
        capture=True,
        cwd=src_dir,
    )
    built_kver = result.stdout.strip()
    _log.log(f"Built kernel version: {built_kver}")
    return built_kver


def install_kernel(cfg: Config, src_dir: Path, built_kver: str) -> None:
    """Install modules and kernel image into mkosi.output/extra-tree/{arch}/."""
    ai = cfg.arch_info
    kernel_output = cfg.extra_tree_output

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    # Install modules
    _log.log("Installing modules...")
    run(
        ["make", f"INSTALL_MOD_PATH={kernel_output}", "modules_install"],
        env=make_env,
        cwd=src_dir,
    )

    # Strip debug symbols from modules
    _log.log("Stripping debug symbols from modules...")
    strip_cmd = f"{ai.strip_prefix}strip"
    for ko in kernel_output.rglob("*.ko"):
        run([strip_cmd, "--strip-unneeded", str(ko)], check=False)

    # Compress modules with zstd (the defconfig sets CONFIG_MODULE_COMPRESS_ZSTD
    # and CONFIG_MODULE_DECOMPRESS so the kernel can load .ko.zst at runtime).
    # We compress explicitly here because the build container's modules_install
    # may not always invoke zstd, and stripping must happen before compression.
    _log.log("Compressing kernel modules with zstd...")
    for ko in kernel_output.rglob("*.ko"):
        run(["zstd", "--rm", "-q", "-19", str(ko)], check=True)

    # Clean up build/source symlinks
    mod_base = kernel_output / "lib" / "modules" / built_kver
    (mod_base / "build").unlink(missing_ok=True)
    (mod_base / "source").unlink(missing_ok=True)

    # Move modules from /lib/modules to /usr/lib/modules (merged-usr)
    usr_moddir = ensure_dir(kernel_output / "usr" / "lib" / "modules" / built_kver)
    if mod_base.is_dir():
        for item in mod_base.iterdir():
            dest = usr_moddir / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        # Remove /lib tree
        shutil.rmtree(kernel_output / "lib", ignore_errors=True)

    # Regenerate module dependency metadata for the compressed .ko.zst files.
    _log.log("Running depmod for compressed modules...")
    run(
        ["depmod", "-a", "-b", str(kernel_output / "usr"), built_kver],
        check=True,
    )

    # Place vmlinuz *outside* the ExtraTrees path so it does NOT end up
    # inside the initramfs CPIO.  iPXE loads the kernel separately.
    kernel_image = src_dir / ai.kernel_image_path
    vmlinuz_dir = ensure_dir(cfg.vmlinuz_output)

    # Remove stale vmlinuz images from prior builds so artifact collection
    # never picks an outdated kernel.
    for old in vmlinuz_dir.glob("vmlinuz-*"):
        old.unlink(missing_ok=True)

    shutil.copy2(kernel_image, vmlinuz_dir / f"vmlinuz-{built_kver}")

    _log.log("Kernel build complete:")
    vmlinuz = vmlinuz_dir / f"vmlinuz-{built_kver}"
    vmlinuz_size = vmlinuz.stat().st_size / (1024 * 1024)
    _log.log(f"    Image:   {vmlinuz} ({vmlinuz_size:.1f}M)")
    _log.log(f"    Modules: {usr_moddir}/")
    _log.log(f"    Version: {built_kver}")
    _log.log(f"    Output:  {kernel_output}")


def build(cfg: Config) -> None:
    """Full kernel build pipeline — download, configure, build, install."""
    # Clean previous output to ensure idempotency
    if cfg.extra_tree_output.exists():
        shutil.rmtree(cfg.extra_tree_output)
    ensure_dir(cfg.extra_tree_output)

    if cfg.vmlinuz_output.exists():
        shutil.rmtree(cfg.vmlinuz_output)

    build_dir = Path("/var/tmp/kernel-build")

    # Obtain kernel source
    if cfg.kernel_src and Path(cfg.kernel_src).is_dir():
        _log.log(f"Using provided kernel source at {cfg.kernel_src}")
        src_dir = Path(cfg.kernel_src)
    else:
        src_dir = download_kernel(cfg.kernel_version, build_dir)

    configure_kernel(cfg, src_dir)
    built_kver = build_kernel(cfg, src_dir)
    install_kernel(cfg, src_dir, built_kver)
