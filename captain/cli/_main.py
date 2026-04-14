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

import logging
import sys
from pathlib import Path

from captain import docker
from captain.config import Config
from captain.util import run

from ._commands import (
    _cmd_build,
    _cmd_checksums,
    _cmd_clean,
    _cmd_initramfs,
    _cmd_iso,
    _cmd_kernel,
    _cmd_qemu_test,
    _cmd_shell,
    _cmd_summary,
    _cmd_tools,
)
from ._parser import _build_parser, _extract_command
from ._release import _cmd_release

log = logging.getLogger(__name__)


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
        project_dir = Path(__file__).resolve().parent.parent.parent

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
        if command in ("qemu-test", "checksums", "release", "clean"):
            handler(cfg, extra, args=args)  # type: ignore[operator]
        else:
            handler(cfg, extra)  # type: ignore[operator]
    else:
        # Pass through to mkosi (shouldn't happen with _extract_command
        # but kept as a safety net).
        tools_tree = str(cfg.tools_output)
        modules_tree = str(cfg.modules_output)
        output_dir = str(cfg.initramfs_output)
        match cfg.mkosi_mode:
            case "docker":
                docker.build_builder(cfg)
                container_tree = f"/work/mkosi.output/tools/{cfg.arch}"
                container_modules = (
                    f"/work/mkosi.output/kernel/{cfg.kernel_version}/{cfg.arch}/modules"
                )
                container_outdir = f"/work/mkosi.output/initramfs/{cfg.kernel_version}/{cfg.arch}"
                docker.run_mkosi(
                    cfg,
                    f"--extra-tree={container_tree}",
                    f"--extra-tree={container_modules}",
                    f"--output-dir={container_outdir}",
                    command,
                    *extra,
                )
            case "native":
                run(
                    [
                        "mkosi",
                        f"--architecture={cfg.arch_info.mkosi_arch}",
                        f"--extra-tree={tools_tree}",
                        f"--extra-tree={modules_tree}",
                        f"--output-dir={output_dir}",
                        command,
                        *extra,
                    ],
                    cwd=cfg.project_dir,
                )
            case "skip":
                log.error("Cannot pass '%s' to mkosi when MKOSI_MODE=skip.", command)
                raise SystemExit(1)
