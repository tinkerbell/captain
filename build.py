#!/usr/bin/env python3
"""CaptainOS build system entry point.

Requires: Python >= 3.13, Rich, Docker (unless all stages use native or skip).
It is recommended to use Astral's uv to run this script, which will automatically
 install dependencies in an isolated environment.
This will automatically use uv to re-launch itself when stages use Docker.
"""

import sys

if sys.version_info < (3, 13):
    print("ERROR: Python >= 3.13 is required.", file=sys.stderr)
    sys.exit(1)

try:
    from captain.cli import main
except ImportError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    uv_url = "https://docs.astral.sh/uv/getting-started/installation/"
    print(f"Missing dependencies, use uv to run. See {uv_url}", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
