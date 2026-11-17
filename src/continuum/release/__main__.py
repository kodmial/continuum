"""`python3 -m continuum.release` entry point."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover - exercised through the module
    sys.exit(main())
