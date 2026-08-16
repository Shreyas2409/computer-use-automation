"""Replay engine unit tests using an in-memory FakeSurface.

No Playwright, no live target app. These cover the pieces of the engine that
must hold regardless of the surface: param binding, deterministic execution,
outcome classification, bounded recovery, checkpoint gating, and the shape of
the ``ReplayResult`` contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cua.evidence import EvidenceWriter
from cua.replay import (
    ParamBindingError,
    ReplayConfig,
    ReplayEngine,
    bind_params,
    replay_with_surface,
)
from cua.result import (
    ReplayBusinessOutcome,
    ReplayEscalated,
    ReplayHardFailure,
    ReplaySuccess,
    result_to_dict,
)
from cua.schema import (
    Capability,
    ElementPresentDetector,
    ExtractSpec,
    LocatorLadder,
    LocatorStrategy,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    RecoveryRule,
    Step,
    TargetSpec,
    TextMatchDetector,
    UrlPatternDetector,
    ValueRef,
)


# --------------------------------------------------------------------- fakes


def _ladder(name: str) -> LocatorLadder:
    return LocatorLadder(
        strategies=[
            LocatorStrategy(
                kind="role_name",
                spec={"role": "button", "name": name},
                durability=90,
            )
        ],
        recorded_match="role_name",
    )


def _label_ladder(label: str) -> LocatorLadder:
    return LocatorLadder(
        strategies=[
            LocatorStrategy(
                kind="label", spec={"label": label}, durability=90
            )
        ],
        recorded_match="label",
    )


@dataclass
class _Script:
    """A queued behavior for one FakeSurface action.

    ``raise_exc``: raise this exception once when the action is invoked
    (subsequent invocations succeed with the default rung).
    ``rung``: report this rung as the matched one.
    """

    raise_exc: Exception | None = None
    rung: str = "role_name"


class FakeSurface:
    """In-memory surface for testing the engine's control flow."""

    def __init__(
        self,
        *,
        text: str = "",
        url: str = "http://localhost:8080/members",
        extract_values: dict[str, str] | None = None,
    ) -> None:
        self._text = text
        self._url = url
        self._extract_values = extract_values or {}
        self.calls: list[tuple[str, Any]] = []
        self.click_scripts: list[_Script] = []
        self.fill_scripts: list[_Script] = []
        self.dismiss_calls = 0
        self.dismiss_available = False

    def _pop(self, q: list[_Script]) -> _Script:
        return q.pop(0) if q else _Script()

    def current_url(self) -> str:
        return self._url

    async def navigate(self, url: str, *, timeout_ms: int = 6000) -> None:
        self._url = url
        self.calls.append(("navigate", url))

    async def click(self, ladder: LocatorLadder, *, timeout_ms: int = 6000):
        s = self._pop(self.click_scripts)
        self.calls.append(("click", ladder.recorded_match))
        if s.raise_exc is not None:
            raise s.raise_exc
        return s.rung

    async def fill(
        self, ladder: LocatorLadder, value: str, *, timeout_ms: int = 6000
    ):
        s = self._pop(self.fill_scripts)
        self.calls.append(("fill", value))
        if s.raise_exc is not None:
            raise s.raise_exc
        return s.rung

    async def select(
        self, ladder: LocatorLadder, value: str, *, timeout_ms: int = 6000
    ):
        self.calls.append(("select", value))
        return "role_name"

    async def wait_for(self, ladder: LocatorLadder, *, timeout_ms: int = 6000):
        self.calls.append(("wait_for", ladder.recorded_match))
        return ladder.recorded_match

    async def read(self, ladder: LocatorLadder, *, timeout_ms: int = 6000):
        return ""

    async def page_text(self) -> str:
        return self._text

    async def check_detector(self, detector) -> bool:
        return False

    async def extract(self, spec: ExtractSpec) -> str | None:
        return self._extract_values.get(id(spec)) or next(
            iter(self._extract_values.values()), None
        )

    async def dismiss_top_dialog(self) -> bool:
        self.dismiss_calls += 1
        if self.dismiss_available:
            self._text = ""
            self.dismiss_available = False
            return True
        return False

    async def screenshot(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"")


def _target() -> TargetSpec:
    return TargetSpec(
        app_id="demo",
        base_url="http://localhost:8080",
        tenant_id="meridian",
        target_version="1.0",
        surface_kind="legacy_web",
    )


