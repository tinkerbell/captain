#!/usr/bin/env python3
"""CaptainOS build system entry point.

Requires: Python >= 3.10, Docker (unless all stages use native or skip)
"""

import sys

if sys.version_info < (3, 10):
    print("ERROR: Python >= 3.10 is required.", file=sys.stderr)
    sys.exit(1)

try:
    from captain.cli import main
except ImportError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    print("Install dependencies:  pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
