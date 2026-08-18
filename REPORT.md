# REPORT

Companions: `README.md` (setup + every command line) and
`evidence/EVIDENCE.md` (the eight committed live-recorded artifacts).

## Architecture

Two disjoint code paths sit under one perception surface.

- **Discovery** (`cua/discover.py`) drives the target app with an LLM in a
  tool-use loop. Each turn: observe → the model proposes a typed action
  from a fixed toolset (`navigate`, `click`, `type_text`, `select_option`, `read_text`, `wait_for`, `finish`) → policy gate evaluates → Playwright executes →
  next observation. Only `finish` compiles the successful trace into a
  `Capability` artifact.
- **Replay** (`cua/replay.py`) is a deterministic interpreter over thatartifact. **It structurally cannot import the Anthropic or OpenAI SDK**— grep-guarded by `tests/test_surface_and_ladder.py`. There is nomodel in the production decision loop.
- **Perception surface** (`cua/surface/`): `base.py` is a `Protocol`,`web.py` is the Playwright implementation, `observation.py` normalisesthe a11y tree. A desktop surface is a seam, not an implementation.
- **Locate** (`cua/locate.py`) is a locator ladder — `role_name → label → text → table_cell → css → coordinates`. CSS as the *primary* rung isrefused (`schema.py` raises `ValueError`); it can only sit below asemantic rung.
- **Policy gate** (`cua/policy.py`) is the single choke point in frontof every Playwright action; both loops call it.
- **Escalation** (`cua/escalate.py` + `cua/operator/`) models control asa lease over a single browser session; the operator UI is a pollingFlask app.
- **Catalog** (`cua/catalog.py`) translates approved capabilities intoAnthropic and OpenAI tool definitions and executes them through thereplay engine.
- **LLM provider seam** (`cua/llm.py`). Brief was written aroundAnthropic; live runs in this repo were recorded against OpenAIbecause that account is currently funded. Both providers are firstclass and every tool-translation test exercises both shapes.`DEFAULT_MODELS["openai"] = "gpt-5"`; that model rejects the legacy`max_tokens` parameter, so `cua/llm.py` sends`max_completion_tokens` plus`response_format={"type":"json_object"}` for the JSON proposalcall. `demo_agent_invocation.py` uses the same `cua/llm.py`provider client, so it works under the default model withoutworkarounds.
- **Playwright 1.62 compatibility shim, not a version pin.**Playwright removed `Page.accessibility` in 1.60. Rather than pinthe whole repo to <=1.59 the surface ships two small pieces:`cua/surface/observation.py::capture_a11y_snapshot` (a DOM-walkproducing a Playwright-snapshot-shaped nested dict via`page.evaluate`) and `cua/surface/_playwright_compat.py` (whichinstalls the compat property on `Page` as a side effect, importedfrom `cua/__init__.py` and `cua/__main__.py`). Nothing downstream —locator ladder, action layer, replay — is aware of the upstream SDKchange.

## Artifact schema

`cua/schema.py`, Pydantic v2, `extra="forbid"` on every model.

- `Capability(target, params[], outputs[], outcomes[], steps[], version_major, version_minor, schema_version, approval, ...)`. Everymodel rejects unknown keys — a schema drift trips validation on load,not at replay time.
- **Three version axes.** `schema_version` (the schema itself),`capability version_major.version_minor` (behaviour of one capability),`target.target_version` (the upstream app build this recording wastaken against). `classify_version_bump()` compares two capabilityrevisions and returns `none | patch | minor | major` —contract-affecting differences (`params`, `outputs`, `outcomes` names)force a major bump.
- **Params** carry `type` and `sensitivity ∈ {public, internal, pii, secret}`. `secret` params have their `example` stripped from thepublished tool schema (`cua/catalog.py`) and their runtime valuesredacted at every evidence-write boundary (`cua/evidence.py`).
- **Outputs** are `ValueRef`s (extraction directives — locator + optionalnormaliser) validated for shape at load time.
- **Outcomes** are classified `business_outcome | recoverable | hard_failure` and matched *after* every action; `business_outcome`exits with `0` (a meaningful successful outcome for the caller),`hard_failure` exits with `2`, escalation exits with `3`.
- **Steps** carry a `Locator` (the ladder) plus an optional post-action`checkpoint` and optional `recovery` rules; replay records the`matched_rung` versus the `recorded_rung` so drift is telemetry(`RungReport.drifted` in `cua/result.py`) rather than a silent hazard.
- **Immutability.** Released `v*.json` files must not be edited in
  place. New behaviour necessitates a new version file or a new overlay version. This ensures that historical replay artifacts remain strictly tied to the contract they were verified against.

