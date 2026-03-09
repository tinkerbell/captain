"""CLI parser infrastructure — formatter, constants, and flag definitions."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Callable, Iterable

import configargparse

from captain.config import DEFAULT_KERNEL_VERSION

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
    "clean": "Remove build artifacts (per kernel version or all)",
    "summary": "Print mkosi configuration summary",
    "qemu-test": "Boot the image in QEMU for testing",
}

VALID_MODES = ("docker", "native", "skip")

# Boolean (store_true) flags — these do NOT consume the next token as a value.
# Used by _extract_command to avoid treating a flag value as a subcommand.
_BOOLEAN_FLAGS = frozenset(
    {
        "--all",
        "--force-kernel",
        "--force-tools",
        "--force-iso",
        "--force",
        "--no-cache",
        "-h",
        "--help",
    }
)


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
            "pull": "Pull and extract artifacts (amd64, arm64, or combined)",
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
        default=DEFAULT_KERNEL_VERSION,
        help="kernel version to build",
    )
    g.add_argument(
        "--kernel-config",
        env_var="KERNEL_CONFIG",
        metavar="PATH",
        default=None,
        help="path to kernel config file (overrides auto-detection from kernel.configs/)",
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


def _add_clean_flags(parser: configargparse.ArgParser) -> None:
    """--all flag for the clean command."""
    g = parser.add_argument_group("clean")
    g.add_argument(
        "--all",
        env_var="CLEAN_ALL",
        action="store_true",
        default=False,
        dest="clean_all",
        help="remove ALL build artifacts instead of just the selected kernel version",
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
        choices=["amd64", "arm64", "combined"],
        help="artifact target (amd64, arm64, or combined; default: --arch value)",
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
    g.add_argument(
        "--force",
        env_var="FORCE",
        action="store_true",
        default=False,
        help="publish even if the image already exists in the registry",
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
    "initramfs": [_add_common_flags, _add_kernel_flags, _add_mkosi_flags],
    "iso": [_add_common_flags, _add_kernel_flags, _add_iso_flags],
    "checksums": [_add_common_flags, _add_kernel_flags, _add_checksums_flags],
    "release": [_add_common_flags, _add_kernel_flags, _add_release_flags],
    "shell": [_add_common_flags],
    "clean": [_add_common_flags, _add_kernel_flags, _add_clean_flags],
    "summary": [_add_common_flags, _add_kernel_flags, _add_summary_flags],
    "qemu-test": [_add_common_flags, _add_kernel_flags, _add_qemu_flags, _add_tink_flags],
}
