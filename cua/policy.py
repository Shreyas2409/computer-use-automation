"""Policy gate: action-layer enforcement of allowed domains, routes, and destructive rules.

PolicyGate.check(action, context) → Allow | RequireConfirmation | Block

Called on every action in both discovery and replay. No browser imports.
Redaction is applied to every disk write via redact_sensitive_value().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from pathlib import Path
import yaml

# Load policy configuration
_POLICY_FILE = Path(__file__).parent.parent / "policy.yaml"
if _POLICY_FILE.exists():
    with open(_POLICY_FILE) as f:
        _POLICY_CONFIG = yaml.safe_load(f)
else:
    _POLICY_CONFIG = {}

ActionVerdict = Literal["allow", "require_confirmation", "block"]


@dataclass
class Action:
    """Context for a policy check."""
    verb: str  # e.g., "click", "type", "navigate", "delete"
    url: str  # current page URL
    domain: str | None = None  # extracted from URL
    route: str | None = None  # path extracted from URL
    value: str | None = None  # sensitive data that may need redaction


def redact_sensitive_value(value: str | None) -> str:
    """Redact sensitive data according to policy.yaml rules.
    
    Args:
        value: The potential sensitive value
        
    Returns:
        Either the original value (if not sensitive) or a redacted version
        like <redacted:type:length>.
    """
    if value is None:
        return None
    
    rules = _POLICY_CONFIG.get("redaction_rules", {})
    for pattern, replacement in rules.items():
        if re.search(pattern, str(value)):
            return replacement
    
    return value


class PolicyGate:
    """Action-layer policy enforcement.
    
    Checks every action against allowed domains, routes, and risky rules.
    """
    
    @staticmethod
    def check(action: Action, context: dict | None = None) -> ActionVerdict:
        """Check if an action is allowed, requires confirmation, or is blocked.

        Args:
            action: The action to check
            context: Optional context (e.g., step history, current state)

        Returns:
            One of: "allow", "require_confirmation", "block"
        """
        # Extract domain and route from URL
        action.domain = _extract_domain(action.url)
        action.route = _extract_route(action.url)

        # Check domain allowlist
        if not _is_domain_allowed(action.domain):
            return "block"

        # Check route allowlist (if route exists)
        if action.route is not None:
            if not _is_route_allowed(action.route):
                return "block"

        # Check for destructive actions (blocks even if matches confirmation pattern)
        if _is_destructive(action.verb):
            return "block"

        # Check for actions requiring confirmation
        if _requires_confirmation(action.verb):
            return "require_confirmation"

        return "allow"


def _extract_domain(url: str) -> str | None:
    """Extract domain from URL."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc or None
    except Exception:
        return None


def _extract_route(url: str) -> str | None:
    """Extract path (route) from URL."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.path or None
    except Exception:
        return None


def _is_domain_allowed(domain: str | None) -> bool:
    """Check if domain is in allowlist."""
    if not domain:
        return False

    # Strip port from domain (localhost:8080 -> localhost)
    domain_only = domain.split(":")[0] if ":" in domain else domain

    allowed = _POLICY_CONFIG.get("allowed_domains", [])
    for pattern in allowed:
        if pattern == domain_only or _matches_pattern(domain_only, pattern):
            return True
    return False


def _is_route_allowed(route: str) -> bool:
    """Check if route matches allowlist patterns."""
    patterns = _POLICY_CONFIG.get("allowed_route_patterns", [])
    for pattern in patterns:
        if re.match(pattern, route):
            return True
    return False


def _is_destructive(verb: str) -> bool:
    """Check if verb is a destructive action."""
    destructive = _POLICY_CONFIG.get("destructive_verbs", [])
    return verb.lower() in [v.lower() for v in destructive]


def _requires_confirmation(verb: str) -> bool:
    """Check if action requires explicit confirmation."""
    patterns = _POLICY_CONFIG.get("require_confirmation_patterns", [])
    return any(p.lower() in verb.lower() for p in patterns)


def _matches_pattern(domain: str, pattern: str) -> bool:
    """Wildcard domain matching: example.com, *.example.com."""
    if pattern.startswith("*."):
        # *.example.com matches foo.example.com, bar.example.com, etc.
        suffix = pattern[2:]
        return domain.endswith("." + suffix) or domain == suffix.lstrip("*.")
    return domain == pattern
