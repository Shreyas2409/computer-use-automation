"""Control-lease state machine + operator escalation broker.

Design decision 5 from the Build Brief: control transfer is a *lease*, not a
callback. Exactly one holder at any instant; every transition is timestamped
and attributable; no browser or model imports live here so the module can be
consumed from anywhere in the codebase (including tests) without a Playwright
or Anthropic dependency.

Legal transitions form a closed cycle::

    AUTOMATION → PENDING_HANDOFF → HUMAN → RESUMING → AUTOMATION

with one escape hatch — ``PENDING_HANDOFF → AUTOMATION`` — used only when an
intervention is cancelled before an operator takes control. Any other jump
raises ``IllegalTransition``. The replay engine calls ``lease.assert_holder``
before every action so a stale AUTOMATION step can never slip through while a
human is driving.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal


LeaseState = Literal["AUTOMATION", "PENDING_HANDOFF", "HUMAN", "RESUMING"]


# Adjacency map for the state machine. Kept module-level so tests can inspect
# the exact set of legal transitions without reaching into a class.
_LEGAL_TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    "AUTOMATION": frozenset({"PENDING_HANDOFF"}),
    "PENDING_HANDOFF": frozenset({"HUMAN", "AUTOMATION"}),
    "HUMAN": frozenset({"RESUMING"}),
    "RESUMING": frozenset({"AUTOMATION"}),
}


_HOLDER_FOR_STATE: dict[LeaseState, str] = {
    "AUTOMATION": "automation",
    "PENDING_HANDOFF": "pending",
    "HUMAN": "human",
    "RESUMING": "resuming",
}


class LeaseError(RuntimeError):
    """Base class for lease-machine misuse."""


class IllegalTransition(LeaseError):
    """Raised when a transition is not in ``_LEGAL_TRANSITIONS``."""


class HolderMismatch(LeaseError):
    """Raised when an actor tries to act while another party holds the lease."""


@dataclass
class LeaseTransition:
    """One immutable row of the control-transfer audit trail."""

    at: str
    from_state: LeaseState
    to_state: LeaseState
    actor: str
    reason: str


@dataclass
class Lease:
    """Single-holder control lease with a transition log.

    ``holder`` mirrors the current ``state`` (automation | pending | human |
    resuming) — kept as a separate field so evidence dumps and operator UI
    can display it without decoding state semantics on the client side.
    """

    state: LeaseState = "AUTOMATION"
    holder: str = "automation"
    transitions: list[LeaseTransition] = field(default_factory=list)

    def transition(
        self, to: LeaseState, *, actor: str, reason: str
    ) -> LeaseTransition:
        """Move to ``to`` if legal from the current state; log unconditionally.

        The recorded row includes UTC timestamp, actor, and reason so the
        evidence log tells the whole story without additional context.
        """

        allowed = _LEGAL_TRANSITIONS.get(self.state, frozenset())
        if to not in allowed:
            raise IllegalTransition(
                f"illegal lease transition {self.state} → {to}; "
                f"allowed from {self.state}: {sorted(allowed)}"
            )
        row = LeaseTransition(
            at=datetime.now(timezone.utc).isoformat(),
            from_state=self.state,
            to_state=to,
            actor=actor,
            reason=reason,
        )
        self.state = to
        self.holder = _HOLDER_FOR_STATE[to]
        self.transitions.append(row)
        return row

    def assert_holder(self, expected: str) -> None:
        """Raise ``HolderMismatch`` unless ``expected`` currently owns the lease."""

        if self.holder != expected:
            raise HolderMismatch(
                f"lease held by {self.holder!r}, not {expected!r}; "
                f"state={self.state}"
            )

    def transitions_as_dicts(self) -> list[dict[str, str]]:
        """JSON-safe snapshot of the transition log, for evidence/state views."""

        return [asdict(t) for t in self.transitions]


# ---- Intervention request ------------------------------------------------


@dataclass
class InterventionRequest:
    """Everything a human needs to know to take over safely.

    ``state_summary`` is a redacted, JSON-safe dict — the engine builds this
    from the observable page/context so no secret or PII value ever reaches
    the operator UI even if the sender is careless. The ``resume_token`` is
    what the operator posts back to prove the handback matches this request.
    """

    intervention_id: str
    capability_id: str
    version: str
    tenant_id: str
    step_index: int
    reason: str
    goal: str
    screenshot_path: str
    state_summary: dict[str, Any]
    resume_token: str
    evidence_dir: str


def new_intervention_id() -> str:
    return f"iv_{secrets.token_hex(8)}"


def new_resume_token() -> str:
    return secrets.token_urlsafe(16)


# ---- Broker --------------------------------------------------------------


HumanActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class EscalationBroker:
    """Single-intervention, in-process coordination surface.

    The replay engine calls ``open_intervention`` to declare "I am done, a
    human needs to drive". The operator surface reads ``pending()``, invokes
    ``take_control`` / ``hand_back`` in response to user clicks, and posts
    scripted "do this in the live browser" commands via ``invoke_action``
    (which fans out to a handler the demo/live wiring registers — kept as a
    seam so this module stays browser-free).

    One broker == one active intervention. Callers wanting concurrent
    escalations should instantiate multiple brokers; the operator surface
    already assumes exactly one pending item at a time.
    """

    def __init__(self, lease: Lease | None = None) -> None:
        self.lease = lease or Lease()
        self._pending: InterventionRequest | None = None
        self._handback_notes: str = ""
        self._handback_event = asyncio.Event()
        self._cancelled = False
        self._action_handler: HumanActionHandler | None = None
        self._action_log: list[dict[str, Any]] = []

    # -- introspection ----------------------------------------------------

    def pending(self) -> InterventionRequest | None:
        return self._pending

    def action_log(self) -> list[dict[str, Any]]:
        return list(self._action_log)

    # -- automation side --------------------------------------------------

    async def open_intervention(self, request: InterventionRequest) -> None:
        """Automation-side: transition AUTOMATION → PENDING_HANDOFF, park."""

        self.lease.assert_holder("automation")
        self.lease.transition(
            "PENDING_HANDOFF",
            actor="automation",
            reason=f"open: {request.reason}",
        )
        self._pending = request
        self._handback_notes = ""
        self._handback_event.clear()
        self._cancelled = False

    async def wait_for_handback(self, timeout: float = 300.0) -> str:
        """Automation-side: block until operator hands back or timeout hits.

        Polls a bounded interval rather than awaiting the event's own
        wake-notification. hand_back() (and cancel_pending()) may run on a
        different thread than whichever event loop is running this
        coroutine — e.g. the operator console's Flask handler thread versus
        a per-request asyncio.run() loop in a caller that opens a fresh
        loop per call. A cross-thread Event.set() updates the flag
        correctly (a plain attribute write, safe under the GIL) but is not
        guaranteed to wake a *different* thread's event loop out of its own
        selector wait; polling only depends on reading that flag, never on
        being notified, so it works the same regardless of which thread
        signalled it or how many event loops have come and gone since this
        wait started.
        """
        poll_interval = 0.05
        elapsed = 0.0
        while True:
            if self._cancelled:
                raise LeaseError("intervention cancelled before handback")
            if self._handback_event.is_set():
                return self._handback_notes
            if elapsed >= timeout:
                raise LeaseError(
                    f"handback timed out after {timeout:.1f}s in state "
                    f"{self.lease.state}"
                )
            await asyncio.sleep(min(poll_interval, timeout - elapsed))
            elapsed += poll_interval

    def complete_resume(
        self, *, actor: str = "automation", reason: str = "resumed"
    ) -> None:
        """Automation-side: RESUMING → AUTOMATION, clear the pending slot."""

        self.lease.transition("AUTOMATION", actor=actor, reason=reason)
        self._pending = None

    def cancel_pending(
        self, *, actor: str = "automation", reason: str = "cancelled"
    ) -> None:
        """Abort a pending handoff before an operator has taken control."""

        if self.lease.state != "PENDING_HANDOFF":
            raise LeaseError(
                f"cannot cancel pending from state {self.lease.state}"
            )
        self.lease.transition("AUTOMATION", actor=actor, reason=reason)
        self._pending = None
        self._cancelled = True
        self._handback_event.set()

    # -- operator side ----------------------------------------------------

    def take_control(self, *, actor: str = "operator") -> LeaseTransition:
        if self._pending is None:
            raise LeaseError("no pending intervention to take control of")
        return self.lease.transition(
            "HUMAN", actor=actor, reason="operator took control"
        )

    def hand_back(
        self,
        *,
        actor: str = "operator",
        notes: str = "",
        resume_token: str | None = None,
    ) -> LeaseTransition:
        if self._pending is None:
            raise LeaseError("no pending intervention to hand back")
        if resume_token is not None and resume_token != self._pending.resume_token:
            raise LeaseError("resume_token does not match pending intervention")
        row = self.lease.transition(
            "RESUMING", actor=actor, reason=notes or "operator handback"
        )
        self._handback_notes = notes
        self._handback_event.set()
        return row

    # -- scripted human action seam --------------------------------------

    def register_action_handler(self, handler: HumanActionHandler) -> None:
        """Wire an in-process callable used to execute ``/intervene`` requests.

        The demo/live wiring passes something that drives the actual live
        page (Playwright over CDP — same session as automation). Left as a
        seam so this module and the operator app never import Playwright.
        """

        self._action_handler = handler

    async def invoke_action(self, command: dict[str, Any]) -> dict[str, Any]:
        """Operator-side: run a scripted human action against the live page.

        Requires HUMAN state; raises otherwise. All results are appended to
        ``action_log`` for evidence.
        """

        self.lease.assert_holder("human")
        if self._action_handler is None:
            raise LeaseError(
                "no human action handler registered; wire one before /intervene"
            )
        result = await self._action_handler(command)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "result": result,
        }
        self._action_log.append(entry)
        return entry


__all__ = [
    "EscalationBroker",
    "HolderMismatch",
    "HumanActionHandler",
    "IllegalTransition",
    "InterventionRequest",
    "Lease",
    "LeaseError",
    "LeaseState",
    "LeaseTransition",
    "new_intervention_id",
    "new_resume_token",
]
