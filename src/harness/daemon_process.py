from __future__ import annotations

import sys

from harness.entrypoints import harnessd_main


def main() -> int:
    """Run the canonical Harness daemon from the installed Python package."""
    sys.argv = [sys.argv[0], "serve"]
    return harnessd_main()


if __name__ == "__main__":
    raise SystemExit(main())
