"""Operator surface package.

Minimal Flask app + HTML console the human uses to take control of a paused
replay. Kept in its own package because it MUST NOT import Playwright or the
Anthropic SDK — the live-browser side is wired through
``EscalationBroker.register_action_handler`` at the call site.
"""

from cua.operator.server import DEFAULT_HOST, DEFAULT_PORT, create_app

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "create_app"]