## Determinism & error handling

- **No model in the replay loop.** Every decision — which step to run,which locator rung to try, whether a checkpoint fired, which outcomematched — is pre-baked in the artifact.
- **No unbounded sleeps.** Every wait is anchored to an explicit`checkpoint` or a bounded timeout; `checkpoint_status` records `ok | recovered | failed | skipped`.
- **Four-variant **`ReplayResult` (`cua/result.py`):`ok`→0, `business_outcome`→0, `failed`→2, `escalated`→3. The fullstatus string is always printed as JSON on stdout so callers canbranch on `status` without parsing stderr.
- **Failure evidence is self-contained.** `ReplayHardFailure` records`step_index`, `action`, `expected`, `observed`, `outcome_name`,`error`, and the evidence directory — enough to reproduce withoutreading the surrounding logs.
- **Ladder recovery.** If the recorded rung misses, replay walks theremaining rungs in order; a match on a lower rung completes the stepand records `drifted=true` in `RungReport`, so a slow tenant driftbecomes a visible trend rather than a sudden outage.
- **Recovery rules** (e.g. `dismiss_dialog`) may attach to a step; theengine attempts them once before failing the checkpoint (see the`?inject=dialog` evidence). After a human handback the enginere-verifies the checkpoint before proceeding.

## Heterogeneity & multi-tenant

- **Two tenants share one artifact.** `target_app` runs Meridian(`route_prefix=""`) or Summit (`route_prefix="/servicing"`, differentbutton labels and headings) selected by `TENANT=` env var.
- **Overlays, not forks.** `TenantOverlay(base_capability, base_version, tenant_id, patches[])` in `cua/registry.py`; `compose(base, overlay)`applies JSON-path patches (`target.tenant_id`, `target.base_url`,`steps[i].target.strategies[j].spec.name`, …) then **re-validatesthrough Pydantic**. The composed capability is a first-class`Capability`; downstream code cannot tell it apart from anon-overlaid one.
- **Overlay staleness is a load-time failure.** `compose` refuses anoverlay pinned to a major that the base has since moved past (raises`ValueError`), so an overlay left behind by a base bump does notsilently run against the wrong recording.
- **Same steps, same outcomes, same outputs.** The Summit replay(evidence item 8) returns the same `checking_balance=285043` and`member_status="Active"` as the Meridian baseline (item 2). Verifiedby `tests/test_registry.py::test_summit_overlay_patches_every_meridian_string`.
- **Perception surface is a Protocol.** `cua/surface/base.py` istransport-agnostic; a desktop or terminal surface would be anotherimplementation of the same seam. Only the web surface ships.

## Escalation & handoff

`cua/escalate.py` — a control lease over a single browser session.

- **State machine.**
  `AUTOMATION → PENDING_HANDOFF → HUMAN → RESUMING → AUTOMATION`, with
  one escape hatch (`PENDING_HANDOFF → AUTOMATION`) used when an
  operator refuses to take over. Illegal transitions raise
  `IllegalTransition`; wrong-actor calls raise `HolderMismatch`.
- **One session, two drivers.** The engine and the operator both drivethe *same* Playwright/CDP session (`cua/surface/web.py :: connect_web_surface` attaches via `connect_over_cdp`), so a human'sclicks, cookies, and form state survive the handback. `escalate.py`never imports Playwright; the operator UI is wired to a live browservia a `HumanActionHandler` seam registered by the demo harness.
- `resume_token`** gates handback.** The engine mints a random tokenwhen it parks (`new_resume_token()`); the operator must present it tohand back (`Broker.resume(..., resume_token=…)`). This makes theoperator UI safe against a stale/careless `POST /resume`.
- **Verified end-to-end** by `scripts/live_escalation_demo.py`, whichspawns a real browser + operator console, polls `/state`, takescontrol, and hands back with the correct token; evidence in`evidence/live_escalation_20260816T182729Z/` +`evidence/replay_lookup_member_balance_meridian_20260816T182729Z/`.

## Safety

Three mechanisms, each verifiable from the committed evidence.

