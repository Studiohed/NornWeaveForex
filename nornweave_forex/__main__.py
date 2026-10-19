"""``python -m nornweave_forex`` — same entry point as the console script."""

from __future__ import annotations

import sys

from nornweave_forex.cli import main

if __name__ == "__main__":
    sys.exit(main())
