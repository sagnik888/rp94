"""Refuse unsupported interpreters before starting a live-money process."""

import sys


REQUIRED = (3, 13)
if sys.version_info[:2] != REQUIRED:
    raise SystemExit(
        f"Unsupported Python {sys.version.split()[0]}; this system is pinned to "
        f"{REQUIRED[0]}.{REQUIRED[1]}.x (validated on 3.13.9)."
    )
