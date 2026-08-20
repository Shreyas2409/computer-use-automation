"""Deterministic replay engine.

Runs a versioned Capability artifact against a Surface, with no model in the
decision loop. Bounded recovery, redacted evidence, typed results. This module
imports NEITHER the model SDK nor any browser driver; the browser side lives
in ``cua.surface.web`` and any language-model side is confined to
``cua.discover``.

Deterministic guarantees:
- No unbounded sleeps; every wait is anchored to a checkpoint or timeout.
- Fixed ladder order (as declared in the artifact).
- Recovery is bounded per rule and every event is logged.
- Business outcomes short-circuit cleanly; hard failures preserve the exact
  observation that tripped them.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cua.escalate import (
    EscalationBroker,
    InterventionRequest,
    LeaseError,
    new_intervention_id,
    new_resume_token,
)
from cua.evidence import EvidenceWriter
from cua.policy import Action, PolicyGate, redact_sensitive_value
from cua.result import (
    RecoveryEvent,
    ReplayBusinessOutcome,
    ReplayEscalated,
    ReplayHardFailure,
    ReplayResult,
    ReplaySuccess,
    RungReport,
    StepEvent,
)
from cua.schema import (
    Capability,
    Detector,
    ElementPresentDetector,
    ExtractSpec,
    LocatorKind,
    LocatorLadder,
    OutcomeSpec,
    ParamSpec,
    RecoveryAction,
    RecoveryRule,
    Step,
    TextMatchDetector,
    UrlPatternDetector,
    ValueRef,
)


DEFAULT_STEP_TIMEOUT_MS = 6000
DEFAULT_RETRY_BACKOFF_MS = 500


class ReplaySurface(Protocol):
    """Contract the replay engine expects from any Surface implementation.

    Kept local to this module so the engine's dependency surface is explicit;
    ``cua.surface.web.WebSurface`` structurally satisfies it (and so does the
    ``FakeSurface`` used in tests).
    """

    def current_url(self) -> str: ...

    async def navigate(self, url: str, *, timeout_ms: int = ...) -> None: ...

    async def click(
        self, ladder: LocatorLadder, *, timeout_ms: int = ...
    ) -> LocatorKind: ...

    async def fill(
        self, ladder: LocatorLadder, value: str, *, timeout_ms: int = ...
    ) -> LocatorKind: ...

    async def select(
        self, ladder: LocatorLadder, value: str, *, timeout_ms: int = ...
    ) -> LocatorKind: ...

    async def wait_for(
        self, ladder: LocatorLadder, *, timeout_ms: int = ...
    ) -> LocatorKind: ...

    async def read(
        self, ladder: LocatorLadder, *, timeout_ms: int = ...
    ) -> str: ...

    async def page_text(self) -> str: ...

    async def check_detector(self, detector: Detector) -> bool: ...

    async def extract(self, spec: ExtractSpec) -> str | None: ...

    async def dismiss_top_dialog(self) -> bool: ...

    async def screenshot(self, path: str) -> None: ...


# ---- Parameter binding ---------------------------------------------------


class ParamBindingError(ValueError):
    """Raised when a caller-supplied param is missing or mistyped."""


def bind_params(
    inputs: list[ParamSpec], supplied: dict[str, Any]
) -> dict[str, Any]:
    """Validate and coerce caller params against a capability's inputs.

    Returns a new dict with only declared param names. Extra params are
    ignored deliberately: the artifact is the contract; callers passing
    unknown keys shouldn't accidentally influence anything.
    """

    bound: dict[str, Any] = {}
    for spec in inputs:
        if spec.name in supplied:
            bound[spec.name] = supplied[spec.name]
        elif spec.required:
            raise ParamBindingError(
                f"missing required param {spec.name!r} "
                f"(declared with sensitivity={spec.sensitivity})"
            )
    return bound


def resolve_value(step_value: Any, bound: dict[str, Any]) -> Any:
    """Resolve a step's declared value against bound params.

    ValueRef → param value; literals pass through unchanged.
    """

    if isinstance(step_value, ValueRef):
        if step_value.param not in bound:
            raise ParamBindingError(
                f"step references param {step_value.param!r} that was not "
                f"supplied at replay time"
            )
        return bound[step_value.param]
    return step_value


# ---- Detector evaluation -------------------------------------------------


async def evaluate_detector(
    detector: Detector, surface: ReplaySurface
) -> bool:
    """Ask the surface whether a detector matches the current observation.

    text_match and url_pattern are evaluated here (cheap, no locator work);
    element_present delegates to the surface's ladder resolution.
    """

    if isinstance(detector, TextMatchDetector):
        haystack = await surface.page_text()
        needle = detector.contains
        if not detector.case_sensitive:
            return needle.lower() in (haystack or "").lower()
        return needle in (haystack or "")
    if isinstance(detector, UrlPatternDetector):
        url = surface.current_url()
        return re.search(detector.pattern, url or "") is not None
    if isinstance(detector, ElementPresentDetector):
        return await surface.check_detector(detector)
    raise TypeError(f"unknown detector type: {type(detector).__name__}")


async def match_outcome(
    outcomes: list[OutcomeSpec], surface: ReplaySurface
) -> OutcomeSpec | None:
    """Return the first outcome whose detector fires in the current state.

    Order matters: business outcomes should be declared before overlapping
    hard failures in the artifact for correct precedence.
    """

    for oc in outcomes:
        if await evaluate_detector(oc.detect, surface):
            return oc
    return None


# ---- Engine --------------------------------------------------------------


@dataclass
class ReplayConfig:
    """Runtime knobs for the replay engine.

    ``inject_mode`` is a test/demo affordance for the target app's injection
    routes (``?inject=dialog`` etc.); when set, the engine appends the query
    once, after the first step whose URL lands on an in-app route.
    """

    step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS
    retry_backoff_ms: int = DEFAULT_RETRY_BACKOFF_MS
    inject_mode: str | None = None
    inject_after_step: int = 3
    escalation_broker: EscalationBroker | None = None
    escalation_timeout_s: float = 300.0
    escalation_goal: str = ""


@dataclass
class _RunState:
    steps: list[StepEvent] = field(default_factory=list)
    rung_drift: list[RungReport] = field(default_factory=list)
    recoveries: list[RecoveryEvent] = field(default_factory=list)
    recovery_attempts: dict[tuple[int, str], int] = field(default_factory=dict)
    injected: bool = False
    escalated_steps: set[int] = field(default_factory=set)
    interventions: list[dict[str, Any]] = field(default_factory=list)
    # inject_mode='escalate' plants exactly one fault inside the action
    # handler for the configured step. Set to True after it fires so the
    # post-handback retry sees the fault healed.
    escalate_fault_fired: bool = False


class ReplayEngine:
    """Executes a capability deterministically against a Surface."""

    def __init__(
        self,
        capability: Capability,
        surface: ReplaySurface,
        params: dict[str, Any],
        evidence: EvidenceWriter,
        config: ReplayConfig | None = None,
    ) -> None:
        self.cap = capability
        self.surface = surface
        self.params_by_name = {p.name: p for p in capability.inputs}
        self.bound = bind_params(capability.inputs, params)
        self.evidence = evidence
        self.config = config or ReplayConfig()
        self.state = _RunState()

    # -- helpers ----------------------------------------------------------

    def _log(
        self,
        *,
        message: str,
        step_index: int | None = None,
        action: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.evidence.log(
            phase="replay",
            step_index=step_index,
            action=action,
            message=message,
            error=error,
            metadata=metadata,
            params=self.params_by_name,
        )

    def _describe_detector(self, det: Detector) -> str:
        if isinstance(det, TextMatchDetector):
            return f"text~{det.contains!r}"
        if isinstance(det, UrlPatternDetector):
            return f"url~{det.pattern!r}"
        if isinstance(det, ElementPresentDetector):
            kinds = [s.kind for s in det.locator.strategies]
            return f"element_present[{','.join(kinds)}]"
        return type(det).__name__

    async def _snapshot(self, name: str) -> str:
        path = str(Path(self.evidence.evidence_dir) / name)
        try:
            await self.surface.screenshot(path)
        except Exception:  # noqa: BLE001
            return ""
        return path

    async def _maybe_inject(self, after_step_index: int) -> None:
        """Append ``?inject=MODE`` once, after the configured step.

        ``inject_mode='escalate'`` is a purely in-process fault (fires inside
        ``_execute_action`` for the next step); it never touches the URL, so
        we skip the target-app query-string route for it.
        """

        if not self.config.inject_mode or self.state.injected:
            return
        if after_step_index != self.config.inject_after_step:
            return
        if self.config.inject_mode == "escalate":
            # Escalate is a pure in-process fault: it self-triggers inside
            # _execute_action when step.index == inject_after_step, so this
            # URL-injection path is not what arms it. Marking ``injected``
            # keeps the log symmetric with the other inject modes.
            self.state.injected = True
            return
        url = self.surface.current_url()
        sep = "&" if "?" in url else "?"
        injected_url = f"{url}{sep}inject={self.config.inject_mode}"
        self._log(
            message="inject_query_applied",
            metadata={"mode": self.config.inject_mode, "url": injected_url},
        )
        await self.surface.navigate(
            injected_url, timeout_ms=self.config.step_timeout_ms
        )
        self.state.injected = True

    def _maybe_escalate_fault_inject(self, step: Step) -> None:
        """Raise once inside the action handler when inject_mode='escalate'.

        Triggers the escalation seam WITHIN the step's action attempt so the
        full lease cycle exercises: recovery-exhausted → escalate → operator
        take_control → hand_back → RESUMING → retry action. Fires exactly once
        per run; after handback ``escalate_fault_fired`` is set so the retry
        proceeds normally.
        """

        if self.config.inject_mode != "escalate":
            return
        if step.index != self.config.inject_after_step:
            return
        if self.state.escalate_fault_fired:
            return
        self.state.escalate_fault_fired = True
        raise RuntimeError(
            f"injected escalate: forcing operator handoff at step "
            f"{step.index} ({step.action})"
        )

    # -- policy gate ------------------------------------------------------

    @staticmethod
    def _click_label(ladder: LocatorLadder) -> str | None:
        """Best-effort accessible label for a click, for policy verb matching.

        Mirrors discover.py, which uses the clicked element's name/text as
        the policy verb so require_confirmation_patterns (Submit, Confirm,
        Transfer, ...) can match. Without this, replay always sees the
        generic action kind ("click") and those patterns can never fire.
        """
        for strategy in ladder.strategies:
            label = strategy.spec.get("name") or strategy.spec.get("text")
            if label:
                return str(label)
        return None

    def _policy_verdict(
        self, step: Step, resolved_value: Any
    ) -> tuple[str, Action]:
        verb = step.action
        if step.action == "click" and step.target is not None:
            verb = self._click_label(step.target) or step.action
        # For a navigate the *destination* is what policy must vet; for every
        # other action, we vet the current page (``about:blank`` before the
        # first navigate is fine because the domain allowlist happens to
        # reject it — but only after the first real step has landed).
        if step.action == "navigate":
            url = str(resolved_value or "")
        else:
            url = self.surface.current_url() or ""
        action = Action(
            verb=verb,
            url=url,
            value=str(resolved_value) if resolved_value is not None else None,
        )
        verdict = PolicyGate.check(action)
        return verdict, action

    # -- outcome check ----------------------------------------------------

    async def _check_outcomes(
        self, step_index: int
    ) -> OutcomeSpec | None:
        oc = await match_outcome(self.cap.outcomes, self.surface)
        if oc is not None:
            self._log(
                message="outcome_detected",
                step_index=step_index,
                metadata={
                    "name": oc.name,
                    "classification": oc.classification,
                    "terminal": oc.terminal,
                },
            )
        return oc

    # -- recovery ---------------------------------------------------------

    async def _try_recovery(
        self, step: Step, error_detail: str
    ) -> tuple[bool, str | None]:
        """Iterate on_error rules; run the first whose detector fires.

        Returns ``(recovered, rule_name)`` — ``recovered`` is True if the
        recovery action ran successfully (the caller then retries the step).
        Bounded by each rule's ``max_attempts`` counted per step+rule.
        """

        for rule in step.on_error:
            key = (step.index, rule.do)
            attempts_so_far = self.state.recovery_attempts.get(key, 0)
            if attempts_so_far >= rule.max_attempts:
                continue
            fired = await evaluate_detector(rule.on, self.surface)
            if not fired:
                continue
            attempt = attempts_so_far + 1
            self.state.recovery_attempts[key] = attempt
            success = await self._run_recovery_action(rule.do)
            trigger = self._describe_detector(rule.on)
            self.state.recoveries.append(
                RecoveryEvent(
                    step_index=step.index,
                    rule=rule.do,
                    trigger=trigger,
                    attempt=attempt,
                    success=success,
                )
            )
            self._log(
                message="recovery_triggered",
                step_index=step.index,
                metadata={
                    "rule": rule.do,
                    "trigger": trigger,
                    "attempt": attempt,
                    "success": success,
                    "step_error": error_detail[:200],
                },
            )
            return success, rule.do
        return False, None

    async def _run_recovery_action(self, action: RecoveryAction) -> bool:
        if action == "dismiss_dialog":
            return await self.surface.dismiss_top_dialog()
        if action == "retry_with_backoff":
            await asyncio.sleep(self.config.retry_backoff_ms / 1000.0)
            return True
        if action == "reauth":
            # Re-run the login prelude (steps 0..3) by convention.
            return await self._reauth_prelude()
        if action == "escalate":
            return False
        return False

    # -- escalation seam --------------------------------------------------

    async def _open_intervention_and_wait(
        self, *, step_index: int, reason: str, kind: str
    ) -> tuple[bool, str | None]:
        """Fire an intervention through the broker, park, wait for handback.

        Returns ``(resumed, intervention_id)``. ``resumed`` is True on a clean
        handback (caller retries the failing thing); False on timeout, cancel,
        broker misconfiguration, or when no broker is registered. Bounded to
        one escalation per step to prevent loops between the engine and the
        operator.
        """

        broker = self.config.escalation_broker
        if broker is None:
            return False, None
        if step_index in self.state.escalated_steps:
            return False, None
        self.state.escalated_steps.add(step_index)

        iv_id = new_intervention_id()
        token = new_resume_token()
        try:
            url = self.surface.current_url() or ""
        except Exception:  # noqa: BLE001
            url = ""
        try:
            page_text = await self.surface.page_text()
        except Exception:  # noqa: BLE001
            page_text = ""
        summary = {
            "url": url,
            "page_text_length": len(page_text or ""),
            "steps_completed": len(self.state.steps),
            "kind": kind,
        }
        snap_name = f"intervention_{iv_id}.png"
        snap_path = await self._snapshot(snap_name)
        request = InterventionRequest(
            intervention_id=iv_id,
            capability_id=self.cap.capability_id,
            version=f"{self.cap.version_major}.{self.cap.version_minor}",
            tenant_id=self.cap.target.tenant_id,
            step_index=step_index,
            reason=reason,
            goal=self.config.escalation_goal or self.cap.title,
            screenshot_path=snap_path,
            state_summary=summary,
            resume_token=token,
            evidence_dir=str(self.evidence.evidence_dir),
        )
        try:
            await broker.open_intervention(request)
        except LeaseError as exc:
            self._log(
                message="intervention_open_failed",
                step_index=step_index,
                error=str(exc),
                metadata={"kind": kind},
            )
            return False, iv_id
        self._log(
            message="intervention_opened",
            step_index=step_index,
            metadata={
                "intervention_id": iv_id,
                "kind": kind,
                "reason": reason,
                "screenshot": snap_path,
            },
        )
        try:
            notes = await broker.wait_for_handback(
                timeout=self.config.escalation_timeout_s
            )
            broker.complete_resume(
                actor="automation",
                reason=f"resume:{iv_id}",
            )
        except LeaseError as exc:
            self._log(
                message="intervention_wait_failed",
                step_index=step_index,
                error=str(exc),
                metadata={"intervention_id": iv_id, "kind": kind},
            )
            self.state.interventions.append(
                {
                    "intervention_id": iv_id,
                    "step_index": step_index,
                    "kind": kind,
                    "reason": reason,
                    "outcome": "failed",
                    "error": str(exc),
                    "transitions": broker.lease.transitions_as_dicts(),
                }
            )
            return False, iv_id
        self._log(
            message="intervention_resumed",
            step_index=step_index,
            metadata={"intervention_id": iv_id, "notes": notes[:200]},
        )
        self.state.interventions.append(
            {
                "intervention_id": iv_id,
                "step_index": step_index,
                "kind": kind,
                "reason": reason,
                "outcome": "resumed",
                "notes": notes,
                "transitions": broker.lease.transitions_as_dicts(),
                "action_log": broker.action_log(),
            }
        )
        return True, iv_id

    def _escalated_result(
        self,
        *,
        step_index: int,
        started: float,
        reason: str,
        intervention_id: str | None,
    ) -> ReplayEscalated:
        duration = int((time.monotonic() - started) * 1000)
        return ReplayEscalated(
            capability_id=self.cap.capability_id,
            version=f"{self.cap.version_major}.{self.cap.version_minor}",
            tenant_id=self.cap.target.tenant_id,
            duration_ms=duration,
            steps=list(self.state.steps),
            rung_drift=list(self.state.rung_drift),
            recovery_events=list(self.state.recoveries),
            evidence_dir=str(self.evidence.evidence_dir),
            interventions=list(self.state.interventions),
            intervention_id=intervention_id or f"confirm-{step_index}",
            reason=reason,
            resumable=True,
        )

    async def _reauth_prelude(self) -> bool:
        """Best-effort re-auth: replay the first four steps (nav+creds+submit).

        Deterministic reauth requires knowing which steps constitute login.
        Convention here: capability's steps 0..3 (navigate → type → type →
        click). If the shape doesn't match, we bail cleanly and let the
        higher layer escalate.
        """

        if len(self.cap.steps) < 4:
            return False
        prelude = self.cap.steps[:4]
        expected = ("navigate", "type", "type", "click")
        if tuple(s.action for s in prelude) != expected:
            return False
        for s in prelude:
            try:
                await self._execute_action(s, allow_recovery=False)
            except Exception:  # noqa: BLE001
                return False
        return True

    # -- action dispatch --------------------------------------------------

    async def _execute_action(
        self, step: Step, *, allow_recovery: bool = True
    ) -> LocatorKind | None:
        """Run one step's action against the surface. Returns matched rung."""

        self._maybe_escalate_fault_inject(step)
        resolved = resolve_value(step.value, self.bound)
        if step.action == "navigate":
            await self.surface.navigate(
                str(resolved), timeout_ms=self.config.step_timeout_ms
            )
            return None
        assert step.target is not None  # schema-enforced
        ladder = step.target
        if step.action == "click":
            return await self.surface.click(
                ladder, timeout_ms=self.config.step_timeout_ms
            )
        if step.action == "type":
            return await self.surface.fill(
                ladder, str(resolved), timeout_ms=self.config.step_timeout_ms
            )
        if step.action == "select":
            return await self.surface.select(
                ladder, str(resolved), timeout_ms=self.config.step_timeout_ms
            )
        if step.action == "wait_for":
            return await self.surface.wait_for(
                ladder, timeout_ms=self.config.step_timeout_ms
            )
        if step.action == "read":
            await self.surface.read(
                ladder, timeout_ms=self.config.step_timeout_ms
            )
            return None
        if step.action == "assert":
            return await self.surface.wait_for(
                ladder, timeout_ms=self.config.step_timeout_ms
            )
        raise ValueError(f"unknown action: {step.action!r}")

    # -- main loop --------------------------------------------------------

    async def run(self) -> ReplayResult:
        started = time.monotonic()
        version = f"{self.cap.version_major}.{self.cap.version_minor}"
        self._log(
            message="replay_start",
            metadata={
                "capability_id": self.cap.capability_id,
                "version": version,
                "tenant_id": self.cap.target.tenant_id,
                "base_url": self.cap.target.base_url,
                "params": sorted(self.bound.keys()),
                "inject_mode": self.config.inject_mode,
            },
        )

        for step in self.cap.steps:
            outcome_result = await self._run_one_step(step, started)
            if outcome_result is not None:
                return outcome_result
            await self._maybe_inject(step.index)

        # Extract outputs after all steps pass.
        outputs, extract_failure = await self._extract_outputs()
        if extract_failure is not None:
            return await self._hard_failure(
                step_index=len(self.cap.steps) - 1,
                action="extract",
                expected="output extraction to succeed",
                observed=extract_failure,
                started=started,
                error=extract_failure,
            )

        duration = int((time.monotonic() - started) * 1000)
        self._log(
            message="replay_success",
            metadata={"outputs": sorted(outputs.keys()), "duration_ms": duration},
        )
        return ReplaySuccess(
            capability_id=self.cap.capability_id,
            version=version,
            tenant_id=self.cap.target.tenant_id,
            duration_ms=duration,
            steps=list(self.state.steps),
            rung_drift=list(self.state.rung_drift),
            recovery_events=list(self.state.recoveries),
            evidence_dir=str(self.evidence.evidence_dir),
            interventions=list(self.state.interventions),
            outputs=outputs,
        )

    async def _run_one_step(
        self, step: Step, started: float
    ) -> ReplayResult | None:
        """Execute one step end-to-end. Return a terminal result or None."""

        step_started = time.monotonic()
        resolved = resolve_value(step.value, self.bound)
        verdict, action_ctx = self._policy_verdict(step, resolved)
        if verdict == "block":
            return await self._hard_failure(
                step_index=step.index,
                action=step.action,
                expected=f"policy allows verb={step.action!r}",
                observed=f"policy blocked url={action_ctx.url!r}",
                started=started,
                error="policy_blocked",
            )
        if verdict == "require_confirmation":
            self._log(
                message="policy_require_confirmation",
                step_index=step.index,
                metadata={"verb": step.action, "url": action_ctx.url},
            )
            reason = f"policy requires confirmation for {step.action!r}"
            resumed, iv_id = await self._open_intervention_and_wait(
                step_index=step.index, reason=reason, kind="policy_confirm"
            )
            if not resumed:
                return self._escalated_result(
                    step_index=step.index,
                    started=started,
                    reason=reason,
                    intervention_id=iv_id,
                )
            # Operator approved: fall through and execute the action.

        matched: LocatorKind | None = None
        recovered_this_step = False
        error_detail: str | None = None
        while True:
            try:
                matched = await self._execute_action(step)
                error_detail = None
                break
            except Exception as exc:  # noqa: BLE001
                error_detail = f"{type(exc).__name__}: {exc}"[:400]
                await self._snapshot(f"step_{step.index:02d}_action_error.png")
                # Check if a business/hard outcome fired mid-error (e.g. 500).
                oc = await self._check_outcomes(step.index)
                if oc is not None and oc.terminal:
                    return await self._terminal_outcome(
                        step.index, oc, started
                    )
                recovered, rule = await self._try_recovery(step, error_detail)
                if not recovered:
                    resumed, iv_id = await self._open_intervention_and_wait(
                        step_index=step.index,
                        reason=f"action {step.action!r} failed: {error_detail}",
                        kind="action_failed",
                    )
                    if resumed:
                        recovered_this_step = True
                        continue
                    if iv_id is not None:
                        return self._escalated_result(
                            step_index=step.index,
                            started=started,
                            reason=f"action {step.action!r} exhausted recovery",
                            intervention_id=iv_id,
                        )
                    return await self._hard_failure(
                        step_index=step.index,
                        action=step.action,
                        expected=f"{step.action} to succeed",
                        observed=error_detail,
                        started=started,
                        error=error_detail,
                    )
                recovered_this_step = True

        # Record rung drift (only for stepped actions with a ladder).
        if step.target is not None and matched is not None:
            self.state.rung_drift.append(
                RungReport(
                    step_index=step.index,
                    recorded_match=step.target.recorded_match,
                    replay_match=matched,
                )
            )

        # Post-action: outcomes take precedence over checkpoint.
        oc = await self._check_outcomes(step.index)
        if oc is not None and oc.terminal:
            return await self._terminal_outcome(step.index, oc, started)

        checkpoint_status: str = "skipped"
        if step.checkpoint is not None:
            fired = await evaluate_detector(
                step.checkpoint.detect, self.surface
            )
            if fired:
                checkpoint_status = "ok"
            else:
                # Try recovery before failing checkpoint.
                recovered, _ = await self._try_recovery(
                    step, f"checkpoint failed: {step.checkpoint.description}"
                )
                if recovered:
                    recovered_this_step = True
                    fired = await evaluate_detector(
                        step.checkpoint.detect, self.surface
                    )
                if fired:
                    checkpoint_status = "ok"
                else:
                    resumed, iv_id = await self._open_intervention_and_wait(
                        step_index=step.index,
                        reason=(
                            f"checkpoint {step.checkpoint.description!r} did "
                            "not fire; recovery exhausted"
                        ),
                        kind="checkpoint_failed",
                    )
                    if resumed:
                        # Brief: engine re-verifies the checkpoint after
                        # handback — cannot trust where the human left off.
                        fired = await evaluate_detector(
                            step.checkpoint.detect, self.surface
                        )
                        if fired:
                            recovered_this_step = True
                            checkpoint_status = "ok"
                    if checkpoint_status != "ok":
                        if iv_id is not None:
                            return self._escalated_result(
                                step_index=step.index,
                                started=started,
                                reason=(
                                    "checkpoint failed after handback re-verify"
                                ),
                                intervention_id=iv_id,
                            )
                        return await self._hard_failure(
                            step_index=step.index,
                            action=step.action,
                            expected=step.checkpoint.description,
                            observed=self._describe_detector(
                                step.checkpoint.detect
                            )
                            + " did not fire",
                            started=started,
                            error="checkpoint_failed",
                        )

        duration_ms = int((time.monotonic() - step_started) * 1000)
        self.state.steps.append(
            StepEvent(
                step_index=step.index,
                action=step.action,
                status="recovered" if recovered_this_step else "ok",
                matched_rung=matched,
                recorded_rung=(
                    step.target.recorded_match if step.target else None
                ),
                checkpoint_status=checkpoint_status,  # type: ignore[arg-type]
                duration_ms=duration_ms,
            )
        )
        self._log(
            message="step_ok",
            step_index=step.index,
            action=step.action,
            metadata={
                "matched_rung": matched,
                "recorded_rung": (
                    step.target.recorded_match if step.target else None
                ),
                "checkpoint": checkpoint_status,
                "duration_ms": duration_ms,
                "recovered": recovered_this_step,
            },
        )
        return None

    async def _terminal_outcome(
        self, step_index: int, oc: OutcomeSpec, started: float
    ) -> ReplayResult:
        duration = int((time.monotonic() - started) * 1000)
        version = f"{self.cap.version_major}.{self.cap.version_minor}"
        self.state.steps.append(
            StepEvent(
                step_index=step_index,
                action="outcome",
                status="outcome",
                detail=oc.name,
            )
        )
        await self._snapshot(f"step_{step_index:02d}_outcome_{oc.name}.png")
        if oc.classification == "business_outcome":
            return ReplayBusinessOutcome(
                capability_id=self.cap.capability_id,
                version=version,
                tenant_id=self.cap.target.tenant_id,
                duration_ms=duration,
                steps=list(self.state.steps),
                rung_drift=list(self.state.rung_drift),
                recovery_events=list(self.state.recoveries),
                evidence_dir=str(self.evidence.evidence_dir),
                interventions=list(self.state.interventions),
                outcome_name=oc.name,
                message=oc.message,
                step_index=step_index,
            )
        # hard_failure or recoverable-but-terminal → HardFailure result.
        return ReplayHardFailure(
            capability_id=self.cap.capability_id,
            version=version,
            tenant_id=self.cap.target.tenant_id,
            duration_ms=duration,
            steps=list(self.state.steps),
            rung_drift=list(self.state.rung_drift),
            recovery_events=list(self.state.recoveries),
            evidence_dir=str(self.evidence.evidence_dir),
            interventions=list(self.state.interventions),
            step_index=step_index,
            action="outcome",
            expected="no terminal hard-failure outcome",
            observed=self._describe_detector(oc.detect) + " matched",
            outcome_name=oc.name,
            error=oc.message,
        )

    async def _hard_failure(
        self,
        *,
        step_index: int,
        action: str,
        expected: str,
        observed: str,
        started: float,
        error: str,
    ) -> ReplayHardFailure:
        version = f"{self.cap.version_major}.{self.cap.version_minor}"
        # Check whether any declared hard_failure outcome explains this state.
        outcome_name: str | None = None
        for oc in self.cap.outcomes:
            if (
                oc.classification == "hard_failure"
                and await evaluate_detector(oc.detect, self.surface)
            ):
                outcome_name = oc.name
                break
        snap = await self._snapshot(f"step_{step_index:02d}_failed.png")
        redacted_observed = redact_sensitive_value(observed) or observed
        self.state.steps.append(
            StepEvent(
                step_index=step_index,
                action=action,
                status="action_failed"
                if error != "checkpoint_failed"
                else "checkpoint_failed",
                detail=redacted_observed[:200],
            )
        )
        duration = int((time.monotonic() - started) * 1000)
        self._log(
            message="replay_hard_failure",
            step_index=step_index,
            action=action,
            error=error,
            metadata={
                "expected": expected,
                "observed": redacted_observed,
                "outcome_name": outcome_name,
                "screenshot": snap,
            },
        )
        return ReplayHardFailure(
            capability_id=self.cap.capability_id,
            version=version,
            tenant_id=self.cap.target.tenant_id,
            duration_ms=duration,
            steps=list(self.state.steps),
            rung_drift=list(self.state.rung_drift),
            recovery_events=list(self.state.recoveries),
            evidence_dir=str(self.evidence.evidence_dir),
            interventions=list(self.state.interventions),
            step_index=step_index,
            action=action,
            expected=expected,
            observed=redacted_observed,
            outcome_name=outcome_name,
            error=error,
        )

    # -- output extraction ------------------------------------------------

    async def _extract_outputs(
        self,
    ) -> tuple[dict[str, Any], str | None]:
        outputs: dict[str, Any] = {}
        for spec in self.cap.outputs:
            try:
                raw = await self.surface.extract(spec.extract)
            except Exception as exc:  # noqa: BLE001
                return outputs, f"extract {spec.name!r}: {exc}"[:200]
            if raw is None:
                return outputs, f"extract {spec.name!r}: no value found"
            value = _apply_transform(raw, spec.extract)
            outputs[spec.name] = value
        return outputs, None


