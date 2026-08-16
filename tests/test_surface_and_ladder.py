"""Tests for Surface abstraction and locator ladder — Wave 3 tests.

Test 1 (brief #2): Ladder falls through correctly when top rung removed.
Test 2 (brief structure): Grep-based test that replay/policy never import Playwright.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from cua.locate import build_ladder
from cua.schema import LocatorStrategy
from cua.surface.observation import (
    A11yNode,
    a11y_node_from_snapshot,
    capture_a11y_snapshot,
)


def test_ladder_fallthrough_when_top_rung_removed():
    """Brief test #2: Ladder falls through when the top rung is removed.

    Build a ladder with role_name → label → css → coordinates.
    When we remove role_name, the ladder should still be valid.
    When we resolve it, it should try label next.
    """
    # Build a complete ladder
    ladder = build_ladder(
        role="button",
        name="Search",
        label_text="Search Members",
        css="button.search",
        coordinates=(100, 50),
    )

    # Verify the ladder has strategies in durability order
    assert len(ladder.strategies) >= 3
    assert ladder.strategies[0].kind == "role_name"
    assert ladder.strategies[1].kind == "label"

    # Now remove the top rung (role_name) and rebuild
    ladder_without_top = build_ladder(
        label_text="Search Members",
        css="button.search",
        coordinates=(100, 50),
    )

    # Verify recorded_match shifted to label (the new top rung)
    assert ladder_without_top.recorded_match == "label"
    assert ladder_without_top.strategies[0].kind == "label"

    # Verify we still have fallthrough rungs
    kinds = [s.kind for s in ladder_without_top.strategies]
    assert "label" in kinds
    assert "css" in kinds
    assert "coordinates" in kinds


def test_no_playwright_import_in_replay():
    """Grep-based test: cua/replay.py must not import Playwright directly.

    This ensures the no-model-in-replay seam is structurally enforced:
    the replay engine can only use abstract surfaces, not concrete drivers.
    """
    repo_root = Path(__file__).resolve().parent.parent
    replay_path = repo_root / "cua" / "replay.py"

    # If replay.py doesn't exist yet, that's okay for this phase
    if not replay_path.exists():
        pytest.skip("replay.py not yet implemented")

    content = replay_path.read_text()
    assert "playwright" not in content.lower(), (
        "replay.py must not import Playwright; use Surface abstraction instead"
    )
    assert "from playwright" not in content, (
        "replay.py must not import Playwright; use Surface abstraction instead"
    )


def test_no_anthropic_import_in_replay():
    """FIX 5 — brief hard constraint: cua/replay.py must not import the Anthropic SDK.

    Deterministic replay is the production path; no model in the decision loop.
    Grep the source (skip cleanly while replay.py doesn't exist yet).
    """
    repo_root = Path(__file__).resolve().parent.parent
    replay_path = repo_root / "cua" / "replay.py"

    if not replay_path.exists():
        pytest.skip("replay.py not yet implemented")

    content = replay_path.read_text()
    assert "anthropic" not in content.lower(), (
        "replay.py must not import the Anthropic SDK; no model in the replay loop"
    )
    assert "from anthropic" not in content, (
        "replay.py must not import the Anthropic SDK; no model in the replay loop"
    )
    assert "import anthropic" not in content, (
        "replay.py must not import the Anthropic SDK; no model in the replay loop"
    )


def test_no_openai_import_in_replay():
    """Brief hard constraint (provider-portable): cua/replay.py must not
    import the OpenAI SDK either.

    Same rationale as the Anthropic guard: deterministic replay must never
    reach for a model client of any flavour. The discovery loop is where
    provider selection lives; replay is model-free.
    """
    repo_root = Path(__file__).resolve().parent.parent
    replay_path = repo_root / "cua" / "replay.py"

    if not replay_path.exists():
        pytest.skip("replay.py not yet implemented")

    content = replay_path.read_text()
    assert "openai" not in content.lower(), (
        "replay.py must not import the OpenAI SDK; no model in the replay loop"
    )
    assert "from openai" not in content, (
        "replay.py must not import the OpenAI SDK; no model in the replay loop"
    )
    assert "import openai" not in content, (
        "replay.py must not import the OpenAI SDK; no model in the replay loop"
    )


def test_no_playwright_import_in_policy():
    """Grep-based test: cua/policy.py must not import Playwright directly.

    Policy gates run in discovery and replay; they must not assume a browser.
    """
    repo_root = Path(__file__).resolve().parent.parent
    policy_path = repo_root / "cua" / "policy.py"

    # If policy.py doesn't exist yet, that's okay for this phase
    if not policy_path.exists():
        pytest.skip("policy.py not yet implemented")

    content = policy_path.read_text()
    assert "playwright" not in content.lower(), (
        "policy.py must not import Playwright; use Surface abstraction instead"
    )
    assert "from playwright" not in content, (
        "policy.py must not import Playwright; use Surface abstraction instead"
    )


def test_surface_implementation_exists():
    """Smoke test: Surface protocol is defined and importable."""
    from cua.surface import Surface, Observation

    assert Surface is not None
    assert Observation is not None


def test_build_ladder_ordering():
    """Verify build_ladder produces strategies in correct durability order."""
    ladder = build_ladder(
        role="textbox",
        name="Member ID",
        label_text="Search by ID",
        text="Member ID",
        css="input#member-search",
        coordinates=(50, 100),
    )

    kinds = [s.kind for s in ladder.strategies]
    # Expected order: role_name, label, text, css, coordinates
    assert kinds[0] == "role_name"
    assert kinds[1] == "label"
    assert kinds[2] == "text"
    assert kinds[3] == "css"
    assert kinds[4] == "coordinates"


def test_build_ladder_recorded_match():
    """Verify recorded_match is set to the first available strategy."""
    # Only label provided
    ladder = build_ladder(label_text="Submit")
    assert ladder.recorded_match == "label"

    # Label and css provided
    ladder = build_ladder(label_text="Submit", css="button.submit")
    assert ladder.recorded_match == "label"

    # When css and coordinates both provided (no higher-durability rungs),
    # css would be primary, which is not allowed
    with pytest.raises(ValueError, match="CSS cannot be the primary"):
        build_ladder(css="button.submit", coordinates=(100, 100))


def test_build_ladder_requires_non_css_first_strategy():
    """Verify CSS cannot be the only or primary locator."""
    with pytest.raises(ValueError, match="CSS cannot be the primary"):
        build_ladder(css="button.search")


# ---- A11y capture (Playwright 1.62 compat) --------------------------------


class _FakePage:
    """Minimal stand-in for a Playwright Page with an ``evaluate`` coroutine."""

    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[str] = []

    async def evaluate(self, js: str):
        self.calls.append(js)
        if self._exc is not None:
            raise self._exc
        return self._result


def test_capture_a11y_snapshot_delegates_to_page_evaluate():
    snapshot = {
        "role": "document",
        "name": "Sign In",
        "value": None,
        "children": [
            {"role": "textbox", "name": "Username", "value": "demo",
             "children": []},
            {"role": "button", "name": "Sign In", "value": None,
             "children": []},
        ],
    }
    page = _FakePage(result=snapshot)
    got = asyncio.run(capture_a11y_snapshot(page))
    assert got == snapshot
    assert len(page.calls) == 1
    # Sanity: the JS actually walks the DOM (no reliance on removed APIs).
    assert "document.body" in page.calls[0]
    assert "accessibility" not in page.calls[0]


def test_capture_a11y_snapshot_is_loud_on_failure():
    page = _FakePage(exc=RuntimeError("evaluate exploded"))
    with pytest.raises(RuntimeError, match="evaluate exploded"):
        asyncio.run(capture_a11y_snapshot(page))


def test_a11y_node_from_snapshot_builds_tree():
    snapshot = {
        "role": "document", "name": "T", "value": None,
        "children": [
            {"role": "form", "name": "Login", "value": None, "children": [
                {"role": "textbox", "name": "Username", "value": "demo",
                 "children": []},
            ]},
        ],
    }
    tree = a11y_node_from_snapshot(snapshot)
    assert isinstance(tree, A11yNode)
    assert tree.role == "document" and tree.name == "T"
    assert tree.children and tree.children[0].role == "form"
    assert tree.children[0].children[0].role == "textbox"
    assert tree.children[0].children[0].value == "demo"


def test_a11y_node_from_snapshot_none_returns_none():
    assert a11y_node_from_snapshot(None) is None
    assert a11y_node_from_snapshot({}) is None


def test_playwright_compat_shim_installs_accessibility_property():
    """Importing cua.surface must patch Page.accessibility (Playwright 1.60+)."""
    import cua.surface  # noqa: F401  # side-effect import
    from playwright.async_api import Page

    assert hasattr(Page, "accessibility"), (
        "Page.accessibility should be present after cua.surface import"
    )
    prop = Page.__dict__.get("accessibility")
    assert isinstance(prop, property), (
        "accessibility should be a property descriptor installed by the shim"
    )


def test_playwright_compat_shim_snapshot_uses_capture_helper():
    """The shim's snapshot() delegates to capture_a11y_snapshot on the bound page."""
    import cua.surface  # noqa: F401  # ensures shim is installed
    from cua.surface._playwright_compat import _AccessibilityShim

    snapshot = {"role": "document", "name": "x", "value": None, "children": []}
    page = _FakePage(result=snapshot)
    shim = _AccessibilityShim(page)
    got = asyncio.run(shim.snapshot(interesting_only=True))
    assert got == snapshot
    assert len(page.calls) == 1


def test_playwright_compat_shim_snapshot_propagates_errors():
    import cua.surface  # noqa: F401
    from cua.surface._playwright_compat import _AccessibilityShim

    page = _FakePage(exc=ValueError("boom"))
    shim = _AccessibilityShim(page)
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(shim.snapshot())
