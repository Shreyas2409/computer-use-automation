"""Surface abstraction: Protocol seam between logic and implementation.

This package defines the Surface protocol interface and implementations:
- base.py — Protocol definition: observe(), act(), resolve(ladder), snapshot_evidence()
- observation.py — Observation data model: a11y tree + screenshot + URL + title
- web.py — Playwright implementation of Surface
"""

from .base import Surface
from .observation import Observation

__all__ = ["Surface", "Observation"]
