"""``python -m src.core.demo_data``.

A package needs this to be executable, where :mod:`src.core.seed` - a single
module - did not. Keeping the entry point here rather than moving ``main()``
means the Makefile target reads the same as ``make seed`` beside it.
"""

from __future__ import annotations

import sys

from src.core.demo_data import main

if __name__ == "__main__":
    sys.exit(main())
