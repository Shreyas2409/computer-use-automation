"""Normalized observation: a11y tree + screenshot + URL + title.

An Observation represents the state of the UI at a point in time, with enough
information for locator resolution and action target selection. The a11y tree
is normalized: each node carries role, name, and value attributes. Screenshot
bytes are captured separately so they can be versioned and redacted independently
of the tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class A11yNode:
    """Accessibility tree node: role, name, value, and optional children."""

    role: str
    name: str | None = None
    value: str | None = None
    # Additional attributes available for locator strategies (e.g., id, placeholder)
    attributes: dict[str, Any] | None = None
    children: list[A11yNode] | None = None


@dataclass
class Observation:
    """Normalized UI state: a11y tree + screenshot + URL + title.

    Attributes:
        tree: Root node of the accessibility tree
        screenshot_bytes: PNG/JPEG image data (bytes), or None if not captured
        url: Current page URL
        title: Current page title (or document title)
    """

    tree: A11yNode | None = None
    screenshot_bytes: bytes | None = None
    url: str = ""
    title: str = ""

    def __post_init__(self) -> None:
        """Validate that at least tree or screenshot is present."""
        if self.tree is None and self.screenshot_bytes is None:
            raise ValueError(
                "Observation must have at least tree or screenshot_bytes"
            )
