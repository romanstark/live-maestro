"""Wrapper around :mod:`live_maestro.indexer`, for running from a checkout.

The builder itself ships in the package, because ``match_sound`` tells its caller to
build an index and a caller who installed the wheel has no checkout. From a checkout
this is the same thing without an install:

    .venv/bin/python scripts/build_sound_index.py --limit 200
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from live_maestro.indexer import main

if __name__ == "__main__":
    raise SystemExit(main())
