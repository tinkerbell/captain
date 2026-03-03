#!/usr/bin/env python3
"""Build CaptainOS ISO — in-container entry point.

Runs inside the Docker builder container.  Delegates to captain.iso.build().
"""

import sys
from pathlib import Path

# The project is mounted at /work inside the container
sys.path.insert(0, "/work")

from captain.config import Config
from captain.iso import build


def main() -> int:
    """Entry point for building the ISO inside the container."""
    cfg = Config.from_env(Path("/work"))
    build(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
