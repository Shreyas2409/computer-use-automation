"""Normalized observation: a11y tree + screenshot + URL + title.

An Observation represents the state of the UI at a point in time, with enough
information for locator resolution and action target selection. The a11y tree
is normalized: each node carries role, name, and value attributes. Screenshot
bytes are captured separately so they can be versioned and redacted independently
of the tree.

The ``capture_a11y_snapshot`` helper produces a Playwright-snapshot-shaped
nested dict using only APIs supported in Playwright 1.62+ (``page.evaluate``);
it replaces the direct use of ``Page.accessibility.snapshot`` which was
removed in 1.60. Failures propagate — the surface must not silently return
an empty tree.
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


# ---- Accessibility capture ------------------------------------------------

# Single-shot JS that walks the DOM and produces a Playwright-a11y-snapshot-
# shaped tree ({role, name, value, children}). Implicit ARIA roles cover the
# element types the discovery loop cares about; explicit ``role`` attributes
# always win. Non-role wrappers (div, span, main, ...) are unwrapped so their
# role-bearing descendants surface as children of the nearest role-bearing
# ancestor.
_A11Y_CAPTURE_JS = r"""
() => {
  const IMPLICIT = {
    A: (el) => el.hasAttribute('href') ? 'link' : null,
    BUTTON: () => 'button',
    INPUT: (el) => {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'hidden' || t === 'file') return null;
      return 'textbox';
    },
    TEXTAREA: () => 'textbox',
    SELECT: () => 'combobox',
    OPTION: () => 'option',
    H1: () => 'heading', H2: () => 'heading', H3: () => 'heading',
    H4: () => 'heading', H5: () => 'heading', H6: () => 'heading',
    NAV: () => 'navigation',
    MAIN: () => 'main',
    FORM: () => 'form',
    TABLE: () => 'table',
    TR: () => 'row',
    TD: () => 'cell',
    TH: () => 'columnheader',
    DIALOG: () => 'dialog',
    IMG: (el) => el.getAttribute('alt') ? 'img' : null,
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const fn = IMPLICIT[el.tagName];
    return fn ? fn(el) : null;
  };
  const cleanText = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const accessibleName = (el) => {
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al && al.trim()) return cleanText(al);
    const alb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (alb) {
      const parts = alb.split(/\s+/).filter(Boolean).map(id => {
        const t = document.getElementById(id);
        return t ? cleanText(t.textContent) : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      try {
        const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lbl) return cleanText(lbl.textContent);
      } catch (_) { /* invalid id char */ }
    }
    const wrap = el.closest && el.closest('label');
    if (wrap && wrap !== el) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input,select,textarea').forEach(c => c.remove());
      const t = cleanText(clone.textContent);
      if (t) return t;
    }
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      const ph = el.getAttribute('placeholder');
      if (ph && ph.trim()) return cleanText(ph);
    }
    const title = el.getAttribute && el.getAttribute('title');
    if (title && title.trim()) return cleanText(title);
    const alt = el.getAttribute && el.getAttribute('alt');
    if (alt && alt.trim()) return cleanText(alt);
    const tc = cleanText(el.textContent);
    return tc.slice(0, 200);
  };
  const valueOf = (el, role) => {
    if (role === 'textbox' || role === 'combobox') {
      const v = el.value;
      if (v !== undefined && v !== null && String(v).length) return String(v);
    }
    if (role === 'checkbox' || role === 'radio') {
      return el.checked ? 'checked' : null;
    }
    return null;
  };
  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return true;
  };
  const walk = (el) => {
    const children = [];
    for (const child of el.children) {
      const out = walk(child);
      if (out) {
        if (Array.isArray(out)) children.push(...out);
        else children.push(out);
      }
    }
    const role = roleOf(el);
    if (!role) return children;
    if (!isVisible(el)) return children;
    return { role, name: accessibleName(el), value: valueOf(el, role), children };
  };
  const walked = document.body ? walk(document.body) : [];
  const rootChildren = Array.isArray(walked) ? walked : [walked];
  return {
    role: 'document',
    name: document.title || '',
    value: null,
    children: rootChildren,
  };
}
"""


async def capture_a11y_snapshot(
    page: Any, *, interesting_only: bool = True
) -> dict[str, Any]:
    """Capture the accessibility tree using only Playwright-1.62-supported APIs.

    Returns a nested dict with the same shape Playwright's removed
    ``Page.accessibility.snapshot(interesting_only=True)`` used to produce
    (``{role, name, value, children}``). ``interesting_only`` is accepted for
    API symmetry — the underlying DOM walk always omits non-role wrappers.

    Raises whatever ``page.evaluate`` raises. This is deliberate: silent
    empty trees mask real capture failures and were the root cause of the
    discovery loop's MAX_UNCHANGED_OBS stalls.
    """

    return await page.evaluate(_A11Y_CAPTURE_JS)


def a11y_node_from_snapshot(snapshot: dict[str, Any] | None) -> A11yNode | None:
    """Convert a snapshot dict tree into a normalized ``A11yNode`` tree."""

    if not snapshot:
        return None

    def build(node: dict[str, Any]) -> A11yNode:
        raw_children = node.get("children") or []
        children = [build(c) for c in raw_children] if raw_children else None
        return A11yNode(
            role=str(node.get("role", "")),
            name=(node.get("name") or None),
            value=(node.get("value") or None),
            children=children,
        )

    return build(snapshot)
