"""Surface abstraction: Protocol seam between logic and implementation.

This package defines the Surface protocol interface and implementations:
- base.py — Protocol definition: observe(), act(), resolve(ladder), snapshot_evidence()
- observation.py — Observation data model + ``capture_a11y_snapshot`` helper
- web.py — Playwright implementation of Surface
- _playwright_compat.py — restores ``Page.accessibility`` (removed in 1.60)

Importing this package installs the Playwright compat shim as a side effect
so any downstream code (including the discovery loop) that still calls
``page.accessibility.snapshot(...)`` transparently goes through the
supported DOM-walk capture.
"""

from .base import Surface
from .observation import Observation, capture_a11y_snapshot

# Side-effect import: patches ``Page.accessibility`` at import time.
from . import _playwright_compat  # noqa: F401

__all__ = ["Surface", "Observation", "capture_a11y_snapshot"]
