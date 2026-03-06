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
import subprocess
import sys
from collections.abc import Callable, Iterable
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
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup],
        prefix: str | None,
    ) -> str:
        """Show a short usage line with the command placeholder."""
        prog = self._prog
        # Top-level ("build.py") and release ("build.py release") have subcommands.
        if prog in ("build.py", "build.py release"):
            return f"usage: {prog} [command] [flags]\n\n"
        if prog == "build.py release tag":
            return f"usage: {prog} <version> [flags]\n\n"
        return f"usage: {prog} [flags]\n\n"


from captain import artifacts, docker, iso, kernel, oci, qemu, tools  # noqa: E402
from captain.config import Config  # noqa: E402
from captain.log import for_stage  # noqa: E402
from captain.util import (  # noqa: E402
    check_kernel_dependencies,
    check_mkosi_dependencies,
    check_release_dependencies,
    run,
)

# ---------------------------------------------------------------------------
# Known subcommands (order matters for help text)
# ---------------------------------------------------------------------------

COMMANDS: dict[str, str] = {
    "build": "Run all build stages: kernel → tools → initramfs → iso (default)",
    "kernel": "Build only the kernel + modules",
    "tools": "Download tools (containerd, runc, nerdctl, CNI)",
    "initramfs": "Build only the initramfs via mkosi",
    "iso": "Build a UEFI-bootable ISO image",
    "checksums": "Compute SHA-256 checksums for specified files",
    "release": "OCI artifact operations (publish, pull, tag)",
    "shell": "Interactive shell inside the builder container",
    "clean": "Remove all build artifacts",
    "summary": "Print mkosi configuration summary",
    "qemu-test": "Boot the image in QEMU for testing",
}

VALID_MODES = ("docker", "native", "skip")

# Boolean (store_true) flags — these do NOT consume the next token as a value.
# Used by _extract_command to avoid treating a flag value as a subcommand.
_BOOLEAN_FLAGS = frozenset(
    {
        "--force-kernel",
        "--force-tools",
        "--force-iso",
        "--force",
        "--no-cache",
        "-h",
        "--help",
    }
)


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
            # Boolean flags don't consume the next token.
            prev_was_value_flag = False if "=" in tok else tok not in _BOOLEAN_FLAGS
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
            f"error: unknown command '{tok}'\nvalid commands: {valid}",
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
        commands_list = "\n".join(f"  {name:14s} {d}" for name, d in COMMANDS.items())
        epilog = f"""\
commands:
{commands_list}
"""
    elif command == "release":
        desc = "OCI release workflow: pull (or build) → publish → tag"
        release_cmds = {
            "publish": "Publish artifacts as a multi-arch OCI image",
            "pull": "Pull and extract artifacts (amd64, arm64, or both)",
            "tag": "Tag all artifact images with a version",
        }
        commands_list = "\n".join(f"  {name:14s} {d}" for name, d in release_cmds.items())
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
            prog,
            max_help_position=38,
            width=columns,
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
        "--arch",
        env_var="ARCH",
        default="amd64",
        choices=["amd64", "arm64"],
        help="target architecture",
    )
    g.add_argument(
        "--builder-image",
        env_var="BUILDER_IMAGE",
        metavar="IMAGE",
        default="captainos-builder",
        help="Docker builder image name",
    )
    g.add_argument(
        "--no-cache",
        env_var="NO_CACHE",
        action="store_true",
        default=False,
        help="rebuild builder image without Docker cache",
    )


def _add_kernel_flags(parser: configargparse.ArgParser) -> None:
    """--kernel-version, --kernel-src, --kernel-mode, --force-kernel"""
    g = parser.add_argument_group("kernel")
    g.add_argument(
        "--kernel-version",
        env_var="KERNEL_VERSION",
        metavar="VER",
        default="6.12.69",
        help="kernel version to build",
    )
    g.add_argument(
        "--kernel-src",
        env_var="KERNEL_SRC",
        metavar="PATH",
        default=None,
        help="path to local kernel source tree",
    )
    g.add_argument(
        "--kernel-mode",
        env_var="KERNEL_MODE",
        default="docker",
        choices=list(VALID_MODES),
        help="kernel stage execution mode",
    )
    g.add_argument(
        "--force-kernel",
        env_var="FORCE_KERNEL",
        action="store_true",
        default=False,
        help="force kernel rebuild even if outputs exist",
    )


