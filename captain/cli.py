"""CLI entry point — single configargparse parser with pre-extracted subcommand.

Every configuration parameter is both a ``--cli-flag`` and an environment
variable, following the ff priority model:

    CLI args  >  environment variables  >  defaults

The subcommand (``build``, ``kernel``, ``tools``, …) is extracted from
``sys.argv`` *before* parsing so that flags work in any position::

    ./build.py --arch=arm64 kernel      # works
    ./build.py kernel --arch=arm64      # also works
    ARCH=arm64 ./build.py kernel        # also works
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import configargparse


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Clean help: word-wrapped text, raw epilog, no env-var refs,
    no defaults when the value is empty / None / False, and a short
    usage line."""

    def _get_help_string(self, action: argparse.Action) -> str:
        """Append ``(default: X)`` only when X is meaningful."""
        text = action.help or ""
        if action.default in (None, "", False, argparse.SUPPRESS):
            return text
        if "%(default)" not in text:
            text += " (default: %(default)s)"
        return text

    def _format_usage(
        self, usage: str | None, actions: list, groups: list, prefix: str | None,
    ) -> str:
        """Show ``usage: prog [flags]`` instead of enumerating every flag."""
        prog = self._prog
        return f"usage: {prog} [flags]\n\n"

from captain import artifacts, docker, iso, kernel, qemu, tools
from captain.config import Config
from captain.log import for_stage
from captain.util import check_kernel_dependencies, check_mkosi_dependencies, run

# ---------------------------------------------------------------------------
# Known subcommands (order matters for help text)
# ---------------------------------------------------------------------------

COMMANDS: dict[str, str] = {
    "build": "Run all build stages: kernel → tools → initramfs → iso (default)",
    "kernel": "Build only the kernel + modules",
    "tools": "Download tools (containerd, runc, nerdctl, CNI)",
    "initramfs": "Build only the initramfs via mkosi",
    "iso": "Build a UEFI-bootable ISO image",
    "shell": "Interactive shell inside the builder container",
    "clean": "Remove all build artifacts",
    "summary": "Print mkosi configuration summary",
    "qemu-test": "Boot the image in QEMU for testing",
}

VALID_MODES = ("docker", "native", "skip")

# Boolean (store_true) flags — these do NOT consume the next token as a value.
# Used by _extract_command to avoid treating a flag value as a subcommand.
_BOOLEAN_FLAGS = frozenset({
    "--force-kernel", "--force-tools", "--force-iso", "--force",
    "--no-cache", "-h", "--help",
})


