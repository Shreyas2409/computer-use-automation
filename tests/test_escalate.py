"""Unit tests for the control-lease state machine and escalation broker.

No Playwright, no Flask, no live target app — these exercise the pure
in-process contract that ``cua.escalate`` promises to callers.
"""

from __future__ import annotations

import asyncio

import pytest

from cua.escalate import (
    EscalationBroker,
    HolderMismatch,
    IllegalTransition,
    InterventionRequest,
    Lease,
    LeaseError,
    new_intervention_id,
    new_resume_token,
)


def _sample_request(**overrides) -> InterventionRequest:
    base = dict(
        intervention_id=new_intervention_id(),
        capability_id="test_cap",
        version="1.0",
        tenant_id="meridian",
        step_index=3,
        reason="unit test",
        goal="drive it",
        screenshot_path="",
        state_summary={"url": "about:blank"},
        resume_token=new_resume_token(),
        evidence_dir="",
    )
    base.update(overrides)
    return InterventionRequest(**base)


# -------------------------------------------------------------- state machine


def test_default_lease_starts_in_automation():
    lease = Lease()
    assert lease.state == "AUTOMATION"
    assert lease.holder == "automation"
    assert lease.transitions == []


def test_full_cycle_transitions_are_legal_and_logged():
    lease = Lease()
    lease.transition("PENDING_HANDOFF", actor="automation", reason="open")
    lease.transition("HUMAN", actor="operator", reason="took control")
    lease.transition("RESUMING", actor="operator", reason="handback")
    lease.transition("AUTOMATION", actor="automation", reason="resumed")
    kinds = [t.to_state for t in lease.transitions]
    assert kinds == ["PENDING_HANDOFF", "HUMAN", "RESUMING", "AUTOMATION"]
    assert lease.holder == "automation"
    # Each row carries actor + reason + timestamp.
    for row in lease.transitions:
        assert row.actor
        assert row.reason
        assert row.at


def test_illegal_transitions_are_rejected():
    lease = Lease()
    with pytest.raises(IllegalTransition):
        lease.transition("HUMAN", actor="operator", reason="skip pending")
    lease.transition("PENDING_HANDOFF", actor="automation", reason="open")
    with pytest.raises(IllegalTransition):
        lease.transition("RESUMING", actor="operator", reason="skip human")


def test_pending_handoff_can_cancel_back_to_automation():
    lease = Lease()
    lease.transition("PENDING_HANDOFF", actor="automation", reason="open")
    lease.transition("AUTOMATION", actor="automation", reason="cancelled")
    assert lease.state == "AUTOMATION"


def test_assert_holder_rejects_stale_actor():
    lease = Lease()
    lease.transition("PENDING_HANDOFF", actor="automation", reason="open")
    lease.transition("HUMAN", actor="operator", reason="took control")
    with pytest.raises(HolderMismatch):
        lease.assert_holder("automation")
    lease.assert_holder("human")  # correct holder — no raise


# ------------------------------------------------------------------ broker


def _run(coro):
    return asyncio.run(coro)


def test_broker_open_take_handback_resume_full_cycle():
    broker = EscalationBroker()
    request = _sample_request()

    async def scenario():
        await broker.open_intervention(request)
        assert broker.pending() is request
        assert broker.lease.state == "PENDING_HANDOFF"

        broker.take_control(actor="alice")
        assert broker.lease.state == "HUMAN"

        broker.hand_back(
            actor="alice",
            notes="looked ok",
            resume_token=request.resume_token,
        )
        notes = await broker.wait_for_handback(timeout=1.0)
        assert notes == "looked ok"
        broker.complete_resume(actor="automation", reason="resume")
        assert broker.lease.state == "AUTOMATION"
        assert broker.pending() is None

    _run(scenario())


def test_broker_rejects_bad_resume_token():
    broker = EscalationBroker()
    request = _sample_request()

    async def scenario():
        await broker.open_intervention(request)
        broker.take_control(actor="alice")
        with pytest.raises(LeaseError):
            broker.hand_back(actor="alice", resume_token="wrong-token")

    _run(scenario())


def test_broker_wait_for_handback_times_out():
    broker = EscalationBroker()
    request = _sample_request()

    async def scenario():
        await broker.open_intervention(request)
        # No operator ever takes control — wait must raise LeaseError, not
        # hang forever.
        with pytest.raises(LeaseError):
            await broker.wait_for_handback(timeout=0.05)

    _run(scenario())


def test_cancel_pending_wakes_waiter_with_lease_error():
    broker = EscalationBroker()
    request = _sample_request()

    async def scenario():
        await broker.open_intervention(request)
        waiter = asyncio.create_task(broker.wait_for_handback(timeout=1.0))
        await asyncio.sleep(0)  # let the waiter park
        broker.cancel_pending(actor="automation", reason="user aborted")
        with pytest.raises(LeaseError):
            await waiter
        assert broker.lease.state == "AUTOMATION"
        assert broker.pending() is None

    _run(scenario())


def test_take_control_without_pending_raises():
    broker = EscalationBroker()
    with pytest.raises(LeaseError):
        broker.take_control(actor="alice")


def test_invoke_action_requires_human_holder_and_handler():
    broker = EscalationBroker()
    request = _sample_request()

    async def scenario():
        # No pending intervention yet — automation still holds the lease.
        with pytest.raises(HolderMismatch):
            await broker.invoke_action({"kind": "noop"})

        await broker.open_intervention(request)
        broker.take_control(actor="alice")
        # HUMAN holds the lease but no handler wired yet.
        with pytest.raises(LeaseError):
            await broker.invoke_action({"kind": "noop"})

        seen: list[dict] = []

        async def handler(cmd):
            seen.append(cmd)
            return {"ok": True, "echo": cmd}

        broker.register_action_handler(handler)
        entry = await broker.invoke_action({"kind": "click", "id": "x"})
        assert seen == [{"kind": "click", "id": "x"}]
        assert entry["result"] == {"ok": True, "echo": {"kind": "click", "id": "x"}}
        assert broker.action_log() == [entry]

    _run(scenario())


def test_new_ids_are_unique_and_prefixed():
    ids = {new_intervention_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("iv_") for i in ids)
    tokens = {new_resume_token() for _ in range(50)}
    assert len(tokens) == 50
