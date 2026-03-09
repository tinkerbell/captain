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

from captain.cli._main import main
from captain.cli._parser import COMMANDS

__all__ = [
    "COMMANDS",
    "main",
]