def _add_tools_flags(parser: configargparse.ArgParser) -> None:
    """--tools-mode, --force-tools"""
    g = parser.add_argument_group("tools")
    g.add_argument(
        "--tools-mode",
        env_var="TOOLS_MODE",
        default="docker",
        choices=list(VALID_MODES),
        help="tools stage execution mode",
    )
    g.add_argument(
        "--force-tools",
        env_var="FORCE_TOOLS",
        action="store_true",
        default=False,
        help="re-download tools even if outputs exist",
    )


def _add_mkosi_flags(parser: configargparse.ArgParser) -> None:
    """--mkosi-mode, --force (mkosi passthrough)"""
    g = parser.add_argument_group("initramfs (mkosi)")
    g.add_argument(
        "--mkosi-mode",
        env_var="MKOSI_MODE",
        default="docker",
        choices=list(VALID_MODES),
        help="mkosi stage execution mode",
    )
    g.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="passed through to mkosi as --force",
    )


def _add_summary_flags(parser: configargparse.ArgParser) -> None:
    """--mkosi-mode only (no --force)."""
    g = parser.add_argument_group("mkosi")
    g.add_argument(
        "--mkosi-mode",
        env_var="MKOSI_MODE",
        default="docker",
        choices=list(VALID_MODES),
        help="mkosi stage execution mode",
    )


def _add_iso_flags(parser: configargparse.ArgParser) -> None:
    """--iso-mode, --force-iso"""
    g = parser.add_argument_group("iso")
    g.add_argument(
        "--iso-mode",
        env_var="ISO_MODE",
        default="docker",
        choices=list(VALID_MODES),
        help="iso stage execution mode",
    )
    g.add_argument(
        "--force-iso",
        env_var="FORCE_ISO",
        action="store_true",
        default=False,
        help="force ISO rebuild even if outputs exist",
    )


def _add_mode_flags(parser: configargparse.ArgParser) -> None:
    """All four --*-mode flags (used by 'shell' which checks needs_docker)."""
    g = parser.add_argument_group("stage modes")
    for stage in ("kernel", "tools", "mkosi", "iso"):
        g.add_argument(
            f"--{stage}-mode",
            env_var=f"{stage.upper()}_MODE",
            default="docker",
            choices=list(VALID_MODES),
            help=f"{stage} stage execution mode",
        )


def _add_checksums_flags(parser: configargparse.ArgParser) -> None:
    """--output and positional file arguments for the checksums command."""
    g = parser.add_argument_group("checksums")
    g.add_argument(
        "--output",
        "-o",
        metavar="FILE",
        default=None,
        help="path to write the checksum file (default: out/sha256sums-{arch}.txt)",
    )
    g.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="files to checksum (default: standard release artifacts in out/)",
    )


def _add_release_flags(parser: configargparse.ArgParser) -> None:
    """--release-mode and OCI registry flags for the release command."""
    _add_release_base_flags(parser)
    _add_release_target_flag(parser)
    _add_release_pull_output(parser)


def _add_release_base_flags(parser: configargparse.ArgParser) -> None:
    """Core release flags shared by all release subcommands."""
    g = parser.add_argument_group("release")
    g.add_argument(
        "--release-mode",
        env_var="RELEASE_MODE",
        default="native",
        choices=list(VALID_MODES),
        metavar="MODE",
        help="release stage execution mode",
    )

    g = parser.add_argument_group("OCI registry")
    g.add_argument(
        "--registry",
        env_var="REGISTRY",
        metavar="HOST",
        default="ghcr.io",
        help="OCI registry hostname",
    )
    g.add_argument(
        "--repository",
        env_var="GITHUB_REPOSITORY",
        metavar="OWNER/NAME",
        default="tinkerbell/captain",
        help="repository (owner/name)",
    )
    g.add_argument(
        "--oci-artifact-name",
        env_var="OCI_ARTIFACT_NAME",
        metavar="NAME",
        default="artifacts",
        help="OCI artifact image name",
    )