def _cap(
    steps: list[Step],
    outcomes: list[OutcomeSpec] | None = None,
    outputs: list[OutputSpec] | None = None,
    inputs: list[ParamSpec] | None = None,
) -> Capability:
    return Capability(
        schema_version="1.0",
        capability_id="test_cap",
        version_major=1,
        version_minor=0,
        title="t",
        description="d",
        target=_target(),
        inputs=inputs or [],
        outputs=outputs or [],
        steps=steps,
        outcomes=outcomes or [],
        approval="approved",
        recorded_at="2026-01-01T00:00:00Z",
        recorded_by="human_edit",
    )


def _evidence(tmp_path: Path) -> EvidenceWriter:
    return EvidenceWriter(tmp_path)


# --------------------------------------------------------------------- tests


def test_bind_params_missing_required():
    inputs = [
        ParamSpec(
            name="member_id",
            type="string",
            required=True,
            description="d",
            example="10001",
            sensitivity="internal",
        )
    ]
    with pytest.raises(ParamBindingError):
        bind_params(inputs, {})


def test_bind_params_ignores_extras_and_keeps_declared():
    inputs = [
        ParamSpec(
            name="member_id",
            type="string",
            required=True,
            description="d",
            example="10001",
            sensitivity="internal",
        )
    ]
    bound = bind_params(inputs, {"member_id": "42", "junk": "x"})
    assert bound == {"member_id": "42"}


def test_success_result_contract(tmp_path):
    steps = [
        Step(index=0, action="navigate", value="http://localhost:8080/login"),
        Step(index=1, action="click", target=_ladder("Go")),
    ]
    outputs = [
        OutputSpec(
            name="value",
            type="string",
            description="d",
            extract=ExtractSpec(locator=_label_ladder("Value")),
        )
    ]
    cap = _cap(steps, outputs=outputs)
    surface = FakeSurface(extract_values={0: "hello"})
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplaySuccess)
    assert result.exit_code == 0
    assert result.outputs == {"value": "hello"}
    assert result.capability_id == "test_cap"
    assert result.version == "1.0"
    # Steps recorded with their matched rungs.
    assert [s.action for s in result.steps] == ["navigate", "click"]
    assert result_to_dict(result)["status"] == "ok"


def test_business_outcome_classification(tmp_path):
    outcomes = [
        OutcomeSpec(
            name="no_such_member",
            detect=TextMatchDetector(contains="No member found"),
            classification="business_outcome",
            terminal=True,
            message="not found",
        )
    ]
    steps = [
        Step(index=0, action="click", target=_ladder("Search")),
    ]
    cap = _cap(steps, outcomes=outcomes)
    surface = FakeSurface(text="No member found matching that ID")
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayBusinessOutcome)
    assert result.outcome_name == "no_such_member"
    assert result.exit_code == 0


def test_hard_failure_classification(tmp_path):
    outcomes = [
        OutcomeSpec(
            name="server_error",
            detect=TextMatchDetector(contains="Internal Server Error"),
            classification="hard_failure",
            terminal=True,
            message="500",
        )
    ]
    steps = [Step(index=0, action="click", target=_ladder("Search"))]
    cap = _cap(steps, outcomes=outcomes)
    surface = FakeSurface(text="Internal Server Error")
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayHardFailure)
    assert result.outcome_name == "server_error"
    assert result.exit_code == 2


def test_recovery_bounded_and_records_events(tmp_path):
    steps = [
        Step(
            index=0,
            action="click",
            target=_ladder("Search"),
            on_error=[
                RecoveryRule(
                    on=TextMatchDetector(contains="please confirm"),
                    do="dismiss_dialog",
                    max_attempts=1,
                )
            ],
        )
    ]
    cap = _cap(steps)
    surface = FakeSurface(text="Please confirm you have read the terms")
    surface.dismiss_available = True
    # Fail once (modal blocking) then succeed after dismiss.
    surface.click_scripts = [
        _Script(raise_exc=RuntimeError("modal blocked click")),
        _Script(rung="role_name"),
    ]
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplaySuccess)
    assert len(result.recovery_events) == 1
    ev = result.recovery_events[0]
    assert ev.rule == "dismiss_dialog"
    assert ev.attempt == 1
    assert ev.success is True
    assert surface.dismiss_calls == 1


