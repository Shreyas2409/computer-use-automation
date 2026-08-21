"""Locator ladder: build and resolve logic.

A locator ladder is an ordered list of strategies to find an element, ordered by
durability (resistance to UI changes). At record time, the discovery model finds
an element and we build a ladder. At replay time, we try each strategy top-down
and record which one matched. A mismatch between recorded and replay rung is the
drift signal.

Rung order (durability, most to least durable):
1. role_name — accessibility role + accessible name (most durable, least fragile to layout)
2. label — <label> element's text
3. text — visible text in the element
4. table_cell — table row+column headers (for table cells)
5. label_cell — legacy table-form field with no <label>/aria-label at all
   (plain adjacent cell text); resolved as the control in the same row
6. css — CSS selector
7. coordinates — x,y pixels (least durable; layout changes break it)

CSS is never used as the primary/first rung because:
- Highly fragile to class/style changes
- Hard to audit for robustness
- Accessibility attributes are more stable
"""

from __future__ import annotations

import re
from typing import Any

from cua.schema import LocatorKind, LocatorLadder, LocatorStrategy

# A bare HTML tag name with no id/class/attribute/combinator qualifier
# (e.g. "td", "input", "a") — matches every element of that tag on the
# page, so it can never fail to resolve to *something* and can never be
# trusted to be the *right* something. "#id", ".class", "tr td:nth-child(2)"
# etc. are all narrower than this and pass through untouched.
_BARE_TAG_SELECTOR = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*$")


def label_cell_pattern(label_text: str) -> re.Pattern[str]:
    """Regex matching a label cell's text, normalized against trailing punctuation.

    Legacy table-form labels are inconsistently punctuated across this kind
    of site — "Operator ID:", "Confirmation:", but also a bare share ID used
    as a row label with a trailing colon ("100234-S0070:") that the model
    (or a caller) may reasonably supply without it. Matching on the label
    text with any trailing `:`/`.` and surrounding whitespace stripped —
    rather than patching one exact string at a time — is what actually
    protects every label lookup on the site, not just the one that broke.
    Leading punctuation/whitespace is deliberately left alone: nothing
    observed on this target puts stray characters before a label.
    """

    return re.compile(r"^\s*" + re.escape(label_text) + r"\s*[:.]?\s*$")


def build_ladder(
    role: str | None = None,
    name: str | None = None,
    label_text: str | None = None,
    text: str | None = None,
    table_cell: dict[str, Any] | None = None,
    label_cell: dict[str, Any] | None = None,
    css: str | None = None,
    coordinates: tuple[int, int] | None = None,
) -> LocatorLadder:
    """Build a locator ladder from available locator strategies.

    Strategies are added in durability order (most durable first). The first
    non-None strategy becomes the recorded_match (the rung that matched at
    record time). CSS is NEVER the primary rung; it can only appear after
    higher-durability strategies.

    Args:
        role: Accessibility role (e.g., "button")
        name: Accessible name (e.g., "Search")
        label_text: Text of a <label> element
        text: Visible text content
        table_cell: Dict with table headers (e.g., {"row": "...", "column": "..."})
        label_cell: Dict with the adjacent-cell label text (e.g.,
            {"label_text": "Operator ID:"}) for legacy table-form fields
            with no real <label>/aria-label association
        css: CSS selector
        coordinates: (x, y) tuple

    Returns:
        A LocatorLadder with strategies in durability order and recorded_match set.

    Raises:
        ValueError: If no strategies provided or if CSS would be primary.
    """
    strategies = []
    recorded_match = None

    # Rung order: role_name, label, text, table_cell, css, coordinates
    if role and name:
        strategies.append(
            LocatorStrategy(kind="role_name", spec={"role": role, "name": name}, durability=95)
        )
        if recorded_match is None:
            recorded_match = "role_name"

    if label_text:
        strategies.append(
            LocatorStrategy(kind="label", spec={"label": label_text}, durability=85)
        )
        if recorded_match is None:
            recorded_match = "label"

    if text:
        strategies.append(
            LocatorStrategy(kind="text", spec={"text": text}, durability=75)
        )
        if recorded_match is None:
            recorded_match = "text"

    if table_cell:
        strategies.append(
            LocatorStrategy(kind="table_cell", spec=table_cell, durability=70)
        )
        if recorded_match is None:
            recorded_match = "table_cell"

    if label_cell:
        strategies.append(
            LocatorStrategy(kind="label_cell", spec=label_cell, durability=65)
        )
        if recorded_match is None:
            recorded_match = "label_cell"

    # CSS can only be added if we already have a higher-durability primary rung
    if css:
        if recorded_match is None:
            raise ValueError(
                "CSS cannot be the primary locator strategy; provide at least one "
                "higher-durability strategy (role_name, label, text, or table_cell)"
            )
        if _BARE_TAG_SELECTOR.match(css.strip()):
            raise ValueError(
                f"CSS selector {css!r} is a bare tag name with no id/class/"
                "attribute/combinator qualifier — it will match every element "
                "of that tag on the page (e.g. every <td>), so a fallback "
                "match can never fail and can never be trusted to be the "
                "right element. Narrow it (an id, a class, a scoped "
                "compound selector like 'table#x tr:nth-child(2)') or drop "
                "to coordinates instead."
            )
        strategies.append(
            LocatorStrategy(kind="css", spec={"selector": css}, durability=40)
        )

    if coordinates:
        strategies.append(
            LocatorStrategy(
                kind="coordinates", spec={"x": coordinates[0], "y": coordinates[1]}, durability=20
            )
        )

    if not strategies:
        raise ValueError("build_ladder requires at least one locator strategy")

    if recorded_match is None:
        raise ValueError("Could not determine recorded_match")

    return LocatorLadder(strategies=strategies, recorded_match=recorded_match)


async def resolve_ladder(ladder: LocatorLadder, resolver: Any) -> LocatorKind:
    """Resolve a ladder by trying each rung in order.

    At replay time, try each strategy top-down until one resolves to a visible
    element. Return the kind of the first strategy that resolved.

    Args:
        ladder: LocatorLadder with strategies in durability order
        resolver: An object with a resolve_strategy(strategy) method (e.g., a Surface)

    Returns:
        The LocatorKind of the strategy that matched.

    Raises:
        ValueError: If no rung in the ladder resolved.
    """
    for strategy in ladder.strategies:
        try:
            await resolver.resolve_strategy(strategy)
            return strategy.kind
        except Exception:
            # Strategy didn't resolve, try next rung
            continue

    raise ValueError(
        f"Ladder resolution failed: no rung resolved. "
        f"Strategies tried: {[s.kind for s in ladder.strategies]}"
    )