def _add_release_target_flag(parser: configargparse.ArgParser) -> None:
    """--target flag for publish and pull (not tag)."""
    g = parser.add_argument_group("target")
    g.add_argument(
        "--target",
        env_var="TARGET",
        default=None,
        choices=["amd64", "arm64", "both"],
        help="artifact target (amd64, arm64, or both; default: --arch value)",
    )
    g.add_argument(
        "--git-sha",
        env_var="GITHUB_SHA",
        metavar="SHA",
        default=None,
        help="git commit SHA (default: from git rev-parse HEAD)",
    )
    g.add_argument(
        "--version-exclude",
        env_var="VERSION_EXCLUDE",
        metavar="TAG",
        default=None,
        help="tag to exclude from git-describe version lookup",
    )


def _add_release_pull_output(parser: configargparse.ArgParser) -> None:
    """--pull-output flag (only relevant for 'release pull')."""
    g = parser.add_argument_group("pull")
    g.add_argument(
        "--pull-output",
        metavar="DIR",
        default=None,
        help="output directory for pulled artifacts",
    )


def _add_release_tag_version(parser: configargparse.ArgParser) -> None:
    """Positional <version> argument for 'release tag'."""
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help="version tag to apply (e.g. v1.0.0)",
    )


def _add_qemu_flags(parser: configargparse.ArgParser) -> None:
    """--qemu-append, --qemu-mem, --qemu-smp"""
    g = parser.add_argument_group("qemu")
    g.add_argument(
        "--qemu-append",
        env_var="QEMU_APPEND",
        metavar="ARGS",
        default="",
        help="extra kernel cmdline args for qemu-test",
    )
    g.add_argument(
        "--qemu-mem",
        env_var="QEMU_MEM",
        metavar="SIZE",
        default="2G",
        help="QEMU RAM size",
    )
    g.add_argument(
        "--qemu-smp",
        env_var="QEMU_SMP",
        metavar="N",
        default="2",
        help="QEMU CPU count",
    )


def _add_tink_flags(parser: configargparse.ArgParser) -> None:
    """Tinkerbell kernel cmdline flags + --ipam."""
    g = parser.add_argument_group("tinkerbell")
    g.add_argument(
        "--tink-worker-image",
        env_var="TINK_WORKER_IMAGE",
        metavar="IMAGE",
        default="ghcr.io/tinkerbell/tink-agent:latest",
        help="tink-agent container image reference",
    )
    g.add_argument(
        "--tink-docker-registry",
        env_var="TINK_DOCKER_REGISTRY",
        metavar="HOST",
        default="",
        help="registry host (triggers tink-agent services)",
    )
    g.add_argument(
        "--tink-grpc-authority",
        env_var="TINK_GRPC_AUTHORITY",
        metavar="ADDR",
        default="",
        help="tink-server gRPC endpoint (host:port)",
    )
    g.add_argument(
        "--tink-worker-id",
        env_var="TINK_WORKER_ID",
        metavar="ID",
        default="",
        help="machine / worker ID",
    )
    g.add_argument(
        "--tink-tls",
        env_var="TINK_TLS",
        metavar="BOOL",
        default="false",
        help="enable TLS to tink-server",
    )
    g.add_argument(
        "--tink-insecure-tls",
        env_var="TINK_INSECURE_TLS",
        metavar="BOOL",
        default="true",
        help="allow insecure TLS",
    )
    g.add_argument(
        "--tink-insecure-registries",
        env_var="TINK_INSECURE_REGISTRIES",
        metavar="LIST",
        default="",
        help="comma-separated insecure registries",
    )
    g.add_argument(
        "--tink-registry-username",
        env_var="TINK_REGISTRY_USERNAME",
        metavar="USER",
        default="",
        help="registry auth username",
    )
    g.add_argument(
        "--tink-registry-password",
        env_var="TINK_REGISTRY_PASSWORD",
        metavar="PASS",
        default="",
        help="registry auth password",
    )
    g.add_argument(
        "--tink-syslog-host",
        env_var="TINK_SYSLOG_HOST",
        metavar="HOST",
        default="",
        help="remote syslog host",
    )
    g.add_argument(
        "--tink-facility",
        env_var="TINK_FACILITY",
        metavar="CODE",
        default="",
        help="facility code",
    )
    g.add_argument(
        "--ipam",
        env_var="IPAM",
        metavar="PARAM",
        default="",
        help="static networking IPAM parameter",
    )


