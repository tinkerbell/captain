"""captain — CaptainOS build system.

Logging is configured here so that every ``logging.getLogger(__name__)``
call in submodules automatically inherits the Rich console handler.
"""

from __future__ import annotations

import logging
import os

from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as _install_rich_traceback

# Rich console — writes to stderr so log output never pollutes piped stdout.
# If running under GHA, force colors.
if os.environ.get("GITHUB_ACTIONS", "") == "":
    console = Console(stderr=True)
else:
    console = Console(stderr=True, color_system="standard", width=160, highlight=False)

# Install Rich traceback handler globally (once, at import time).
_install_rich_traceback(console=console, show_locals=True, width=None)


class _StageFormatter(logging.Formatter):
    """Show the module path relative to the ``captain`` package as a prefix."""

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        stage = name.split(".", 1)[1] if name.startswith("captain.") else name
        record.__dict__["stage"] = stage
        return super().format(record)


# Configure the ``captain`` logger hierarchy once.
_root = logging.getLogger("captain")

if not _root.handlers:
    _handler = RichHandler(
        console=console,
        show_time=False,
        show_level=True,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
    )
    _handler.setFormatter(_StageFormatter("[%(stage)s] %(message)s"))
    _root.addHandler(_handler)
    _root.setLevel(logging.DEBUG)
    _root.propagate = False
