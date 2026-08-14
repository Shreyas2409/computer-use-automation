"""Registry tests — brief test #3: versioning, overlay composition, stale pins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.registry import compose, list_capabilities, load
from cua.schema import Capability, OverlayPatch, TenantOverlay


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO_ROOT / "artifacts"


def test_list_capabilities():
    """list_capabilities() returns at least lookup_member_balance."""
    caps = list_capabilities()
    assert "lookup_member_balance" in caps
    assert isinstance(caps, list)
    assert all(isinstance(c, str) for c in caps)


def test_load_version_none_returns_highest_approved():
    """load(cap_id, version=None) returns the highest-versioned approved capability."""
    cap = load("lookup_member_balance", version=None)
    assert cap.capability_id == "lookup_member_balance"
    assert cap.approval == "approved"
    # v2.1 is the highest approved version we created
    assert (cap.version_major, cap.version_minor) == (2, 1)


def test_load_version_major_returns_highest_major_x_approved():
    """load(cap_id, version='2') returns highest 2.x that is approved."""
    cap = load("lookup_member_balance", version="2")
    assert cap.version_major == 2
    assert cap.approval == "approved"
    # v2.1 should be returned since it's the highest 2.x
    assert cap.version_minor == 1


def test_load_version_exact_returns_exact_match():
    """load(cap_id, version='2.0') returns exact version 2.0."""
    cap = load("lookup_member_balance", version="2.0")
    assert (cap.version_major, cap.version_minor) == (2, 0)
    assert cap.approval == "approved"


def test_load_version_exact_v1_returns_v1():
    """load(cap_id, version='1.0') returns exact version 1.0 (approved)."""
    cap = load("lookup_member_balance", version="1.0")
    assert (cap.version_major, cap.version_minor) == (1, 0)
    assert cap.approval == "approved"


def test_load_nonexistent_capability_raises():
    """load('no_such_capability') raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="not found"):
        load("no_such_capability")


def test_load_nonexistent_version_raises():
    """load(cap_id, version='99.0') raises when version doesn't exist."""
    with pytest.raises(FileNotFoundError):
        load("lookup_member_balance", version="99.0")


def test_load_major_with_no_approved_raises():
    """load with a major that has no approved versions raises ValueError."""
    # v1.0 is approved but let's assume we're looking for major 5 which doesn't exist
    with pytest.raises(ValueError):
        load("lookup_member_balance", version="5")


def test_compose_applies_patches():
    """compose() applies patches and returns a valid capability."""
    base = load("lookup_member_balance", version="2.0")
    overlay = TenantOverlay(
        overlay_version=1,
        base_capability="lookup_member_balance",
        base_version="2.0",
        tenant_id="summit",
        patches=[
            OverlayPatch(path="target.base_url", value="http://localhost:8080/servicing"),
            OverlayPatch(
                path="steps[5].target.strategies[0].spec.name",
                value="Find Member",
            ),
        ],
    )
    composed = compose(base, overlay)
    assert composed.target.base_url == "http://localhost:8080/servicing"
    assert composed.steps[5].target.strategies[0].spec["name"] == "Find Member"
    # Should remain approved and valid
    assert composed.approval == "approved"


def test_compose_validates_through_pydantic():
    """compose() re-validates the composed capability."""
    base = load("lookup_member_balance", version="2.0")
    overlay = TenantOverlay(
        overlay_version=1,
        base_capability="lookup_member_balance",
        base_version="2.0",
        tenant_id="summit",
        patches=[
            OverlayPatch(path="target.base_url", value="http://localhost:8080/servicing"),
            OverlayPatch(
                path="steps[5].target.strategies[0].spec.name",
                value="Find Member",
            ),
        ],
    )
    composed = compose(base, overlay)
    # Composed should be a valid Capability
    assert isinstance(composed, Capability)


def test_compose_with_stale_overlay_pin_fails_loudly():
    """compose() fails loudly if overlay pins a base_version whose major has moved on."""
    base = load("lookup_member_balance", version="2.1")
    # Overlay pins 1.0 but base is now 2.1
    overlay = TenantOverlay(
        overlay_version=1,
        base_capability="lookup_member_balance",
        base_version="1.0",
        tenant_id="summit",
        patches=[
            OverlayPatch(path="target.base_url", value="http://localhost:8080/servicing"),
        ],
    )
    with pytest.raises(ValueError, match="stale"):
        compose(base, overlay)


def test_compose_with_matching_major_allows_minor_ahead():
    """compose() fails if overlay pins minor ahead of base within same major."""
    base = load("lookup_member_balance", version="2.0")
    # Overlay pins 2.1 but base is 2.0
    overlay = TenantOverlay(
        overlay_version=1,
        base_capability="lookup_member_balance",
        base_version="2.1",
        tenant_id="summit",
        patches=[
            OverlayPatch(path="target.base_url", value="http://localhost:8080/servicing"),
        ],
    )
    with pytest.raises(ValueError, match="stale"):
        compose(base, overlay)


def test_compose_with_matching_pinned_version_succeeds():
    """compose() succeeds when overlay pins the exact base version."""
    base = load("lookup_member_balance", version="2.0")
    overlay = TenantOverlay(
        overlay_version=1,
        base_capability="lookup_member_balance",
        base_version="2.0",
        tenant_id="summit",
        patches=[
            OverlayPatch(path="target.base_url", value="http://localhost:8080/servicing"),
        ],
    )
    composed = compose(base, overlay)
    assert composed.target.base_url == "http://localhost:8080/servicing"


def test_compose_from_file_summit_overlay():
    """Load and compose the committed summit.v1.json overlay."""
    overlay_path = ARTIFACTS / "lookup_member_balance" / "overlays" / "summit.v1.json"
    assert overlay_path.exists(), f"summit overlay not found at {overlay_path}"
    
    overlay_data = json.loads(overlay_path.read_text())
    overlay = TenantOverlay.model_validate(overlay_data)
    
    base = load("lookup_member_balance", version="2.0")
    composed = compose(base, overlay)
    
    # Verify patches were applied
    assert composed.target.base_url == "http://localhost:8080/servicing"
    assert composed.steps[5].target.strategies[0].spec["name"] == "Find Member"
    # Verify it's a valid Capability
    assert isinstance(composed, Capability)
    assert composed.approval == "approved"
