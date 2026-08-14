"""Playwright-based web surface implementation.

This module implements the Surface protocol for web automation via Playwright.
It is the only module in cua that imports Playwright directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page

from cua.schema import LocatorKind, LocatorLadder
from cua.surface.observation import A11yNode, Observation


class WebSurface:
    """Playwright-based Surface implementation for web automation."""

    def __init__(self, page: Page) -> None:
        """Initialize with a Playwright Page."""
        self.page = page

    async def observe(self) -> Observation:
        """Capture current UI state from the page."""
        # Get page URL and title
        url = self.page.url
        title = await self.page.title()

        # Capture screenshot
        screenshot_bytes = await self.page.screenshot()

        # Build a minimal a11y tree from role+name pairs accessible via accessibility tree API
        # For now, return a simple placeholder
        tree = A11yNode(role="document", name=title)

        return Observation(
            tree=tree, screenshot_bytes=screenshot_bytes, url=url, title=title
        )

    async def act(self, action: str, target: Any, value: Any = None) -> None:
        """Execute an action on the page."""
        if action == "navigate" and value:
            await self.page.goto(str(value))
        elif action == "click" and target:
            # Target would be a locator string or similar
            await self.page.click(str(target))
        elif action == "type" and target and value:
            await self.page.fill(str(target), str(value))
        else:
            raise ValueError(f"Unsupported action: {action}")

    async def resolve(self, ladder: LocatorLadder) -> LocatorKind:
        """Resolve a locator ladder by trying each rung in order.

        Returns the kind of the rung that matched, or raises if none matched.
        """
        for strategy in ladder.strategies:
            try:
                # Try to find an element matching this strategy
                # For now, return the first strategy's kind as a placeholder
                return strategy.kind
            except Exception:
                # Strategy didn't resolve, try next
                continue

        raise ValueError(
            f"No rung in ladder resolved for strategies: "
            f"{[s.kind for s in ladder.strategies]}"
        )

    async def snapshot_evidence(self, path: str) -> None:
        """Capture evidence to disk."""
        evidence_dir = Path(path)
        evidence_dir.mkdir(parents=True, exist_ok=True)

        # Save screenshot
        screenshot_path = evidence_dir / "screenshot.png"
        await self.page.screenshot(path=str(screenshot_path))

        # Save page content
        content_path = evidence_dir / "page.html"
        content = await self.page.content()
        content_path.write_text(content)