def test_recovery_bounded_exhausted_becomes_hard_failure(tmp_path):
    steps = [
        Step(
            index=0,
            action="click",
            target=_ladder("Search"),
            on_error=[
                RecoveryRule(
                    on=TextMatchDetector(contains="please confirm"),
                    do="dismiss_dialog",
                    max_attempts=1,
                )
            ],
        )
    ]
    cap = _cap(steps)
    surface = FakeSurface(text="Please confirm you have read the terms")
    surface.dismiss_available = False  # dismiss_dialog returns False
    surface.click_scripts = [
        _Script(raise_exc=RuntimeError("modal blocked click")),
    ]
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayHardFailure)
    assert result.step_index == 0
    assert "modal blocked click" in result.observed


def test_checkpoint_failure_becomes_hard_failure(tmp_path):
    from cua.schema import Checkpoint

    steps = [
        Step(
            index=0,
            action="click",
            target=_ladder("Go"),
            checkpoint=Checkpoint(
                description="url matches /done",
                detect=UrlPatternDetector(pattern="/done$"),
            ),
        )
    ]
    cap = _cap(steps)
    surface = FakeSurface(url="http://localhost:8080/members?other=1")
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayHardFailure)
    assert result.error == "checkpoint_failed"


def test_valueref_resolution_and_missing_param(tmp_path):
    inputs = [
        ParamSpec(
            name="member_id",
            type="string",
            required=True,
            description="d",
            example="10001",
            sensitivity="internal",
        )
    ]
    steps = [
        Step(
            index=0,
            action="type",
            target=_label_ladder("Member ID"),
            value=ValueRef(param="member_id"),
        )
    ]
    cap = _cap(steps, inputs=inputs)
    surface = FakeSurface()
    result = asyncio.run(
        replay_with_surface(
            cap, surface, {"member_id": "10001"}, _evidence(tmp_path)
        )
    )
    assert isinstance(result, ReplaySuccess)
    assert ("fill", "10001") in surface.calls

    # Now try again without supplying member_id → bind fails at construction.
    with pytest.raises(ParamBindingError):
        ReplayEngine(cap, surface, {}, _evidence(tmp_path))


def test_policy_blocked_produces_hard_failure(tmp_path):
    steps = [
        Step(
            index=0,
            action="navigate",
            value="http://evil.example.com/",
        )
    ]
    cap = _cap(steps)
    surface = FakeSurface(url="")
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayHardFailure)
    assert "policy" in result.observed.lower()


def test_extract_transform_currency_to_cents(tmp_path):
    steps = [
        Step(index=0, action="navigate", value="http://localhost:8080/members"),
    ]
    outputs = [
        OutputSpec(
            name="balance",
            type="currency",
            description="d",
            extract=ExtractSpec(
                locator=_label_ladder("Balance"),
                regex=r"\$([0-9,]+\.[0-9]{2})",
                transform="currency_to_cents",
            ),
        )
    ]
    cap = _cap(steps, outputs=outputs)
    surface = FakeSurface(extract_values={0: "$1,234.56"})
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplaySuccess)
    assert result.outputs == {"balance": 123456}


def test_result_to_dict_includes_drift_flag():
    from cua.result import RungReport

    r = RungReport(step_index=0, recorded_match="role_name", replay_match="css")
    assert r.drifted is True
    r2 = RungReport(step_index=0, recorded_match="label", replay_match="label")
    assert r2.drifted is False


# ------------------------------------------------------------ escalation seam


def test_escalation_resumes_on_handback(tmp_path):
    """Broker attached: unrecovered click failure escalates, human hands back,
    engine retries the same step and completes normally."""

    from cua.escalate import EscalationBroker

    steps = [Step(index=0, action="click", target=_ladder("Go"))]
    cap = _cap(steps)
    surface = FakeSurface()
    # First click raises (no on_error rule → recovery exhausted immediately),
    # second click succeeds after the operator "hands back".
    surface.click_scripts = [
        _Script(raise_exc=RuntimeError("modal blocked click")),
        _Script(rung="role_name"),
    ]

    broker = EscalationBroker()

    async def scenario():
        # Simulate the operator: as soon as PENDING_HANDOFF appears, take
        # control and hand back so the engine unparks.
        async def operator():
            for _ in range(50):
                if broker.pending() is not None:
                    break
                await asyncio.sleep(0.01)
            assert broker.pending() is not None
            broker.take_control(actor="alice")
            broker.hand_back(
                actor="alice",
                notes="fixed the modal",
                resume_token=broker.pending().resume_token,
            )

        op_task = asyncio.create_task(operator())
        cfg = ReplayConfig(escalation_broker=broker, escalation_timeout_s=2.0)
        result = await replay_with_surface(
            cap, surface, {}, _evidence(tmp_path), cfg
        )
        await op_task
        return result

    result = asyncio.run(scenario())
    assert isinstance(result, ReplaySuccess)
    assert len(result.interventions) == 1
    iv = result.interventions[0]
    assert iv["outcome"] == "resumed"
    assert iv["kind"] == "action_failed"