- **Policy gate at the action boundary.** `cua/policy.py::PolicyGate`is the single choke point. `policy.yaml` declares an allowlist ofdomains and route patterns and a set of`require_confirmation_patterns` (verbs like `submit`, `delete`,`transfer`). Discovery *and* replay both call`PolicyGate.evaluate(action)` before Playwright touches the page;anything outside the allowlist or matching a destructive verbwithout an approved confirmation is refused. Evidence:`evidence/discovery_open_subaccount_20260818T173007Z/` completeswith the sub-account form filled but the `Submit` button notattempted — the model was instructed to treat the completed form assuccess precisely because the gate would refuse the submit.
- **Sensitivity-driven redaction at the write boundary.** `Sensitivity = Literal["public", "internal", "pii", "secret"]` (`cua/schema.py:19`).Every write to `evidence/` goes through `cua/evidence.py`, whichredacts by declared ParamSpec sensitivity first (`pii` and `secret`become shape-only, e.g. `<redacted:string:5>`) and then appliesregex fallback from `policy.yaml::redaction_rules` for free-text`message` and `error` fields. `cua/catalog.py` strips `example` fromany `sensitivity=secret` ParamSpec before publishing the tool schema(`catalog.py:160`), so the demo password never reaches the LLMcatalog. Verified in`evidence/agent_catalog_invocation_20260816T182447Z/transcript.json`:`operator_password` is `***REDACTED***` in every recorded argumentsblob.
- **Superseded artifacts removed, not overwritten.**`agent_catalog_invocation_20260816T063815Z/` was recorded before thecatalog redaction fix landed and leaked the password twice (apublished `example` on a secret param, and a raw`tool_calls[].arguments` string). It was deleted;`agent_catalog_invocation_20260816T182447Z/` is the cleanreplacement (item 7 in `evidence/EVIDENCE.md`).

**Note on stray `demo123` occurrences.** A grep of the entire
`evidence/` tree for `demo123` returns hits only inside
`discovery_open_subaccount_*/{transcript,evidence}.json`.
These are expected discovery-time content; the app's login page renders them, so they appear in snapshots. The value is handled as a `secret` and redacted in all downstream artifacts.

## Cuts

Everything below was scoped *out* on purpose; each has a one-linereason.

- **No desktop surface, only a seam.** `cua/surface/base.py` is a`Protocol`; only `cua/surface/web.py` is implemented. A desktop-CDPimplementation is a future addition against the same Protocol andwould not touch `replay.py` or `locate.py`.
- **Operator console is a polling mock.** `cua/operator/server.py`serves `/state`, `/take_control`, `/intervene`, `/handback` overplain HTTP; the driver polls. No WebSockets, no SSE, no auth, nomulti-operator arbitration, no persistence. Sufficient for anauditor to see the full lease cycle; a production console wouldtrade this for a push transport.
- **No queueing / multi-tenant infra.** One target-app process pertenant (restart to switch); one Playwright browser per replay run;no job queue, no rate limiter, no per-tenant sandboxing. Themulti-tenant *contract* (overlays) is proven; the *infrastructure*is not.
- **No LLM fallback on replay failure — deliberate.** When a replaystep fails and no declared recovery rule matches, the engine emits`ReplayHardFailure` (exit 2). It does *not* silently phone the LLMto invent a fix. Falling back to the model in production wouldinvalidate the single most important guarantee of the system(deterministic, auditable replay). The recovery story is:ladder-drift telemetry surfaces the change → maintainerre-discovers → new artifact minor minted (`v2.1` → `v2.2`).
- **No schema migration framework.** `schema_version` is a plaininteger with no auto-upgrade path. Bumping it requires re-approvingevery capability by hand — intentional, because a silent migrationwould let a stale artifact quietly succeed against a shiftedcontract.
- **Tests cover schema, registry, error taxonomy, and policy gate —not end-to-end Playwright in CI.** 147 unit tests exercise theschema (validation, version-bump classification), the registry(immutability, overlay compose), the locator ladder (drifttelemetry, CSS-primary rejection), the error taxonomy(`ReplayResult` variants and exit codes), the policy gate(allowlist, redaction rules), the catalog (tool translation,secret-example stripping), escalate (state machine, holderassertion, resume-token gating), and the discovery loop(unchanged-observation guard). Full end-to-end Playwright replaysagainst a live target app are proven by the committed evidencepack, not by a CI job — the live suite is expensive andbrowser-flaky and would gate PRs on unrelated Chromium updates.
- **Discovery unchanged-observation guard, not infinite retries.**`cua/discover.py` breaks the tool-use loop with `status="stuck"`after `MAX_UNCHANGED_OBS` consecutive identical observation hashes.Without this the loop could burn tokens indefinitely on a page themodel can't make progress against; with it, a stuck run terminatespromptly and the transcript records the reason.