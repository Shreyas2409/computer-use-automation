"""Playwright compatibility shim for the discovery loop's a11y capture.

Playwright 1.60 removed ``Page.accessibility``. The discovery loop (which
is out of scope for edits in this task) still calls
``page.accessibility.snapshot(interesting_only=True)``. Rather than teach
the loop to call the surface layer directly, we restore the old accessor
here and delegate to :func:`cua.surface.observation.capture_a11y_snapshot`,
which uses only Playwright-1.62-supported APIs.

Importing this module has the side effect of patching ``Page`` once. It is
safe to import multiple times and is idempotent.
"""

from __future__ import annotations

from typing import Any

from cua.surface.observation import capture_a11y_snapshot


class _AccessibilityShim:
    """Compat surface for ``page.accessibility.snapshot(interesting_only=...)``.

    Owns no state beyond the bound page; instantiated per-attribute access
    via the descriptor below.
    """

    __slots__ = ("_page",)

    def __init__(self, page: Any) -> None:
        self._page = page

    async def snapshot(
        self, *, interesting_only: bool = True
    ) -> dict[str, Any]:
        """Delegate to the supported DOM-walk capture.

        Failures propagate: the discovery loop's own bare ``except`` wraps
        this call, so we cannot force the top-level to raise, but we do not
        add our own swallow — the surface layer's contract stays loud.
        """

        return await capture_a11y_snapshot(
            self._page, interesting_only=interesting_only
        )


def _accessibility_getter(self: Any) -> _AccessibilityShim:
    return _AccessibilityShim(self)


def install() -> None:
    """Idempotently install the ``Page.accessibility`` compat property.

    Only patches when the attribute is missing (Playwright <=1.59 keeps its
    native ``Page.accessibility`` and we do not want to shadow it).
    """

    from playwright.async_api import Page as AsyncPage

    for cls in (AsyncPage,):
        if not hasattr(cls, "accessibility"):
            cls.accessibility = property(_accessibility_getter)  # type: ignore[attr-defined]

    # Sync Page is optional (not imported by the discovery loop), but keep
    # it in step so any downstream usage stays consistent.
    try:
        from playwright.sync_api import Page as SyncPage  # noqa: WPS433
    except Exception:  # pragma: no cover — sync_api is always available in 1.62
        return
    if not hasattr(SyncPage, "accessibility"):
        SyncPage.accessibility = property(_accessibility_getter)  # type: ignore[attr-defined]


install()
