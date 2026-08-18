# REPORT — computer-use automation take-home

Wave-8 write-up. Companions: `README.md` (setup + commands), `evidence/EVIDENCE.md`
(the eight required committed artifacts).

## What was built

A working system in which a language model discovers a UI workflow against
a fake legacy credit-union servicing app, compiles the successful run into
a typed reusable **Capability artifact**, and that artifact replays
deterministically without a model in the loop — with policy enforcement,
a human-escalation lease over the same browser session, and structured
evidence for every flow (discovery transcripts + screenshots, replay
telemetry, escalation timelines, agent tool-use transcripts).

Concretely: a Flask target app with two tenants (`meridian`, `summit`)
under one Playwright-driven perception surface, four immutable artifact
versions of `lookup_member_balance` (v1.0 → v2.2) plus a Summit overlay,
one discovery-authored artifact (`open_subaccount/v1.0`, born `draft`),
147 unit tests, and 8 committed live-recorded evidence items covering
discovery, all replay classifications (ok, business_outcome, hard_failure,
recovered-via-dialog, escalated), cross-tenant overlay, and an agent
picking a capability cold from the catalog.

## Architectural decisions

- **The artifact is the contract.** `cua/schema.py` (Pydantic v2) defines
  everything downstream depends on: `Capability(target, params[], steps[],
  outcomes[])`. `model proposes, schema disposes` — discovery emits JSON,
  Pydantic validates, and only then does the registry accept it.
- **Discovery and replay are two disjoint code paths.** `cua/discover.py`
  imports Playwright *and* the model client; `cua/replay.py` imports
  Playwright *only*. A grep-guarded test in `tests/test_surface_and_ladder.py`
  fails CI if either `anthropic` or `openai` ever appears in the replay
  translation unit — this is the single most important invariant of the
  system (no model in production decisions, ever).
- **Provider portability at the seam, not everywhere.** `cua/llm.py` is a
  thin `ProviderClient` that hides SDK differences (Anthropic Messages vs.
  OpenAI Chat Completions with `tool_choice="required"`); each SDK is
  imported lazily so only the used provider needs to be installed.
- **Locator ladder for drift tolerance.** `cua/locate.py` records every
  step's ordered strategy list (role_name → label → text → table_cell →
  css → coordinates); replay records which rung actually matched, so
  drift is telemetry rather than a silent hazard (see
  `RungReport.drifted` in `cua/result.py`).
- **Policy is a gate, not advice.** `PolicyGate` is the single choke point
  in `cua/policy.py`. Discovery and replay both call `evaluate(action)`
  before Playwright touches the page; destructive actions and unknown
  URLs are refused; secrets are redacted at the evidence-write boundary.
- **Escalation is a control lease, not an exception.** `cua/escalate.py`
  models `AUTOMATION → PENDING_HANDOFF → HUMAN → RESUMING → AUTOMATION`
  as a state machine on a single browser session, with `resume_token`
  gating hand-back. The operator console (`cua/operator/`) is polling,
  not push, so it works over any transport a real ops room already has.
- **Tenancy via overlays, not forks.** `Overlay(base_version, patches[])`
  in `cua/registry.py` composes a base capability with tenant-scoped
  JSON-path patches — Summit shares the same steps, outcomes, and
  extraction logic as Meridian, differing only in `base_url`, button
  labels, and heading text.

## Audit trail

Waves 1 → 7 landed and were independently audited before this wave began:
schema/registry, surface + ladder + policy, discovery loop, replay + result
contract, escalation lease + operator console, catalog + agent-facing tool
translation, and multi-tenant overlay support. Every audit verdict is
reflected in the current test count (147/147 green as of Wave 8 close).
Wave 8 itself produced the evidence pack (`evidence/EVIDENCE.md`, 8 items),
this report, and the README rewrite, without modifying any `cua/` or
`target_app/` code.

## Safety

Three mechanisms, each verifiable from the committed evidence:

1. **Secret ParamSpec redaction.** `cua/catalog.py` strips the `example`
   field from any ParamSpec with `sensitivity=secret` before publishing
   the tool definition, and `demo_agent_invocation.py` redacts
   `operator_password` to `***REDACTED***` in every recorded transcript
   surface (tool-call `arguments`, `invocations[].input`, Anthropic
   `tool_use.input`). Verified in `evidence/agent_catalog_invocation_20260816T182447Z/transcript.json`.