def _extract_command(argv: list[str]) -> tuple[str, list[str]]:
    """Remove and return the first recognised subcommand from *argv*.

    Returns ``("build", argv)`` when no subcommand is found.

    The scanner skips tokens that are likely flag *values* (the token
    immediately after a ``--flag`` that is not boolean and does not use
    ``=`` syntax).  This prevents ``--builder-image build`` from
    incorrectly extracting ``build`` as the subcommand.
    """
    prev_was_value_flag = False
    for i, tok in enumerate(argv):
        if tok.startswith("-"):
            if "=" in tok:
                prev_was_value_flag = False
            else:
                # Boolean flags don't consume the next token.
                prev_was_value_flag = tok not in _BOOLEAN_FLAGS
            continue
        if prev_was_value_flag:
            # This token is the value of the preceding flag — skip it.
            prev_was_value_flag = False
            continue
        # Standalone positional token — check if it's a command.
        if tok in COMMANDS:
            return tok, argv[:i] + argv[i + 1 :]
        # Unknown positional token — not a recognised command.
        valid = ", ".join(COMMANDS)
        print(
            f"error: unknown command '{tok}'\n"
            f"valid commands: {valid}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return "build", list(argv)


def _build_parser(command: str) -> configargparse.ArgParser:
    """Construct a command-specific CLI parser.

    Only the flags relevant to *command* are added, so ``--help``
    shows a focused help message.
    """

    # -- description & epilog ------------------------------------------
    if command == "build":
        desc = "Build CaptainOS images. Stages: kernel → tools → initramfs → iso."
        commands_list = "\n".join(
            f"  {name:14s} {d}" for name, d in COMMANDS.items()
        )
        epilog = f"""\
commands:
{commands_list}
"""
    else:
        desc = COMMANDS.get(command, command)
        epilog = None

    # Adapt to the real terminal width so argparse wraps at word
    # boundaries instead of letting the terminal hard-wrap mid-word.
    # max_help_position=38 accommodates the widest flag+metavar.
    columns = shutil.get_terminal_size().columns

    parser = configargparse.ArgParser(
        prog=f"build.py {command}" if command != "build" else "build.py",
        description=desc,
        epilog=epilog,
        add_env_var_help=False,
        formatter_class=lambda prog: _HelpFormatter(
            prog, max_help_position=38, width=columns,
        ),
    )

    # -- Add only the flag groups relevant to this command -------------
    for adder in _COMMAND_FLAGS.get(command, []):
        adder(parser)

    return parser


# ---------------------------------------------------------------------------
# Flag-group helpers — each adds one argument group to the parser
# ---------------------------------------------------------------------------

def _add_common_flags(parser: configargparse.ArgParser) -> None:
    """--arch, --builder-image, --no-cache"""
    g = parser.add_argument_group("build configuration")
    g.add_argument(
        "--arch", env_var="ARCH", default="amd64",
        choices=["amd64", "arm64"], help="target architecture",
    )
    g.add_argument(
        "--builder-image", env_var="BUILDER_IMAGE", metavar="IMAGE",
        default="captainos-builder", help="Docker builder image name",
    )
    g.add_argument(
        "--no-cache", env_var="NO_CACHE", action="store_true",
        default=False, help="rebuild builder image without Docker cache",
    )


def _add_kernel_flags(parser: configargparse.ArgParser) -> None:
    """--kernel-version, --kernel-src, --kernel-mode, --force-kernel"""
    g = parser.add_argument_group("kernel")
    g.add_argument(
        "--kernel-version", env_var="KERNEL_VERSION", metavar="VER",
        default="6.12.69", help="kernel version to build",
    )
    g.add_argument(
        "--kernel-src", env_var="KERNEL_SRC", metavar="PATH", default=None,
        help="path to local kernel source tree",
    )
    g.add_argument(
        "--kernel-mode", env_var="KERNEL_MODE", default="docker",
        choices=list(VALID_MODES), help="kernel stage execution mode",
    )
    g.add_argument(
        "--force-kernel", env_var="FORCE_KERNEL", action="store_true",
        default=False, help="force kernel rebuild even if outputs exist",
    )


def _add_tools_flags(parser: configargparse.ArgParser) -> None:
    """--tools-mode, --force-tools"""
    g = parser.add_argument_group("tools")
    g.add_argument(
        "--tools-mode", env_var="TOOLS_MODE", default="docker",
        choices=list(VALID_MODES), help="tools stage execution mode",
    )
    g.add_argument(
        "--force-tools", env_var="FORCE_TOOLS", action="store_true",
        default=False, help="re-download tools even if outputs exist",
    )


def _add_mkosi_flags(parser: configargparse.ArgParser) -> None:
    """--mkosi-mode, --force (mkosi passthrough)"""
    g = parser.add_argument_group("initramfs (mkosi)")
    g.add_argument(
        "--mkosi-mode", env_var="MKOSI_MODE", default="docker",
        choices=list(VALID_MODES), help="mkosi stage execution mode",
    )
    g.add_argument(
        "--force", action="store_true", default=False,
        help="passed through to mkosi as --force",
    )


def _add_summary_flags(parser: configargparse.ArgParser) -> None:
    """--mkosi-mode only (no --force)."""
    g = parser.add_argument_group("mkosi")
    g.add_argument(
        "--mkosi-mode", env_var="MKOSI_MODE", default="docker",
        choices=list(VALID_MODES), help="mkosi stage execution mode",
    )


def _add_iso_flags(parser: configargparse.ArgParser) -> None:
    """--iso-mode, --force-iso"""
    g = parser.add_argument_group("iso")
    g.add_argument(
        "--iso-mode", env_var="ISO_MODE", default="docker",
        choices=list(VALID_MODES), help="iso stage execution mode",
    )
    g.add_argument(
        "--force-iso", env_var="FORCE_ISO", action="store_true",
        default=False, help="force ISO rebuild even if outputs exist",
    )


def _add_mode_flags(parser: configargparse.ArgParser) -> None:
    """All four --*-mode flags (used by 'shell' which checks needs_docker)."""
    g = parser.add_argument_group("stage modes")
    for stage in ("kernel", "tools", "mkosi", "iso"):
        g.add_argument(
            f"--{stage}-mode", env_var=f"{stage.upper()}_MODE",
            default="docker", choices=list(VALID_MODES),
            help=f"{stage} stage execution mode",
        )


def _add_qemu_flags(parser: configargparse.ArgParser) -> None:
    """--qemu-append, --qemu-mem, --qemu-smp"""
    g = parser.add_argument_group("qemu")
    g.add_argument(
        "--qemu-append", env_var="QEMU_APPEND", metavar="ARGS", default="",
        help="extra kernel cmdline args for qemu-test",
    )
    g.add_argument(
        "--qemu-mem", env_var="QEMU_MEM", metavar="SIZE", default="2G",
        help="QEMU RAM size",
    )
    g.add_argument(
        "--qemu-smp", env_var="QEMU_SMP", metavar="N", default="2",
        help="QEMU CPU count",
    )


def _add_tink_flags(parser: configargparse.ArgParser) -> None:
    """Tinkerbell kernel cmdline flags + --ipam."""
    g = parser.add_argument_group("tinkerbell")
    g.add_argument(
        "--tink-worker-image", env_var="TINK_WORKER_IMAGE", metavar="IMAGE",
        default="ghcr.io/tinkerbell/tink-agent:latest",
        help="tink-agent container image reference",
    )
    g.add_argument(
        "--tink-docker-registry", env_var="TINK_DOCKER_REGISTRY", metavar="HOST",
        default="", help="registry host (triggers tink-agent services)",
    )
    g.add_argument(
        "--tink-grpc-authority", env_var="TINK_GRPC_AUTHORITY", metavar="ADDR",
        default="", help="tink-server gRPC endpoint (host:port)",
    )
    g.add_argument(
        "--tink-worker-id", env_var="TINK_WORKER_ID", metavar="ID",
        default="", help="machine / worker ID",
    )
    g.add_argument(
        "--tink-tls", env_var="TINK_TLS", metavar="BOOL",
        default="false", help="enable TLS to tink-server",
    )
    g.add_argument(
        "--tink-insecure-tls", env_var="TINK_INSECURE_TLS", metavar="BOOL",
        default="true", help="allow insecure TLS",
    )
    g.add_argument(
        "--tink-insecure-registries", env_var="TINK_INSECURE_REGISTRIES", metavar="LIST",
        default="", help="comma-separated insecure registries",
    )
    g.add_argument(
        "--tink-registry-username", env_var="TINK_REGISTRY_USERNAME", metavar="USER",
        default="", help="registry auth username",
    )
    g.add_argument(
        "--tink-registry-password", env_var="TINK_REGISTRY_PASSWORD", metavar="PASS",
        default="", help="registry auth password",
    )
    g.add_argument(
        "--tink-syslog-host", env_var="TINK_SYSLOG_HOST", metavar="HOST",
        default="", help="remote syslog host",
    )
    g.add_argument(
        "--tink-facility", env_var="TINK_FACILITY", metavar="CODE",
        default="", help="facility code",
    )
    g.add_argument(
        "--ipam", env_var="IPAM", metavar="PARAM",
        default="", help="static networking IPAM parameter",
    )


# Map command → list of flag-group adders.
_COMMAND_FLAGS: dict[str, list[object]] = {
    "build":     [_add_common_flags, _add_kernel_flags, _add_tools_flags, _add_mkosi_flags, _add_iso_flags],
    "kernel":    [_add_common_flags, _add_kernel_flags],
    "tools":     [_add_common_flags, _add_tools_flags],
    "initramfs": [_add_common_flags, _add_mkosi_flags],
    "iso":       [_add_common_flags, _add_iso_flags],
    "shell":     [_add_common_flags],
    "clean":     [],
    "summary":   [_add_common_flags, _add_summary_flags],
    "qemu-test": [_add_common_flags, _add_qemu_flags, _add_tink_flags],
}


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

def _build_kernel_stage(cfg: Config) -> None:
    """Run the kernel build stage according to *cfg.kernel_mode*."""
    klog = for_stage("kernel")

    # --- skip ---------------------------------------------------------
    if cfg.kernel_mode == "skip":
        klog.log("KERNEL_MODE=skip — skipping kernel build")
        return

    # --- idempotency --------------------------------------------------
    modules_dir = cfg.extra_tree_output / "usr" / "lib" / "modules"
    vmlinuz_dir = cfg.vmlinuz_output
    has_vmlinuz = vmlinuz_dir.is_dir() and any(vmlinuz_dir.glob("vmlinuz-*"))

    if modules_dir.is_dir() and has_vmlinuz and not cfg.force_kernel:
        klog.log("Kernel already built (use --force-kernel to rebuild)")
        return

    if modules_dir.is_dir() and not has_vmlinuz:
        klog.warn("Modules exist but vmlinuz is missing — rebuilding kernel")

    # --- native -------------------------------------------------------
    if cfg.kernel_mode == "native":
        missing = check_kernel_dependencies(cfg.arch)
        if missing:
            klog.err(f"Missing kernel build tools: {', '.join(missing)}")
            klog.err("Install them or set --kernel-mode=docker.")
            raise SystemExit(1)
        klog.log("Building kernel (native)...")
        kernel.build(cfg)
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg, logger=klog)
    klog.log("Building kernel (docker)...")
    docker.run_in_builder(
        cfg,
        "--entrypoint", "python3",
        cfg.builder_image,
        "/work/build.py", "kernel",
    )
    docker.fix_docker_ownership(cfg, klog, [
        f"/work/mkosi.output/extra-tree/{cfg.arch}",
        f"/work/mkosi.output/vmlinuz/{cfg.arch}",
        "/work/out",
    ])


