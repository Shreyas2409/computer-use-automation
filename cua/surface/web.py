"""Playwright-based Surface implementation.

This is the ONLY module in ``cua`` that imports Playwright. The replay engine
and policy gate consume the ``ReplaySurface`` protocol declared in
``cua.replay``; this module structurally satisfies it and adds a few extras
the original ``Surface`` protocol (observe/act/resolve/snapshot_evidence) needs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    async_playwright,
)

from cua.schema import (
    Detector,
    ElementPresentDetector,
    ExtractSpec,
    LocatorKind,
    LocatorLadder,
    LocatorStrategy,
    TextMatchDetector,
    UrlPatternDetector,
)
from cua.surface.observation import A11yNode, Observation


DEFAULT_TIMEOUT_MS = 6000

_DISMISS_BUTTONS = (
    "Acknowledge",
    "Dismiss",
    "Continue",
    "OK",
    "Close",
    "Got it",
    "I understand",
)


class LocatorNotFoundError(RuntimeError):
    """No rung in the ladder resolved to a visible element."""


class WebSurface:
    """Playwright-backed Surface. Structurally satisfies ReplaySurface."""

    def __init__(self, page: Page) -> None:
        self.page = page

    def current_url(self) -> str:
        return self.page.url

    async def page_text(self) -> str:
        try:
            return await self.page.locator("body").inner_text()
        except Exception:
            try:
                return await self.page.content()
            except Exception:
                return ""

    async def observe(self) -> Observation:
        url = self.page.url
        title = await self.page.title()
        try:
            screenshot = await self.page.screenshot()
        except Exception:
            screenshot = b""
        return Observation(
            tree=A11yNode(role="document", name=title),
            screenshot_bytes=screenshot,
            url=url,
            title=title,
        )

    async def screenshot(self, path: str) -> None:
        try:
            await self.page.screenshot(path=path)
        except Exception:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"")

    async def snapshot_evidence(self, path: str) -> None:
        evidence_dir = Path(path)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self.page.screenshot(path=str(evidence_dir / "screenshot.png"))
        except Exception:
            pass
        try:
            (evidence_dir / "page.html").write_text(await self.page.content())
        except Exception:
            pass

    async def _resolve_strategy(
        self, strategy: LocatorStrategy, timeout_ms: int
    ) -> Locator | None:
        try:
            locator = self._locator_for(strategy)
        except Exception:
            return None
        if locator is None:
            return None
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return None
        return locator

    def _locator_for(self, strategy: LocatorStrategy) -> Locator | None:
        spec = strategy.spec
        kind = strategy.kind
        if kind == "role_name":
            return self.page.get_by_role(
                spec["role"], name=spec["name"], exact=True
            ).first
        if kind == "label":
            return self.page.get_by_label(spec["label"], exact=True).first
        if kind == "text":
            return self.page.get_by_text(spec["text"], exact=True).first
        if kind == "table_cell":
            return self._table_cell_locator(spec)
        if kind == "css":
            return self.page.locator(spec["selector"]).first
        if kind == "coordinates":
            return None
        raise ValueError(f"unknown locator kind: {kind}")

    def _table_cell_locator(self, spec: dict[str, Any]) -> Locator | None:
        table_id = spec.get("table_id")
        if not table_id:
            return None
        rh = spec.get("row_header") or {}
        column = spec.get("column")

        if "column" in rh and "equals" in rh:
            js = """
            (args) => {
              const [tableId, headerCol, needle, targetCol] = args;
              const t = document.getElementById(tableId);
              if (!t) return null;
              const headers = Array.from(t.querySelectorAll('tr:first-child th, tr:first-child td'));
              const headerText = headers.map(h => (h.textContent || '').trim());
              const iSel = headerText.findIndex(x => x === headerCol);
              const iTgt = headerText.findIndex(x => x === targetCol);
              if (iSel < 0 || iTgt < 0) return null;
              const rows = Array.from(t.querySelectorAll('tr')).slice(1);
              for (const row of rows) {
                const cells = row.querySelectorAll('td');
                if (cells.length <= Math.max(iSel, iTgt)) continue;
                if ((cells[iSel].textContent || '').trim() === needle) {
                  return (cells[iTgt].textContent || '').trim();
                }
              }
              return null;
            }
            """
            return _EvalLocator(
                self.page, js, [table_id, rh["column"], rh["equals"], column]
            )  # type: ignore[return-value]
        if "th_text" in rh:
            offset = int(spec.get("column_offset", 1))
            js = """
            (args) => {
              const [tableId, thText, offset] = args;
              const t = document.getElementById(tableId);
              if (!t) return null;
              const rows = Array.from(t.querySelectorAll('tr'));
              for (const row of rows) {
                const cells = Array.from(row.children);
                for (let i = 0; i < cells.length; i++) {
                  const c = cells[i];
                  if (c.tagName === 'TH' && (c.textContent || '').trim() === thText) {
                    const target = cells[i + offset];
                    if (target) return (target.textContent || '').trim();
                  }
                }
              }
              return null;
            }
            """
            return _EvalLocator(
                self.page, js, [table_id, rh["th_text"], offset]
            )  # type: ignore[return-value]
        return None

    async def resolve(self, ladder: LocatorLadder) -> LocatorKind:
        for strategy in ladder.strategies:
            loc = await self._resolve_strategy(strategy, DEFAULT_TIMEOUT_MS)
            if loc is not None:
                return strategy.kind
        raise LocatorNotFoundError(
            f"no rung resolved: kinds={[s.kind for s in ladder.strategies]}"
        )

    async def _find_ladder(
        self, ladder: LocatorLadder, timeout_ms: int
    ) -> tuple[Locator, LocatorKind]:
        for strategy in ladder.strategies:
            loc = await self._resolve_strategy(strategy, timeout_ms)
            if loc is not None:
                return loc, strategy.kind
        raise LocatorNotFoundError(
            f"no rung resolved: kinds={[s.kind for s in ladder.strategies]}"
        )

    async def navigate(
        self, url: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> None:
        await self.page.goto(
            url, wait_until="domcontentloaded", timeout=timeout_ms
        )

    async def click(
        self, ladder: LocatorLadder, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> LocatorKind:
        loc, kind = await self._find_ladder(ladder, timeout_ms)
        await loc.click(timeout=timeout_ms)
        try:
            await self.page.wait_for_load_state(
                "domcontentloaded", timeout=timeout_ms
            )
        except Exception:
            pass
        return kind

    async def fill(
        self,
        ladder: LocatorLadder,
        value: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> LocatorKind:
        loc, kind = await self._find_ladder(ladder, timeout_ms)
        await loc.fill(value, timeout=timeout_ms)
        return kind

    async def select(
        self,
        ladder: LocatorLadder,
        value: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> LocatorKind:
        loc, kind = await self._find_ladder(ladder, timeout_ms)
        await loc.select_option(label=value, timeout=timeout_ms)
        return kind

    async def wait_for(
        self, ladder: LocatorLadder, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> LocatorKind:
        _, kind = await self._find_ladder(ladder, timeout_ms)
        return kind

    async def read(
        self, ladder: LocatorLadder, *, timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> str:
        loc, _ = await self._find_ladder(ladder, timeout_ms)
        text = await loc.text_content(timeout=timeout_ms)
        return (text or "").strip()

    async def act(self, action: str, target: Any, value: Any = None) -> None:
        if action == "navigate":
            await self.navigate(str(value))
            return
        if isinstance(target, LocatorLadder):
            if action == "click":
                await self.click(target)
                return
            if action == "type":
                await self.fill(target, str(value))
                return
            if action == "select":
                await self.select(target, str(value))
                return
            if action in ("wait_for", "assert"):
                await self.wait_for(target)
                return
            if action == "read":
                await self.read(target)
                return
        raise ValueError(f"unsupported (action, target) pair: {action}")

    async def check_detector(self, detector: Detector) -> bool:
        if isinstance(detector, TextMatchDetector):
            text = await self.page_text()
            needle = detector.contains
            if not detector.case_sensitive:
                return needle.lower() in (text or "").lower()
            return needle in (text or "")
        if isinstance(detector, UrlPatternDetector):
            import re

            return re.search(detector.pattern, self.page.url) is not None
        if isinstance(detector, ElementPresentDetector):
            try:
                await self._find_ladder(detector.locator, 1500)
                return True
            except Exception:
                return False
        return False

    async def extract(self, spec: ExtractSpec) -> str | None:
        try:
            loc, _ = await self._find_ladder(spec.locator, DEFAULT_TIMEOUT_MS)
        except Exception:
            return None
        try:
            text = await loc.text_content(timeout=DEFAULT_TIMEOUT_MS)
        except Exception:
            return None
        return (text or "").strip()

    async def dismiss_top_dialog(self) -> bool:
        """Remove any interstitial modal without destroying surrounding state.

        We deliberately prefer DOM removal over clicking the modal's submit
        button: real interstitials in this class of legacy apps use a GET
        form whose submission wipes the surrounding page's query/form state,
        which would break the very step we're recovering from. If nothing
        looks like a modal, fall back to a real click on known dismiss
        buttons for cases where a click really is the right thing.

        BLOCKER 1: the target-app dialog is server-driven via ``?inject=dialog``
        and preserved through the members-page form action, so a DOM-only wipe
        lets the modal reappear on the next POST. We also strip
        ``inject=dialog`` from the current URL (history.replaceState) and from
        any ``<form action>`` / ``<a href>`` on the page. The recovery stays
        bounded (one call per rule/step) and stays logged upstream — this call
        just neutralises the underlying trigger so the retry can complete.
        """

        try:
            removed = await self.page.evaluate(
                """() => {
                    const sels = ['.modal-panel', '.modal-backdrop', '[role=dialog]'];
                    let hit = 0;
                    for (const s of sels) {
                        document.querySelectorAll(s).forEach(el => {
                            el.remove();
                            hit += 1;
                        });
                    }
                    // Strip inject=dialog from the URL and any form/link that
                    // would carry it into the next request.
                    try {
                        const stripQuery = (raw) => {
                            if (!raw) return raw;
                            try {
                                const u = new URL(raw, window.location.href);
                                if (u.searchParams.has('inject')) {
                                    if (u.searchParams.get('inject') === 'dialog') {
                                        u.searchParams.delete('inject');
                                    }
                                }
                                const qs = u.searchParams.toString();
                                return u.pathname + (qs ? '?' + qs : '') + u.hash;
                            } catch (_) { return raw; }
                        };
                        const here = new URL(window.location.href);
                        if (here.searchParams.get('inject') === 'dialog') {
                            here.searchParams.delete('inject');
                            const qs = here.searchParams.toString();
                            const newUrl = here.pathname + (qs ? '?' + qs : '') + here.hash;
                            window.history.replaceState({}, '', newUrl);
                        }
                        document.querySelectorAll('form[action]').forEach(f => {
                            f.setAttribute('action', stripQuery(f.getAttribute('action')));
                        });
                        document.querySelectorAll('a[href]').forEach(a => {
                            a.setAttribute('href', stripQuery(a.getAttribute('href')));
                        });
                    } catch (_) { /* best-effort */ }
                    return hit;
                }"""
            )
            if removed:
                return True
        except Exception:
            pass
        for label in _DISMISS_BUTTONS:
            for selector in (
                lambda l=label: self.page.get_by_role(
                    "button", name=l, exact=True
                ).first,
                lambda l=label: self.page.locator(
                    f'input[type=submit][value="{l}"]'
                ).first,
            ):
                try:
                    loc = selector()
                    await loc.wait_for(state="visible", timeout=500)
                    await loc.click(timeout=1500, force=True)
                    try:
                        await self.page.wait_for_load_state(
                            "domcontentloaded", timeout=2000
                        )
                    except Exception:
                        pass
                    return True
                except Exception:
                    continue
        return False


class _EvalLocator:
    """Adapter mimicking the slice of Playwright's Locator API we need.

    Only ``wait_for`` and ``text_content`` are supported; click/fill/select
    raise. Table cells are extract-only by design.
    """

    def __init__(self, page: Page, js: str, args: list[Any]) -> None:
        self._page = page
        self._js = js
        self._args = args

    async def wait_for(
        self, state: str = "visible", timeout: int = DEFAULT_TIMEOUT_MS
    ) -> None:
        import asyncio

        elapsed = 0
        interval = 100
        while elapsed <= timeout:
            val = await self._page.evaluate(self._js, self._args)
            if val is not None:
                return
            await asyncio.sleep(interval / 1000.0)
            elapsed += interval
        raise LocatorNotFoundError("table_cell evaluator returned no match")

    async def text_content(
        self, timeout: int = DEFAULT_TIMEOUT_MS
    ) -> str | None:
        return await self._page.evaluate(self._js, self._args)

    async def click(self, timeout: int = DEFAULT_TIMEOUT_MS) -> None:
        raise NotImplementedError(
            "table_cell locators are extract-only; declare a css rung to click"
        )

    async def fill(self, value: str, timeout: int = DEFAULT_TIMEOUT_MS) -> None:
        raise NotImplementedError(
            "table_cell locators are extract-only; declare a css rung to fill"
        )

    async def select_option(
        self, label: str, timeout: int = DEFAULT_TIMEOUT_MS
    ) -> None:
        raise NotImplementedError("table_cell locators do not support select")


@asynccontextmanager
async def open_web_surface(
    *,
    headless: bool = True,
    viewport: tuple[int, int] = (1280, 900),
    locale: str = "en-US",
    timezone_id: str = "UTC",
    remote_debugging_port: int | None = None,
):
    """Async context manager: opens Chromium, yields a ``WebSurface``.

    When ``remote_debugging_port`` is set, Chromium is launched with
    ``--remote-debugging-port=<port>`` so the operator surface can attach to
    the same live session via CDP (see ``connect_web_surface``). Requires a
    headed browser to be useful in practice — CDP against a headless
    instance still works, it's just not what the operator UI expects.
    """

    launch_kwargs: dict[str, Any] = {"headless": headless}
    if remote_debugging_port is not None:
        launch_kwargs["args"] = [
            f"--remote-debugging-port={remote_debugging_port}",
        ]
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(**launch_kwargs)
        try:
            context: BrowserContext = await browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                locale=locale,
                timezone_id=timezone_id,
            )
            page = await context.new_page()
            yield WebSurface(page)
        finally:
            await browser.close()


@asynccontextmanager
async def connect_web_surface(
    *,
    cdp_endpoint: str,
):
    """Attach to an already-running Chromium exposing a CDP endpoint.

    ``cdp_endpoint`` is either the plain ``http://host:port`` root that
    ``--remote-debugging-port=port`` exposes, or the ``ws://.../devtools/...``
    URL. Reuses the first context and its first page so the operator sees
    exactly what the automation was driving.
    """

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.connect_over_cdp(cdp_endpoint)
        try:
            context = (
                browser.contexts[0]
                if browser.contexts
                else await browser.new_context()
            )
            page = context.pages[0] if context.pages else await context.new_page()
            yield WebSurface(page)
        finally:
            # We only detach; the browser lifetime belongs to whoever launched
            # it via ``--remote-debugging-port``. Closing it here would kill
            # the automation side too.
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