2. **Policy-gated destructive actions.** The discovery run at
   `evidence/discovery_open_subaccount_20260818T024824Z/` completes with
   the form filled but no `Submit`; `cua/policy.yaml` refuses the
   sub-account submit button by role/name match, and the model was
   instructed to treat `form filled and ready` as success.
3. **Superseded transcript deletion.** One older
   `agent_catalog_invocation_20260816T063815Z/` recorded before the
   redaction fix leaked the operator password twice (as a published
   `example` on the secret param and as a raw `tool_calls[].arguments`
   string). It was deleted and cleanly replaced; the deletion is called
   out in `evidence/EVIDENCE.md → Superseded (removed)`.

## Artifact immutability — open finding

During the Wave-8 self-check the working tree carried uncommitted edits
against two already-released artifacts:
`artifacts/lookup_member_balance/v2.1.json` and
`artifacts/lookup_member_balance/overlays/summit.v1.json`. Both edits
were reverted after empirical validation:

- **v2.1.json** was edited to change the step-3 `dismiss_dialog` trigger
  text from `"Please confirm you have read"` to `"Please confirm your
  identity"` and to remove the step-5 recovery rule. The target app's
  actual dialog (see `target_app/templates/base.html`) renders `"Please
  confirm you have read the updated servicing terms…"`, so the edited
  trigger would silently never fire. Reverted; the HEAD version is
  correct.
- **overlays/summit.v1.json** added a `target.tenant_id` patch and two
  additional text-strategy patches for steps 5 and 7. The HEAD overlay
  (four patches, base_version 2.0) was replayed live against the Summit
  tenant on 2026-08-18 and finished `ok` with the expected outputs
  (`checking_balance=285043`, `member_status="Active"`); the extra
  patches were not needed. Reverted.

The general lesson: **released artifact files must not be edited in
place**. If a real-world dialog-text change turns out to be needed for
v2.1 (e.g. the target app rewords the interstitial), the correct
resolution is for the maintainer to mint `v2.3` (or an overlay
`v2`) rather than mutate a released version. This report leaves the
issue documented rather than minting versions from the auditor seat.

## Deviations & known limitations

- **Anthropic → OpenAI provider swap (billing).** The brief was written
  around Anthropic Claude; live runs in this repo were recorded against
  OpenAI because that account is currently funded. The provider seam
  (`cua/llm.py`) supports both cleanly; `--provider anthropic` is a
  drop-in swap and every tool-translation test exercises both shapes.
- **OpenAI default is `gpt-5`; API contract shifted.** `cua/llm.py`
  `DEFAULT_MODELS["openai"]` was bumped to `gpt-5`; that model rejects
  the legacy `max_tokens` parameter and requires `max_completion_tokens`,
  plus explicit `response_format={"type":"json_object"}` for the JSON
  proposal call. Both were adjusted in `cua/llm.py`. The standalone
  `demo_agent_invocation.py` (repo root, not part of the `cua/` engine)
  still calls the legacy API directly, so under the current default it
  must be invoked as `CUA_MODEL=gpt-4o .venv/bin/python
  demo_agent_invocation.py`; this is documented in the README.
- **Playwright 1.62 removed `Page.accessibility.snapshot`.** Rather than
  pinning Playwright <=1.59, we ship a DOM-walk accessibility capture in
  `cua/surface/observation.py::capture_a11y_snapshot` and a compatibility
  shim in `cua/surface/_playwright_compat.py` that installs a
  Playwright-compatible `page.accessibility.snapshot(interesting_only=…)`
  method. The shim is imported for its side effect from `cua/__init__.py`
  and `cua/__main__.py`; nothing downstream (locator ladder, action layer,
  replay) is aware of the upstream SDK change.
- **Discovery unchanged-observation guard.** `cua/discover.py` breaks the
  loop with `status="stuck"` after `MAX_UNCHANGED_OBS` consecutive
  identical observation hashes (see the `observation_hash` block around
  line 735). Without this the loop could burn tokens indefinitely on a
  page the model can't make progress against; with it, a stuck run
  terminates promptly and the transcript records the reason.
- **Demo-secret hits in discovery evidence are public-page content.**
  The target-app login page publicly renders `Demo credentials: demo /
  demo123`, so the accessibility snapshot captured at discovery time
  contains that string verbatim. Catalog, replay, and escalation
  evidence (where `operator_password` is supplied as a secret ParamSpec)
  contain zero occurrences of the value; see `evidence/EVIDENCE.md →
  Note on stray demo123 occurrences`.
