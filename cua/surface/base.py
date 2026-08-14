"""Surface Protocol: abstraction seam for UI interaction.

A Surface is the interface between the replay/discovery logic and the browser
driver (Playwright). Implementations observe the UI, execute actions, resolve
locators, and capture evidence. This protocol allows swapping implementations
(Playwright, UIA, pywinauto, etc.) without changing the upper layers.
"""

from __future__ import annotations

from typing import Any, Protocol

from cua.schema import LocatorKind, LocatorLadder
from cua.surface.observation import Observation


class Surface(Protocol):
    """Protocol for browser/UI surface implementations.

    All methods are async so they can be called from async discovery/replay loops.
    """

    async def observe(self) -> Observation:
        """Capture current UI state.

        Returns an Observation with normalized a11y tree, screenshot, URL, and title.
        """
        ...

    async def act(self, action: str, target: Any, value: Any = None) -> None:
        """Execute an action on the surface.

        Args:
            action: Action kind (navigate, click, type, select, read, wait_for, assert)
            target: Target specification (varies by action and surface impl)
            value: Optional value (for navigate, type, select actions)

        Raises:
            ValueError: If the action or target is invalid
        """
        ...

    async def resolve(self, ladder: LocatorLadder) -> LocatorKind:
        """Try to resolve a locator ladder, returning the rung that matched.

        Given a ladder of locator strategies ordered by durability, try each
        strategy top-down until one resolves to a visible element. Return the
        kind of the rung that matched.

        This is the key to replay drift detection: at record time, the model
        found the element via one rung; at replay time, if a different rung
        matches, we have drift.

        Args:
            ladder: LocatorLadder with ordered strategies and recorded_match

        Returns:
            The LocatorKind of the strategy that matched (e.g., "role_name")

        Raises:
            ValueError: If no rung in the ladder resolves
        """
        ...

    async def snapshot_evidence(self, path: str) -> None:
        """Capture and save evidence (screenshot, tree dump, logs) to disk.

        Args:
            path: Directory where evidence should be saved
        """
        ...
