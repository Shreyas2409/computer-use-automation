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
5. css — CSS selector
6. coordinates — x,y pixels (least durable; layout changes break it)

CSS is never used as the primary/first rung because:
- Highly fragile to class/style changes
- Hard to audit for robustness
- Accessibility attributes are more stable
"""

from __future__ import annotations

from typing import Any

from cua.schema import LocatorKind, LocatorLadder, LocatorStrategy


def build_ladder(
    role: str | None = None,
    name: str | None = None,
    label_text: str | None = None,
    text: str | None = None,
    table_cell: dict[str, Any] | None = None,
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

    # CSS can only be added if we already have a higher-durability primary rung
    if css:
        if recorded_match is None:
            raise ValueError(
                "CSS cannot be the primary locator strategy; provide at least one "
                "higher-durability strategy (role_name, label, text, or table_cell)"
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