def _build_tools_stage(cfg: Config) -> None:
    """Run the tools download stage according to *cfg.tools_mode*."""
    tlog = for_stage("tools")

    # --- skip ---------------------------------------------------------
    if cfg.tools_mode == "skip":
        tlog.log("TOOLS_MODE=skip — skipping tools download")
        return

    # --- native -------------------------------------------------------
    if cfg.tools_mode == "native":
        tlog.log("Downloading tools (nerdctl, containerd, etc.)...")
        tools.download_all(cfg)
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg, logger=tlog)
    tlog.log("Downloading tools (nerdctl, containerd, etc.)...")
    docker.run_in_builder(
        cfg,
        "--entrypoint", "python3",
        cfg.builder_image,
        "/work/build.py", "tools",
    )
    docker.fix_docker_ownership(cfg, tlog, ["/work/mkosi.output"])


def _build_mkosi_stage(cfg: Config, extra_args: list[str]) -> None:
    """Run the mkosi image-assembly stage according to *cfg.mkosi_mode*."""
    ilog = for_stage("initramfs")

    # --- skip ---------------------------------------------------------
    if cfg.mkosi_mode == "skip":
        ilog.log("MKOSI_MODE=skip — skipping image assembly")
        return

    mkosi_args = list(cfg.mkosi_args) + list(extra_args)

    # --- native -------------------------------------------------------
    if cfg.mkosi_mode == "native":
        missing = check_mkosi_dependencies()
        if missing:
            ilog.err(f"Missing mkosi tools: {', '.join(missing)}")
            ilog.err("Install them or set --mkosi-mode=docker.")
            raise SystemExit(1)
        ilog.log("Building initrd with mkosi (native)...")
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
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg, logger=ilog)
    ilog.log("Building initrd with mkosi (docker)...")
    extra_tree = f"/work/mkosi.output/extra-tree/{cfg.arch}"
    output_dir = f"/work/mkosi.output/initramfs/{cfg.arch}"
    docker.run_mkosi(
        cfg,
        f"--extra-tree={extra_tree}",
        f"--output-dir={output_dir}",
        "build",
        *mkosi_args,
        logger=ilog,
    )
    docker.fix_docker_ownership(cfg, ilog, [
        f"/work/mkosi.output/initramfs/{cfg.arch}",
        "/work/out",
    ])


