"""Install the Live Maestro Remote Script and analyzer device from a checkout.

The logic lives in :mod:`live_maestro.installer`, which ships inside the wheel so
that a user who installed with ``uv tool install`` can run ``live-maestro-install``
without ever seeing this repository. This file is the same entry point for someone
working in a checkout, and it takes the same flags:

    python scripts/install_script.py            # locate the library, copy, clear caches
    python scripts/install_script.py --dry-run
    python scripts/install_script.py --diff
    python scripts/install_script.py --uninstall
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from live_maestro.installer import main
except ModuleNotFoundError:  # a checkout whose package is not installed yet
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from live_maestro.installer import main

if __name__ == "__main__":
    raise SystemExit(main())