# ---- Transform helpers ---------------------------------------------------


def _apply_transform(raw: str, spec: ExtractSpec) -> Any:
    """Apply optional regex + typed transform to an extracted string."""

    text = raw
    if spec.regex:
        m = re.search(spec.regex, text)
        if m:
            text = m.group(1) if m.groups() else m.group(0)
    if spec.transform is None:
        return text.strip()
    if spec.transform == "strip":
        return text.strip()
    if spec.transform == "to_lower":
        return text.strip().lower()
    if spec.transform == "to_int":
        return int(re.sub(r"[^\d-]", "", text))
    if spec.transform == "currency_to_cents":
        cleaned = re.sub(r"[^\d.\-]", "", text)
        if not cleaned:
            raise ValueError(f"currency_to_cents: no digits in {raw!r}")
        return int(round(float(cleaned) * 100))
    return text


# ---- Top-level runners --------------------------------------------------


def _open_evidence(evidence_root: Path, cap: Capability) -> EvidenceWriter:
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{cap.capability_id}_{cap.target.tenant_id}"
    dir_ = evidence_root / f"replay_{tag}_{session_id}"
    return EvidenceWriter(dir_)


async def replay_with_surface(
    capability: Capability,
    surface: ReplaySurface,
    params: dict[str, Any],
    evidence: EvidenceWriter,
    config: ReplayConfig | None = None,
) -> ReplayResult:
    """Run a replay against an already-instantiated Surface + EvidenceWriter.

    Writes a structured, redacted ``evidence.json`` at session end regardless
    of outcome (success, business outcome, hard failure, escalated). The engine
    only *appends* to ``evidence.logs`` during the run; the disk write happens
    exactly once here so PNGs and structured logs land in the same dir.
    """

    engine = ReplayEngine(capability, surface, params, evidence, config)
    try:
        result = await engine.run()
    except BaseException:
        # Even a crash-out (KeyboardInterrupt, cancelled task, unexpected
        # exception the engine didn't map) must leave structured evidence on
        # disk before it propagates.
        _finalize_evidence(evidence, status="crashed")
        raise
    _finalize_evidence(
        evidence, status=type(result).__name__, result_summary=_result_summary(result)
    )
    return result