def _build_iso_stage(cfg: Config) -> None:
    """Run the ISO build stage according to *cfg.iso_mode*."""
    isolog = for_stage("iso")

    # --- skip ---------------------------------------------------------
    if cfg.iso_mode == "skip":
        isolog.log("ISO_MODE=skip — skipping ISO build")
        return

    # --- idempotency --------------------------------------------------
    iso_path = cfg.iso_output / f"captainos-{cfg.arch}.iso"
    if iso_path.is_file() and not cfg.force_iso:
        isolog.log(f"ISO already built: {iso_path} (use --force-iso to rebuild)")
        return

    # --- native -------------------------------------------------------
    if cfg.iso_mode == "native":
        isolog.log("Building ISO (native)...")
        iso.build(cfg)
        return

    # --- docker -------------------------------------------------------
    docker.build_builder(cfg, logger=isolog)
    isolog.log("Building ISO (docker)...")
    docker.run_in_builder(
        cfg,
        "--entrypoint", "python3",
        cfg.builder_image,
        "/work/build.py", "iso",
    )
    docker.fix_docker_ownership(cfg, isolog, [
        "/work/mkosi.output/iso",
        "/work/mkosi.output/iso-staging",
        "/work/out",
    ])

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
    artifacts.collect_initramfs(cfg, logger=ilog)
    artifacts.collect_kernel(cfg, logger=ilog)
    artifacts.collect_checksums(cfg, logger=ilog)
    ilog.log("Initramfs build complete!")



