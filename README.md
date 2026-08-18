# cua-take-home — computer-use automation

A working system where an LLM discovers a UI workflow against a fake legacycredit-union app, compiles the successful run into a typed reusable**Capability artifact**, and that artifact replays deterministically with**no model in the loop** — with policy gating, human escalation over thesame browser session, and committed evidence for every flow.

The model discovers. The artifact is the capability. Deterministic replayis the production path.

## Architecture

```
target_app/         Flask "servicing console" (legacy-hostile HTML: nested
                    tables, ASP.NET-style ids, no data-testid, but real
                    <label for> and real button text). Tenants: meridian
                    (route prefix "") and summit (prefix /servicing).
cua/
  schema.py         Capability artifact (Pydantic v2). Three version axes:
                    schema_version, capability major.minor, target_version.
  registry.py       Immutable versioned files + tenant overlays; approved-
                    only load path.
  surface/          Perception seam. base.py is a Protocol; web.py drives
                    Playwright; observation.py normalizes the a11y tree.
                    Includes a Playwright-1.62 compat shim (see below).
  locate.py         Locator ladder (role_name → label → text → table_cell →
                    css → coordinates); replay records which rung matched.
  policy.py         PolicyGate + redaction; enforced at the action layer.
  discover.py       LLM tool-use loop. Emits draft artifact on `finish`.
  replay.py         Deterministic executor. STRUCTURALLY cannot import the
                    Anthropic or OpenAI SDKs (grep-guarded in tests/).
  escalate.py       Control lease state machine:
                    AUTOMATION → PENDING_HANDOFF → HUMAN → RESUMING → AUTOMATION
  operator/         Flask operator console (polling; not push).
  catalog.py        Agent-facing surface: list published, translate schema
                    to Anthropic/OpenAI tool defs, invoke by name.
  llm.py            Provider-portable client (Anthropic + OpenAI); both
                    SDKs imported lazily.
  result.py         ReplayResult contract + per-step rung telemetry.
  evidence.py       Structured JSONL log + screenshots + redaction hooks.
artifacts/          Immutable capability registry (v*.json + overlays/).
evidence/           Committed evidence pack; see evidence/EVIDENCE.md.
tests/              147 tests: schema, registry, surface+ladder, policy,
                    replay, escalate, catalog, discover, llm.
```

## Setup

Prerequisites: **Python 3.11+** and a Chromium install for Playwright.

```
python3.11 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
cp .env.example .env
# then edit .env and add ONE of:
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
```

Boot the target app (meridian tenant, port 8080):

```
.venv/bin/python -m target_app.app                 # meridian (default)
TENANT=summit .venv/bin/python -m target_app.app   # summit (route /servicing)
```

Only one tenant occupies port 8080 at a time — restart the process to switch.

Run the full test suite:

```
.venv/bin/python -m pytest tests/   # 147 passed
```

## Demo commands

Every command below has been executed as part of the Wave-8 self-check.Reference evidence directories are indexed in `evidence/EVIDENCE.md`.

### Discovery (LLM drives the UI, compiles a new capability)

```
# OpenAI provider (default model: gpt-5; latest committed run:
# evidence/discovery_open_subaccount_20260818T024824Z/):
.venv/bin/python -m cua discover \
  --provider openai \
  --goal "Sign in as demo/demo123 at the login page found at http://localhost:8080/login. Then navigate to member 10001's profile via the member search. Click the link to open a new sub-account. On the sub-account form, choose Account type = Checking, enter Opening deposit = 25.00, and choose the first Funding source option available. Then call finish. The Submit button is gated by policy — do not attempt to submit; treat 'form filled and ready' as success." \
  --base-url http://localhost:8080 --tenant meridian \
  --app-id meridian_servicing_console --target-version 2024.03 \
  --capability-id open_subaccount \
  --param member_id=10001 --param operator_password=demo123 \
  --secret-param operator_password

# Anthropic provider (Claude Sonnet):
.venv/bin/python -m cua discover --provider anthropic  ...same args...
```

Provider auto-detects from whichever key is present in `.env` when`--provider` is omitted. The successful run writes`artifacts/open_subaccount/v1.0.json` (born `draft`) and`evidence/discovery_<capability>_<ts>/` with per-step screenshots.

### Deterministic replay (no model in the loop)