# Map command → list of flag-group adders.
_COMMAND_FLAGS: dict[str, list[Callable[..., None]]] = {
    "build": [
        _add_common_flags,
        _add_kernel_flags,
        _add_tools_flags,
        _add_mkosi_flags,
        _add_iso_flags,
    ],
    "kernel": [_add_common_flags, _add_kernel_flags],
    "tools": [_add_common_flags, _add_tools_flags],
    "initramfs": [_add_common_flags, _add_mkosi_flags],
    "iso": [_add_common_flags, _add_iso_flags],
    "checksums": [_add_common_flags, _add_checksums_flags],
    "release": [_add_common_flags, _add_release_flags],
    "shell": [_add_common_flags],
    "clean": [],
    "summary": [_add_common_flags, _add_summary_flags],
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
        "--entrypoint",
        "python3",
        cfg.builder_image,
        "/work/build.py",
        "kernel",
    )
    docker.fix_docker_ownership(
        cfg,
        klog,
        [
            f"/work/mkosi.output/extra-tree/{cfg.arch}",
            f"/work/mkosi.output/vmlinuz/{cfg.arch}",
            "/work/out",
        ],
    )


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
        "--entrypoint",
        "python3",
        cfg.builder_image,
        "/work/build.py",
        "tools",
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
    docker.fix_docker_ownership(
        cfg,
        ilog,
        [
            f"/work/mkosi.output/initramfs/{cfg.arch}",
            "/work/out",
        ],
    )


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
        "--entrypoint",
        "python3",
        cfg.builder_image,
        "/work/build.py",
        "iso",
    )
    docker.fix_docker_ownership(
        cfg,
        isolog,
        [
            "/work/mkosi.output/iso",
            "/work/mkosi.output/iso-staging",
            "/work/out",
        ],
    )


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
                    "rm -rf /work/mkosi.output/image*"
                    " /work/mkosi.output/initramfs"
                    " /work/mkosi.output/vmlinuz"
                    " /work/mkosi.output/extra-tree"
                    " /work/mkosi.output/iso"
                    " /work/mkosi.output/iso-staging"
                    " /work/mkosi.cache",
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
            docker.run_mkosi(
                cfg,
                f"--extra-tree={container_tree}",
                f"--output-dir={container_outdir}",
                "summary",
                logger=slog,
            )
        case "native":
            run(
                [
                    "mkosi",
                    f"--architecture={cfg.arch_info.mkosi_arch}",
                    f"--extra-tree={extra_tree}",
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
        arch = cfg.arch
        arch_files = [
            out / f"vmlinuz-{arch}",
            out / f"initramfs-{arch}.cpio.zst",
            out / f"captainos-{arch}.iso",
        ]
        existing = [f for f in arch_files if f.is_file()]
        if not existing:
            clog.err(f"No artifacts found for {arch} in {out}")
            raise SystemExit(1)
        dest = Path(output) if output else out / f"sha256sums-{arch}.txt"
        artifacts.collect_checksums(existing, dest, logger=clog)
    clog.log("Checksums complete!")


_RELEASE_SUBCOMMANDS = ("publish", "pull", "tag")

_RELEASE_SUBCMD_INFO: dict[str, tuple[str, list]] = {
    "publish": (
        "Publish artifacts as a multi-arch OCI image",
        [_add_common_flags, _add_release_base_flags, _add_release_target_flag],
    ),
    "pull": (
        "Pull and extract artifacts (amd64, arm64, or both)",
        [
            _add_common_flags,
            _add_release_base_flags,
            _add_release_target_flag,
            _add_release_pull_output,
        ],
    ),
    "tag": (
        "Tag all artifact images with a version",
        [_add_common_flags, _add_release_base_flags, _add_release_tag_version],
    ),
}


def _print_release_subcmd_help(sub: str, *, exit_code: int = 0) -> None:
    """Print help for a release subcommand and exit."""
    desc, adders = _RELEASE_SUBCMD_INFO[sub]
    columns = shutil.get_terminal_size().columns
    parser = configargparse.ArgParser(
        prog=f"build.py release {sub}",
        description=desc,
        add_env_var_help=False,
        formatter_class=lambda prog: _HelpFormatter(
            prog,
            max_help_position=38,
            width=columns,
        ),
    )
    for adder in adders:
        adder(parser)
    parser.print_help()
    raise SystemExit(exit_code)


def _resolve_git_sha(args: object, project_dir: Path) -> str:
    """Return the git SHA from args or by running git rev-parse."""
    sha = getattr(args, "git_sha", None)
    if sha:
        return sha

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=project_dir,
    )
    return result.stdout.strip()


def _cmd_release(cfg: Config, extra_args: list[str], args: object = None) -> None:
    """OCI artifact operations: publish, pull, tag."""
    rlog = for_stage("release")

    # Peel the release subcommand from extra_args.
    if not extra_args:
        rlog.err(
            f"Missing release subcommand.\n"
            f"  usage: build.py release {{{','.join(_RELEASE_SUBCOMMANDS)}}}\n"
        )
        raise SystemExit(2)

    sub = extra_args[0]
    rest = extra_args[1:]

    if sub not in _RELEASE_SUBCOMMANDS:
        rlog.err(
            f"Unknown release subcommand '{sub}'.\n  valid: {', '.join(_RELEASE_SUBCOMMANDS)}\n"
        )
        raise SystemExit(2)

    # Handle --help / -h for the subcommand.
    if "-h" in rest or "--help" in rest:
        _print_release_subcmd_help(sub)

    # --- validate required args early ---------------------------------
    if sub == "tag" and not rest:
        rlog.err("Missing version argument.")
        _print_release_subcmd_help(sub, exit_code=2)
    if sub == "pull" and not getattr(args, "pull_output", None):
        rlog.err("--pull-output is required for 'release pull'.")
        _print_release_subcmd_help(sub, exit_code=2)

    # --- skip ---------------------------------------------------------
    if cfg.release_mode == "skip":
        rlog.log("RELEASE_MODE=skip — skipping release operation")
        return

    # --- docker -------------------------------------------------------
    if cfg.release_mode == "docker":
        docker.build_release_image(cfg, logger=rlog)
        rlog.log(f"Running release {sub} (docker)...")
        # Forward release-specific env vars into the container.
        registry = getattr(args, "registry", "ghcr.io")
        repository = getattr(args, "repository", "tinkerbell/captain")
        artifact_name = getattr(args, "oci_artifact_name", "artifacts")
        sha = _resolve_git_sha(args, cfg.project_dir)
        env_args: list[str] = [
            "-e",
            f"REGISTRY={registry}",
            "-e",
            f"GITHUB_REPOSITORY={repository}",
            "-e",
            f"OCI_ARTIFACT_NAME={artifact_name}",
            "-e",
            f"GITHUB_SHA={sha}",
        ]
        exclude = getattr(args, "version_exclude", None)
        if exclude:
            env_args += ["-e", f"VERSION_EXCLUDE={exclude}"]
        if sub in ("publish", "pull"):
            target = getattr(args, "target", None) or cfg.arch
            env_args += ["-e", f"TARGET={target}"]
        pull_output = getattr(args, "pull_output", None)

        # Build the inner command.
        inner_cmd = ["/work/build.py", "release", sub]
        if pull_output:
            inner_cmd += ["--pull-output", pull_output]
        inner_cmd += list(rest)

        try:
            docker.run_in_release(
                cfg,
                *env_args,
                "--entrypoint",
                "python3",
                docker.RELEASE_IMAGE,
                *inner_cmd,
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(exc.returncode) from None
        paths_to_fix = ["/work/out"]
        if pull_output:
            container_pull_output = f"/work/{pull_output.lstrip('/')}"
            paths_to_fix.append(container_pull_output)
        docker.fix_docker_ownership(cfg, rlog, paths_to_fix)
        return

    # --- native -------------------------------------------------------
    if cfg.release_mode == "native":
        missing = check_release_dependencies()
        if missing:
            rlog.err(f"Missing release tools: {', '.join(missing)}")
            rlog.err("Install them or set --release-mode=docker.")
            raise SystemExit(1)
    # Common OCI parameters.
    registry = getattr(args, "registry", "ghcr.io")
    repository = getattr(args, "repository", "tinkerbell/captain")
    artifact_name = getattr(args, "oci_artifact_name", "artifacts")
    exclude = getattr(args, "version_exclude", None)
    sha = _resolve_git_sha(args, cfg.project_dir)
    tag = oci.compute_version_tag(cfg.project_dir, sha, exclude=exclude)

    if sub == "publish":
        target = getattr(args, "target", None) or cfg.arch
        oci.publish(
            cfg,
            target=target,
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            tag=tag,
            sha=sha,
            logger=rlog,
        )

    elif sub == "pull":
        target = getattr(args, "target", None) or cfg.arch
        pull_output = getattr(args, "pull_output", None)
        if pull_output is None:
            rlog.err("--pull-output is required for 'release pull'.")
            raise SystemExit(2)
        oci.pull(
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            tag=tag,
            target=target,
            output_dir=Path(pull_output),
            logger=rlog,
        )

    elif sub == "tag":
        version = rest[0]
        oci.tag_all(
            registry=registry,
            repository=repository,
            artifact_name=artifact_name,
            src_tag=tag,
            new_tag=version,
            logger=rlog,
        )


def _cmd_qemu_test(cfg: Config, _extra_args: list[str], args: object = None) -> None:
    """Boot the image in QEMU for testing."""
    qemu.run_qemu(cfg, args=args)  # type: ignore[arg-type]


def main(project_dir: Path | None = None) -> None:
    """Main CLI entry point."""

    # 1. Extract the subcommand from argv before parsing so flags
    #    work in any position (before or after the command name).
    raw_argv = sys.argv[1:]
    command, flag_argv = _extract_command(raw_argv)

    # For release subcommands, defer -h/--help to _cmd_release so it
    # can print subcommand-specific help instead of the generic release help.
    # We defer whenever there's any positional token (not just valid ones),
    # so that invalid subcommands like "push" show the proper error instead
    # of the parent help.
    help_deferred = False
    if command == "release":
        has_positional = any(not tok.startswith("-") for tok in flag_argv)
        has_help = "-h" in flag_argv or "--help" in flag_argv
        if has_positional and has_help:
            flag_argv = [t for t in flag_argv if t not in ("-h", "--help")]
            help_deferred = True

    # 2. Build the parser (TINK flags added only for qemu-test).
    parser = _build_parser(command)

    # 3. Parse known args — anything unrecognised passes through to mkosi.
    args, extra = parser.parse_known_args(flag_argv)
    if help_deferred:
        extra.append("--help")

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
        "checksums": _cmd_checksums,
        "shell": _cmd_shell,
        "clean": _cmd_clean,
        "release": _cmd_release,
        "summary": _cmd_summary,
        "qemu-test": _cmd_qemu_test,
    }

    handler = dispatch.get(command)
    if handler is not None:
        if command in ("qemu-test", "checksums", "release"):
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
                docker.run_mkosi(
                    cfg,
                    f"--extra-tree={container_tree}",
                    f"--output-dir={container_outdir}",
                    command,
                    *extra,
                    logger=mlog,
                )
            case "native":
                run(
                    [
                        "mkosi",
                        f"--architecture={cfg.arch_info.mkosi_arch}",
                        f"--extra-tree={extra_tree}",
                        f"--output-dir={output_dir}",
                        command,
                        *extra,
                    ],
                    cwd=cfg.project_dir,
                )
            case "skip":
                mlog.err(f"Cannot pass '{command}' to mkosi when MKOSI_MODE=skip.")
                raise SystemExit(1)