def _cmd_iso(cfg: Config, _extra_args: list[str]) -> None:
    """Build only the ISO image."""
    isolog = for_stage("iso")
    _build_iso_stage(cfg)
    artifacts.collect_iso(cfg, logger=isolog)
    artifacts.collect_checksums(cfg, logger=isolog)
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


def _cmd_qemu_test(cfg: Config, _extra_args: list[str], args: object = None) -> None:
    """Boot the image in QEMU for testing."""
    qemu.run_qemu(cfg, args=args)  # type: ignore[arg-type]


def main(project_dir: Path | None = None) -> None:
    """Main CLI entry point."""

    # 1. Extract the subcommand from argv before parsing so flags
    #    work in any position (before or after the command name).
    raw_argv = sys.argv[1:]
    command, flag_argv = _extract_command(raw_argv)

    # 2. Build the parser (TINK flags added only for qemu-test).
    parser = _build_parser(command)

    # 3. Parse known args — anything unrecognised passes through to mkosi.
    args, extra = parser.parse_known_args(flag_argv)

    # 4. Separate --force (mkosi passthrough) from the rest.
    mkosi_args: list[str] = []
    if getattr(args, "force", False):
        mkosi_args.append("--force")

    # 5. Determine project directory.
    if project_dir is None:
        project_dir = Path(__file__).resolve().parent.parent

    # 6. Build Config from the parsed namespace.
    cfg = Config.from_args(args, project_dir)
    cfg.mkosi_args = mkosi_args

    # 7. Dispatch.
    dispatch: dict[str, object] = {
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
        if command == "qemu-test":
            handler(cfg, extra, args=args)  # type: ignore[operator]
        else:
            handler(cfg, extra)  # type: ignore[operator]
    else:
        # Pass through to mkosi (shouldn't happen with _extract_command
        # but kept as a safety net).
        mlog = for_stage("mkosi")
        extra_tree = str(cfg.extra_tree_output)
        output_dir = str(cfg.initramfs_output)
        match cfg.mkosi_mode:
            case "docker":
                docker.build_builder(cfg, logger=mlog)
                container_tree = f"/work/mkosi.output/extra-tree/{cfg.arch}"
                container_outdir = f"/work/mkosi.output/initramfs/{cfg.arch}"
                docker.run_mkosi(cfg, f"--extra-tree={container_tree}", f"--output-dir={container_outdir}", command, *extra, logger=mlog)
            case "native":
                run(
                    ["mkosi", f"--architecture={cfg.arch_info.mkosi_arch}", f"--extra-tree={extra_tree}", f"--output-dir={output_dir}", command, *extra],
                    cwd=cfg.project_dir,
                )
            case "skip":
                mlog.err(f"Cannot pass '{command}' to mkosi when MKOSI_MODE=skip.")
                raise SystemExit(1)
