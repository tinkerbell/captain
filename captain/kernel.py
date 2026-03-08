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
import urllib.error
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
    _log.log(f"  URL: {url}")
    ensure_dir(dest_dir)
    try:
        _urlretrieve_with_timeout(url, tarball, reporthook=_progress_hook)
    except urllib.error.HTTPError as exc:
        print()  # newline after progress
        _log.err(f"Download failed: {exc} — {url}")
        raise SystemExit(1) from None
    except urllib.error.URLError as exc:
        print()  # newline after progress
        _log.err(f"Download failed: {exc.reason} — {url}")
        raise SystemExit(1) from None
    print()  # newline after progress

    _log.log("Extracting kernel source...")
    with tarfile.open(tarball, "r:xz") as tf:
        safe_extractall(tf, path=dest_dir)
    tarball.unlink()

    return src_dir


def _kernel_branch(version: str) -> str:
    """Derive the stable branch prefix from a full kernel version.

    ``"6.18.16"`` → ``"6.18.y"``
    """
    parts = version.split(".")
    if len(parts) < 2:
        _log.err(f"Invalid kernel version format: {version}")
        raise SystemExit(1)
    return f"{parts[0]}.{parts[1]}.y"


def _find_defconfig(cfg: Config) -> Path:
    """Locate the defconfig for the current kernel version and architecture.

    When ``cfg.kernel_config`` is set, that path is used directly.
    Otherwise returns ``kernel.configs/{major}.{minor}.y.{arch}``.
    Exits with a helpful error if no matching config file is found.
    """
    if cfg.kernel_config:
        explicit = Path(cfg.kernel_config)
        if not explicit.is_absolute():
            explicit = cfg.project_dir / explicit
        if explicit.is_file():
            return explicit
        _log.err(f"Kernel config not found: {explicit}")
        raise SystemExit(1)

    ai = cfg.arch_info
    branch = _kernel_branch(cfg.kernel_version)
    defconfig = cfg.project_dir / "kernel.configs" / f"{branch}.{ai.arch}"
    if defconfig.is_file():
        return defconfig

    # List available branches for a helpful error message.
    configs_dir = cfg.project_dir / "kernel.configs"
    available = sorted(
        {
            p.name.rsplit(".", 1)[0]
            for p in configs_dir.glob(f"*.{ai.arch}")
            if not p.name.startswith(".")
        }
    )
    avail_str = ", ".join(available) if available else "(none)"
    _log.err(
        f"No kernel config found for {branch} on {ai.arch}\n"
        f"    Expected: {defconfig}\n"
        f"    Available branches for {ai.arch}: {avail_str}"
    )
    raise SystemExit(1)


def configure_kernel(cfg: Config, src_dir: Path) -> None:
    """Apply defconfig and run olddefconfig."""
    ai = cfg.arch_info
    defconfig = _find_defconfig(cfg)

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    _log.log(f"Using defconfig: {defconfig}")
    shutil.copy2(defconfig, src_dir / ".config")
    run(["make", "olddefconfig"], env=make_env, cwd=src_dir)
    # Save the resolved config for debugging
    branch = _kernel_branch(cfg.kernel_version)
    resolved = cfg.project_dir / "kernel.configs" / f".config.resolved.{branch}.{ai.arch}"
    shutil.copy2(src_dir / ".config", resolved)
    _log.log(f"Resolved config saved to kernel.configs/.config.resolved.{branch}.{ai.arch}")

    # Increase COMMAND_LINE_SIZE on x86_64 (Tinkerbell needs large cmdlines)
    if ai.kernel_arch == "x86_64":
        _log.log("Increasing COMMAND_LINE_SIZE to 4096 (x86_64)...")
        setup_h = src_dir / "arch" / "x86" / "include" / "asm" / "setup.h"
        text = setup_h.read_text()
        new_text = re.sub(
            r"#define COMMAND_LINE_SIZE\s+2048",
            "#define COMMAND_LINE_SIZE 4096",
            text,
        )
        if new_text == text:
            _log.warn("COMMAND_LINE_SIZE patch did not match — the kernel default may have changed")
        setup_h.write_text(new_text)


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
    """Install modules and vmlinuz into mkosi.output/kernel/{version}/{arch}/."""
    ai = cfg.arch_info
    modules_root = cfg.modules_output

    make_env = {"ARCH": ai.kernel_arch}
    if ai.cross_compile:
        make_env["CROSS_COMPILE"] = ai.cross_compile

    # Install modules into the modules subtree.
    # make modules_install writes to {INSTALL_MOD_PATH}/lib/modules/{kver}/.
    _log.log("Installing modules...")
    run(
        ["make", f"INSTALL_MOD_PATH={modules_root}", "modules_install"],
        env=make_env,
        cwd=src_dir,
    )

    # Strip debug symbols from modules
    _log.log("Stripping debug symbols from modules...")
    strip_cmd = f"{ai.strip_prefix}strip"
    for ko in modules_root.rglob("*.ko"):
        run([strip_cmd, "--strip-unneeded", str(ko)], check=False)

    # Compress modules with zstd (the defconfig sets CONFIG_MODULE_COMPRESS_ZSTD
    # and CONFIG_MODULE_DECOMPRESS so the kernel can load .ko.zst at runtime).
    # We compress explicitly here because the build container's modules_install
    # may not always invoke zstd, and stripping must happen before compression.
    _log.log("Compressing kernel modules with zstd...")
    for ko in modules_root.rglob("*.ko"):
        run(["zstd", "--rm", "-q", "-19", str(ko)], check=True)

    # Clean up build/source symlinks
    mod_base = modules_root / "lib" / "modules" / built_kver
    (mod_base / "build").unlink(missing_ok=True)
    (mod_base / "source").unlink(missing_ok=True)

    # Move modules from /lib/modules to /usr/lib/modules (merged-usr)
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
        # Remove /lib tree
        shutil.rmtree(modules_root / "lib", ignore_errors=True)

    # Regenerate module dependency metadata for the compressed .ko.zst files.
    _log.log("Running depmod for compressed modules...")
    run(
        ["depmod", "-a", "-b", str(modules_root / "usr"), built_kver],
        check=True,
    )

    # Place vmlinuz alongside modules under kernel_output.  iPXE loads
    # the kernel image separately — it must NOT end up in the initramfs.
    kernel_image = src_dir / ai.kernel_image_path
    vmlinuz_dir = ensure_dir(cfg.kernel_output)

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
    _log.log(f"    Output:  {cfg.kernel_output}")


def build(cfg: Config) -> None:
    """Full kernel build pipeline — download, configure, build, install."""
    # Clean previous kernel output to ensure idempotency.
    # Only the kernel directory is wiped — tools are left intact.
    if cfg.kernel_output.exists():
        shutil.rmtree(cfg.kernel_output)
    ensure_dir(cfg.kernel_output)

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