```
# 1. Successful path — member 10001, balance extracted:
.venv/bin/python -m cua replay --capability-id lookup_member_balance \
  --param member_id=10001 --param operator_password=demo123

# 2. Business outcome (member not found):
.venv/bin/python -m cua replay --capability-id lookup_member_balance \
  --param member_id=99999 --param operator_password=demo123

# 3. Hard failure (server 500 injected):
.venv/bin/python -m cua replay --capability-id lookup_member_balance \
  --param member_id=10001 --param operator_password=demo123 \
  --inject-mode 500 --inject-after-step 3

# 4. Interstitial-recovery (servicing-terms dialog injected on step 4;
#    dismiss_dialog rule fires at step 5, run completes ok):
.venv/bin/python -m cua replay --capability-id lookup_member_balance \
  --param member_id=10001 --param operator_password=demo123 \
  --inject-mode dialog --inject-after-step 3

# 5. Cross-tenant replay under the Summit overlay
#    (requires TENANT=summit target app on 8080):
.venv/bin/python -m cua replay --capability-id lookup_member_balance \
  --overlay artifacts/lookup_member_balance/overlays/summit.v1.json \
  --param member_id=10001 --param operator_password=demo123
```

Process exit codes: `0` for both `ok` and `business_outcome` (both aresuccessful replays from the engine's perspective), `2` for `failed`(hard failure or declared hard-failure outcome), `3` for `escalated`.The full status string (`ok`/`business_outcome`/`failed`/`escalated`) isalways printed as JSON on stdout.

### Escalation demo (full lease cycle, no human required)

```
.venv/bin/python scripts/live_escalation_demo.py
```

Runs `cua replay --enable-escalation --inject-mode escalate` as asubprocess against a real browser and the operator console on`http://127.0.0.1:8765`, polls `/state` until the intervention opens,takes control, then hands back with the engine's `resume_token`. Writes a`manifest.json` that stitches the replay evidence dir to the consoletimeline.

### Agent-facing catalog

```
# List every approved capability as an Anthropic-shaped tool definition:
.venv/bin/python -m cua catalog list --tool-only

# Invoke one by name (validates against ParamSpec, runs replay):
.venv/bin/python -m cua catalog invoke --name lookup_member_balance \
  --param member_id=10001 --param operator_password=demo123

# One-shot LLM demo: cold catalog → tool pick → invoke → summary.
# Under the current OpenAI default (gpt-5) the demo needs an explicit
# CUA_MODEL override because the standalone demo script pins the legacy
# max_tokens API; gpt-4o accepts it directly:
CUA_MODEL=gpt-4o .venv/bin/python demo_agent_invocation.py
```

`demo_agent_invocation.py` auto-detects the provider from whichever keyis set in `.env`; override with `DEMO_PROVIDER=anthropic|openai`. See`REPORT.md → Deviations & known limitations` for the gpt-5 API notes.

### Operator console standalone

```
.venv/bin/python -m cua operator --host 127.0.0.1 --port 8765
```

Runs the operator console with no engine attached. `/take_control`returns 409 until an intervention is opened programmatically.

## Structural guarantees

- `cua/replay.py`** cannot import the Anthropic or OpenAI SDKs.** Enforcedby a grep-guarded test in `tests/test_surface_and_ladder.py` — CI failsloudly if either import string ever appears. Replay is a pureinterpreter over the artifact; there is no model in the decision loop.
- **Drafts are invisible to the catalog.** `cua catalog list` and theagent demo only ever see capabilities with at least one `approved`version on disk.
- **Secrets never reach the tool schema.** ParamSpecs with`sensitivity=secret` have their `example` field stripped from thepublished tool definition; runtime values are redacted before evidenceis written.

## Playwright 1.62 compatibility note

Playwright removed `Page.accessibility` in 1.60. The discovery loop stillexpects `page.accessibility.snapshot(interesting_only=True)`; we ship twopieces that keep the loop working without pinning Playwright to <=1.59:

- `cua/surface/observation.py :: capture_a11y_snapshot` — a DOM-walkimplementation that produces a Playwright-snapshot-shaped nested dictusing only APIs supported in 1.62 (`page.evaluate`).
- `cua/surface/_playwright_compat.py` — installs a compat property on`Page` that delegates to the DOM-walk capture. Imported for its sideeffect from `cua/__init__.py` and `cua/__main__.py`.

Everything downstream (locator ladder, action layer, replay) is unawareof the upstream SDK change.