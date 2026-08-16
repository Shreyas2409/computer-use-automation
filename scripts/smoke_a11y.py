"""Live smoke check: verify capture_a11y_snapshot returns a populated tree.

Usage: .venv/bin/python scripts/smoke_a11y.py <base_url>

Loads /login, logs in as demo/demo123, walks to /members, and prints the
count of interesting nodes captured at each step. Exits non-zero if any
capture is empty. Also verifies the discover.py path (page.accessibility
.snapshot via the compat shim) is populated.
"""

from __future__ import annotations

import asyncio
import sys

import cua.surface  # noqa: F401  # installs Playwright compat shim
from cua.discover import flatten_a11y_tree
from cua.surface.observation import capture_a11y_snapshot


async def _log_page(page, label: str, prefix: str) -> int:
    snapshot = await capture_a11y_snapshot(page)
    nodes = flatten_a11y_tree(snapshot)
    shim_snapshot = await page.accessibility.snapshot(interesting_only=True)
    shim_nodes = flatten_a11y_tree(shim_snapshot)
    print(
        f"  {label}: capture={len(nodes)} nodes | "
        f"shim={len(shim_nodes)} nodes | url={page.url}"
    )
    if not nodes or not shim_nodes:
        raise SystemExit(f"EMPTY tree at {label} ({prefix or '/'})")
    return len(nodes)


async def run(base_url: str, prefix: str) -> None:
    from playwright.async_api import async_playwright

    print(f"[{base_url}] prefix={prefix or '/'}")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await ctx.new_page()
            await page.goto(base_url + "/login", wait_until="domcontentloaded")
            await _log_page(page, "login", prefix)
            await page.fill('input[name="ctl00_MainContent_txtUsername"]', "demo")
            await page.fill('input[name="ctl00_MainContent_txtPassword"]', "demo123")
            await _log_page(page, "login (filled)", prefix)
            await page.click('input[type="submit"], button[type="submit"]')
            await page.wait_for_load_state("domcontentloaded")
            await _log_page(page, "members", prefix)
        finally:
            await browser.close()


async def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    prefix = sys.argv[2] if len(sys.argv) > 2 else ""
    await run(base_url, prefix)


if __name__ == "__main__":
    asyncio.run(main())
