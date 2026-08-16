"""Replay result contract: typed discriminated union + per-step telemetry.

Every replay returns a ``ReplayResult``. A failure must be debuggable from the
result alone: the timeline includes which rung matched at each step, whether
any recovery event fired, and where the evidence is on disk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Union


ReplayStatus = Literal["ok", "business_outcome", "failed", "escalated"]


@dataclass
class RungReport:
    """Per-step ladder telemetry (recorded rung vs. rung that resolved)."""

    step_index: int
    recorded_match: str
    replay_match: str

    @property
    def drifted(self) -> bool:
        return self.recorded_match != self.replay_match


@dataclass
class RecoveryEvent:
    """One bounded-recovery attempt performed inside a step."""

    step_index: int
    rule: str  # RecoveryAction (dismiss_dialog, retry_with_backoff, reauth, escalate)
    trigger: str  # short human-readable trigger description
    attempt: int
    success: bool


@dataclass
class StepEvent:
    """One executed step in the replay timeline."""

    step_index: int
    action: str
    status: Literal[
        "ok", "recovered", "action_failed", "checkpoint_failed", "outcome"
    ]
    matched_rung: str | None = None
    recorded_rung: str | None = None
    checkpoint_status: Literal["ok", "failed", "skipped"] = "skipped"
    duration_ms: int = 0
    detail: str | None = None


@dataclass
class _ReplayBase:
    """Fields common to every ReplayResult variant."""

    capability_id: str
    version: str  # "major.minor"
    tenant_id: str
    duration_ms: int
    steps: list[StepEvent] = field(default_factory=list)
    rung_drift: list[RungReport] = field(default_factory=list)
    recovery_events: list[RecoveryEvent] = field(default_factory=list)
    evidence_dir: str = ""


@dataclass
class ReplaySuccess(_ReplayBase):
    """Deterministic success: every step passed, outputs extracted cleanly."""

    status: Literal["ok"] = "ok"
    outputs: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


@dataclass
class ReplayBusinessOutcome(_ReplayBase):
    """A declared business outcome fired (e.g. member_not_found).

    Exit code 0: this is a *successful* replay from the engine's perspective;
    the outcome is meaningful information for the calling agent, not a crash.
    """

    status: Literal["business_outcome"] = "business_outcome"
    outcome_name: str = ""
    message: str = ""
    step_index: int = -1
    exit_code: int = 0


@dataclass
class ReplayHardFailure(_ReplayBase):
    """A declared hard-failure outcome fired, or an undeclared crash occurred.

    Everything a debugger needs is right here: the failing step, the action
    attempted, what we expected to see, what we actually observed, and the
    evidence directory with screenshots and logs.
    """

    status: Literal["failed"] = "failed"
    step_index: int = -1
    action: str = ""
    expected: str = ""
    observed: str = ""
    outcome_name: str | None = None
    error: str = ""
    exit_code: int = 2


@dataclass
class ReplayEscalated(_ReplayBase):
    """Recovery exhausted; control has been (or would be) handed to a human."""

    status: Literal["escalated"] = "escalated"
    intervention_id: str = ""
    reason: str = ""
    resumable: bool = True
    exit_code: int = 3


ReplayResult = Union[
    ReplaySuccess, ReplayBusinessOutcome, ReplayHardFailure, ReplayEscalated
]


def result_to_dict(result: ReplayResult) -> dict[str, Any]:
    """Serialize a ReplayResult to a JSON-safe dict for logs and CLI output."""

    d = asdict(result)
    # RungReport.drifted is a property, not a field — add it back explicitly.
    for rd in d.get("rung_drift", []):
        rd["drifted"] = rd["recorded_match"] != rd["replay_match"]
    return d