def test_escalation_returns_escalated_on_timeout(tmp_path):
    """Broker attached but no operator: wait times out → ReplayEscalated."""

    from cua.escalate import EscalationBroker

    steps = [Step(index=0, action="click", target=_ladder("Go"))]
    cap = _cap(steps)
    surface = FakeSurface()
    surface.click_scripts = [_Script(raise_exc=RuntimeError("boom"))]

    broker = EscalationBroker()
    cfg = ReplayConfig(escalation_broker=broker, escalation_timeout_s=0.05)
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path), cfg)
    )
    assert isinstance(result, ReplayEscalated)
    assert result.exit_code == 3
    assert len(result.interventions) == 1
    assert result.interventions[0]["outcome"] == "failed"
    # Lease should still be parked in PENDING_HANDOFF (nobody resumed it).
    assert broker.lease.state == "PENDING_HANDOFF"


def test_no_broker_preserves_prior_hard_failure_path(tmp_path):
    """Without a broker, exhausted recovery keeps the pre-escalation semantics."""

    steps = [Step(index=0, action="click", target=_ladder("Go"))]
    cap = _cap(steps)
    surface = FakeSurface()
    surface.click_scripts = [_Script(raise_exc=RuntimeError("boom"))]
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayHardFailure)
    assert result.interventions == []



# ------------------------------------------------------ structured evidence


def test_evidence_json_written_on_success_with_step_records(tmp_path):
    """BLOCKER 3: successful replay writes evidence.json with structured
    step-level log entries. Redaction is exercised by supplying a pii-typed
    param whose value must not appear in the log."""

    import json

    inputs = [
        ParamSpec(
            name="member_id",
            type="string",
            required=True,
            description="d",
            example="10001",
            sensitivity="pii",
        )
    ]
    steps = [
        Step(
            index=0,
            action="type",
            target=_label_ladder("Member ID"),
            value=ValueRef(param="member_id"),
        ),
        Step(index=1, action="click", target=_ladder("Search")),
    ]
    cap = _cap(steps, inputs=inputs)
    surface = FakeSurface()
    ev = _evidence(tmp_path)
    result = asyncio.run(
        replay_with_surface(cap, surface, {"member_id": "SECRET-987654"}, ev)
    )
    assert isinstance(result, ReplaySuccess)

    evidence_file = tmp_path / "evidence.json"
    assert evidence_file.exists(), (
        "BLOCKER 3: replay must write evidence.json at session end"
    )
    entries = json.loads(evidence_file.read_text())
    assert isinstance(entries, list) and len(entries) >= 3
    phases = {e.get("phase") for e in entries}
    assert "replay" in phases
    # There must be at least one step-level record.
    step_records = [e for e in entries if e.get("step_index") is not None]
    assert step_records, (
        "BLOCKER 3: evidence must contain step-level records, not just the "
        "session-start/end bookends"
    )
    # A trailing replay_end record must summarise the outcome.
    ends = [e for e in entries if e.get("message") == "replay_end"]
    assert ends, "BLOCKER 3: expected a terminal replay_end record"
    assert ends[-1]["metadata"]["status"] == "ReplaySuccess"

    # Redaction: the raw pii value must not appear anywhere in the file.
    raw = evidence_file.read_text()
    assert "SECRET-987654" not in raw, (
        "BLOCKER 3: pii-sensitivity param value leaked into evidence.json"
    )


def test_evidence_json_written_on_hard_failure(tmp_path):
    """BLOCKER 3: failure path must still land a structured evidence file."""

    import json

    steps = [Step(index=0, action="click", target=_ladder("Go"))]
    cap = _cap(steps)
    surface = FakeSurface()
    surface.click_scripts = [_Script(raise_exc=RuntimeError("boom"))]
    result = asyncio.run(
        replay_with_surface(cap, surface, {}, _evidence(tmp_path))
    )
    assert isinstance(result, ReplayHardFailure)

    evidence_file = tmp_path / "evidence.json"
    assert evidence_file.exists()
    entries = json.loads(evidence_file.read_text())
    ends = [e for e in entries if e.get("message") == "replay_end"]
    assert ends and ends[-1]["metadata"]["status"] == "ReplayHardFailure"
    # The pre-terminal replay_hard_failure record must be present too.
    assert any(e.get("message") == "replay_hard_failure" for e in entries)