def _result_summary(result: ReplayResult) -> dict[str, Any]:
    """Extract the outcome-relevant fields for the trailing evidence entry."""

    summary: dict[str, Any] = {
        "kind": type(result).__name__,
        "exit_code": getattr(result, "exit_code", None),
        "capability_id": getattr(result, "capability_id", None),
        "version": getattr(result, "version", None),
        "tenant_id": getattr(result, "tenant_id", None),
        "duration_ms": getattr(result, "duration_ms", None),
        "step_count": len(getattr(result, "steps", []) or []),
        "recovery_count": len(getattr(result, "recovery_events", []) or []),
    }
    if isinstance(result, ReplayBusinessOutcome):
        summary["outcome_name"] = result.outcome_name
    if isinstance(result, ReplayHardFailure):
        summary["outcome_name"] = result.outcome_name
        summary["error"] = result.error
    if isinstance(result, ReplayEscalated):
        summary["intervention_id"] = result.intervention_id
        summary["reason"] = result.reason
    return summary


def _finalize_evidence(
    evidence: EvidenceWriter,
    *,
    status: str,
    result_summary: dict[str, Any] | None = None,
) -> None:
    """Log a trailing 'replay_end' entry and flush ``evidence.json`` to disk."""

    try:
        evidence.log(
            phase="replay",
            message="replay_end",
            metadata={"status": status, "result": result_summary or {}},
        )
        evidence.write_logs()
    except Exception:  # noqa: BLE001
        # Never let evidence bookkeeping mask the real result.
        pass


async def run_replay(
    capability: Capability,
    params: dict[str, Any],
    *,
    evidence_root: Path,
    headless: bool = True,
    config: ReplayConfig | None = None,
) -> ReplayResult:
    """Open a browser, run the replay, close the browser. Web-only."""

    # Local import: keeps replay.py free of browser-driver imports at
    # module-import time (the grep-based guard test enforces this).
    from cua.surface.web import open_web_surface

    evidence = _open_evidence(evidence_root, capability)
    async with open_web_surface(headless=headless) as surface:
        return await replay_with_surface(
            capability, surface, params, evidence, config
        )
