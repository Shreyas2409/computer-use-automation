"""Artifact registry: load/compose/resolve versioned, immutable capabilities.

Artifacts are stored as immutable files under artifacts/<capability_id>/vX.Y.json.
Tenant overlays are thin patch sets at artifacts/<capability_id>/overlays/<tenant>.vN.json.

This module owns:
- list_capabilities() — enumerate all capabilities in the registry
- load(capability_id, version=None) — resolve versions: None → highest approved,
  "2" → highest 2.x, "2.1" → exact match
- compose(base, overlay) — apply patches to a capability, re-validate through
  Pydantic, and fail loudly if overlay pins a base_version whose major has moved on
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from cua.schema import Capability, TenantOverlay


def _get_artifacts_dir() -> Path:
    """Return the artifacts directory (repo root / artifacts)."""
    return Path(__file__).resolve().parent.parent / "artifacts"


def _parse_version(version_str: str) -> tuple[int, int]:
    """Parse a version string 'X.Y' into (major, minor)."""
    parts = version_str.split(".")
    if len(parts) != 2:
        raise ValueError(f"Invalid version format: {version_str!r}")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid version format: {version_str!r}")


def list_capabilities() -> list[str]:
    """List all capability_ids in the registry.
    
    Returns a sorted list of capability IDs (directories in artifacts/).
    """
    artifacts_dir = _get_artifacts_dir()
    if not artifacts_dir.exists():
        return []
    
    capabilities = []
    for item in artifacts_dir.iterdir():
        if item.is_dir() and item.name not in ("schema.json",):
            capabilities.append(item.name)
    
    return sorted(capabilities)


def _find_versions(capability_id: str) -> list[str]:
    """Find all version files for a capability, sorted by version.
    
    Returns a list of version strings like ["1.0", "2.0", "2.1"] in ascending order.
    """
    artifacts_dir = _get_artifacts_dir()
    cap_dir = artifacts_dir / capability_id
    
    if not cap_dir.exists():
        raise FileNotFoundError(f"Capability {capability_id!r} not found")
    
    versions = []
    for item in cap_dir.glob("v*.json"):
        match = re.match(r"v(\d+\.\d+)\.json$", item.name)
        if match:
            versions.append(match.group(1))
    
    # Sort by (major, minor)
    versions.sort(key=_parse_version)
    return versions


def _load_capability_json(capability_id: str, version: str) -> Capability:
    """Load and validate a capability artifact from disk."""
    artifacts_dir = _get_artifacts_dir()
    path = artifacts_dir / capability_id / f"v{version}.json"
    
    if not path.exists():
        raise FileNotFoundError(f"Artifact {path} not found")
    
    data = json.loads(path.read_text())
    return Capability.model_validate(data)


def load(capability_id: str, version: Optional[str] = None) -> Capability:
    """Load a capability by ID and optional version specifier.
    
    Args:
        capability_id: The capability identifier (e.g., "lookup_member_balance")
        version: Version specifier:
            - None: Load the highest-versioned approved capability
            - "2": Load the highest 2.x version that is approved
            - "2.1": Load the exact version 2.1 (regardless of approval)
    
    Returns:
        A validated Capability.
    
    Raises:
        FileNotFoundError: If the capability or version doesn't exist
        ValueError: If version format is invalid or no matching version is approved
    """
    versions = _find_versions(capability_id)
    
    if not versions:
        raise FileNotFoundError(
            f"No artifact versions found for capability {capability_id!r}"
        )
    
    if version is None:
        # None → highest approved
        for v in reversed(versions):
            cap = _load_capability_json(capability_id, v)
            if cap.approval == "approved":
                return cap
        raise ValueError(
            f"No approved versions found for capability {capability_id!r}"
        )
    
    if "." in version:
        # Exact version "2.1"
        return _load_capability_json(capability_id, version)
    
    # Major-only version "2" → highest 2.x that is approved
    major = int(version)
    matching = [v for v in versions if _parse_version(v)[0] == major]
    
    if not matching:
        raise ValueError(
            f"No versions matching {version!r}.x for capability {capability_id!r}"
        )
    
    for v in reversed(matching):
        cap = _load_capability_json(capability_id, v)
        if cap.approval == "approved":
            return cap
    
    raise ValueError(
        f"No approved {version!r}.x versions for capability {capability_id!r}"
    )


def _apply_path_patch(obj: dict, path_expr: str, value) -> None:
    """Apply a single patch to a dict using a dotted/bracketed path expression.

    Modifies obj in-place. Path examples:
    - "target.base_url"
    - "steps[1].target.strategies[0].spec.name"
    """
    # Tokenize by dots and brackets
    # "target.base_url" → ["target", "base_url"]
    # "steps[1].target.strategies[0].spec.name" → ["steps", "[1]", "target", "strategies", "[0]", "spec", "name"]
    tokens = []
    current_token = ""
    for char in path_expr:
        if char == ".":
            if current_token:
                tokens.append(current_token)
                current_token = ""
        elif char == "[":
            if current_token:
                tokens.append(current_token)
                current_token = "["
            else:
                current_token = "["
        elif char == "]":
            current_token += "]"
            tokens.append(current_token)
            current_token = ""
        else:
            current_token += char
    if current_token:
        tokens.append(current_token)

    # Navigate to the parent of the final key/index
    current = obj
    for token in tokens[:-1]:
        if token.startswith("["):
            # Array index like "[0]"
            idx = int(token[1:-1])
            current = current[idx]
        else:
            # Object key
            if token not in current:
                raise KeyError(f"Path {path_expr!r}: key {token!r} not found")
            current = current[token]

    # Set the final value
    last_token = tokens[-1]
    if last_token.startswith("["):
        idx = int(last_token[1:-1])
        current[idx] = value
    else:
        current[last_token] = value


def compose(base: Capability, overlay: TenantOverlay) -> Capability:
    """Apply a tenant overlay to a base capability and re-validate.
    
    Args:
        base: The base capability to patch
        overlay: The tenant overlay with patches
    
    Returns:
        A new, re-validated Capability with patches applied
    
    Raises:
        ValueError: If the overlay pins a base_version whose major has moved on,
                   or if re-validation fails
    """
    # Check: overlay's pinned base_version major <= current base major
    overlay_major, overlay_minor = _parse_version(overlay.base_version)
    base_major, base_minor = base.version_major, base.version_minor
    
    if overlay_major < base_major or (
        overlay_major == base_major and overlay_minor > base_minor
    ):
        raise ValueError(
            f"Overlay pins base_version {overlay.base_version} but base is "
            f"{base_major}.{base_minor}: base has moved on, this overlay is stale"
        )
    
    # Apply patches
    cap_dict = base.model_dump()
    for patch in overlay.patches:
        _apply_path_patch(cap_dict, patch.path, patch.value)
    
    # Re-validate through Pydantic
    return Capability.model_validate(cap_dict)
