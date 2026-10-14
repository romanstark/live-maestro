"""Native Live and its capture worker must see the same filesystem paths."""
import sys


def require_shared_filesystem() -> None:
    if sys.platform == 'win32' and 'WindowsApps' in sys.base_prefix:
        raise RuntimeError('Use uv-managed Python for Live Maestro; Store Python redirects '
                           'files that native Live and its capture worker cannot share.')
