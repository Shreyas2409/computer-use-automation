# Evidence pack

Index of the eight artifacts the brief requires. Every entry is a genuine
live-recorded run against the target app on `http://localhost:8080` (or the
Summit-tenant instance on the same port with `TENANT=summit`). Older
directories from earlier wave verification remain committed for
traceability; the eight below are the canonical set the write-up references.

Reproduce any single item with the command in the last column — see
`../README.md` for setup, provider selection, and the escalation harness.

## The eight required items

| # | Item | Directory | What it shows | How to reproduce |
|---|------|-----------|---------------|------------------|
| 1 | Discovery run (live LLM, OpenAI provider) | `discovery_open_subaccount_20260818T024824Z/` | `gpt-5` (`gpt-5-2025-08-07`) drove the sub-account form end-to-end; 13 successful steps, policy-gated `submit` not attempted; compiled `artifacts/open_subaccount/v1.0.json` as `draft`. Contains `transcript.json` (full model turns), `evidence.json` (per-step log), `step_00.png`…`step_14.png`. | `.venv/bin/python -m cua discover --provider openai --goal '<see README>' --base-url http://localhost:8080 --tenant meridian --app-id meridian_servicing_console --target-version 2024.03 --capability-id open_subaccount --param member_id=10001 --param operator_password=demo123 --secret-param operator_password` |
| 2 | Successful replay (baseline path) | `replay_lookup_member_balance_meridian_20260816T182449Z/` | Deterministic replay of `lookup_member_balance` v2.2 with member `10001`. Status `ok`, checking balance extracted, per-step rung telemetry matches recording. | `.venv/bin/python -m cua replay --capability-id lookup_member_balance --param member_id=10001 --param operator_password=demo123` |
| 3 | Business-outcome replay (member 99999 → not found) | `replay_lookup_member_balance_meridian_20260816T063411Z/` | Same capability, member `99999` is not in the fixture data. Engine matches the declared `no_such_member` `business_outcome` node — no exception, process exit code `0` (business outcomes are successful replays), JSON status `business_outcome`. Screenshot: `step_05_outcome_no_such_member.png`. | `.venv/bin/python -m cua replay --capability-id lookup_member_balance --param member_id=99999 --param operator_password=demo123` |
| 4 | Hard-failure replay (`?inject=500`) | `replay_lookup_member_balance_meridian_20260816T064006Z/` | Server 500 injected after step 3. `server_error` outcome node (classification `hard_failure`) matches; JSON status `failed`, process exit code `2`. Screenshots: `step_04_action_error.png` and `step_04_outcome_server_error.png`. | `.venv/bin/python -m cua replay --capability-id lookup_member_balance --param member_id=10001 --param operator_password=demo123 --inject-mode 500 --inject-after-step 3` |
| 5 | Interstitial-recovery replay (`?inject=dialog`) | `replay_lookup_member_balance_meridian_20260816T180812Z/` | Servicing-terms dialog injected between steps 3 and 4. The `dismiss_dialog` recovery rule attached to step 5 (search-button click) fires, the dialog is dismissed, replay resumes, run ends `ok`. `evidence.json` records `recovery_triggered` and a subsequent `step_ok` on step 5. | `.venv/bin/python -m cua replay --capability-id lookup_member_balance --param member_id=10001 --param operator_password=demo123 --inject-mode dialog --inject-after-step 3` |
| 6 | Intervention with handoff + resume | `live_escalation_20260816T182729Z/` + companion `replay_lookup_member_balance_meridian_20260816T182729Z/` | Full lease cycle `AUTOMATION → PENDING_HANDOFF → HUMAN → RESUMING → AUTOMATION`, driven by the auditor script against the real operator console. `manifest.json` stitches the replay evidence dir to the console `/state` timeline; `final_operator_state.json` shows the terminal lease state; `driver_cycle_log.json` records every polling step. | `.venv/bin/python scripts/live_escalation_demo.py` |
| 7 | Agent catalog invocation (LLM picks tool cold from catalog) | `agent_catalog_invocation_20260816T182447Z/` | `gpt-4o` given only the catalog + one question; it picked `lookup_member_balance`, filled typed args, called it through the replay engine, and summarised the result. `transcript.json` is clean — the secret `operator_password` param is `***REDACTED***` in every recorded arguments blob and no schema `example` value for a `sensitivity=secret` field is published. | `.venv/bin/python demo_agent_invocation.py` |
| 8 | Cross-tenant replay under the Summit overlay | `replay_lookup_member_balance_summit_20260816T180943Z/` | Same base capability composed with `artifacts/lookup_member_balance/overlays/summit.v1.json`. `base_url` becomes `http://localhost:8080/servicing`, tenant labels differ (`Find Member`, `Servicing Profile`), same data + flow, same success outcome. Demonstrates the overlay compose path in production. | `TENANT=summit .venv/bin/python -m target_app.app` (start the Summit instance first), then `.venv/bin/python -m cua replay --capability-id lookup_member_balance --overlay artifacts/lookup_member_balance/overlays/summit.v1.json --param member_id=10001 --param operator_password=demo123` |

## Superseded (removed)

- `agent_catalog_invocation_20260816T063815Z/` — recorded before the catalog
  redaction fix landed. It leaked the operator-password value both as a
  published `example` on a `sensitivity=secret` param and as a raw
  `tool_calls[].arguments` string in the model transcript. The clean
  replacement is `agent_catalog_invocation_20260816T182447Z/` (item 7). See
  `../REPORT.md → Safety`.

- `discovery_open_subaccount_20260816T063050Z/`,
  `discovery_open_subaccount_20260816T063154Z/`,
  `discovery_open_subaccount_20260816T181601Z/` — earlier discovery runs
  recorded with `gpt-4o`. Superseded by
  `discovery_open_subaccount_20260818T024824Z/` (item 1) which uses `gpt-5`.

## Note on stray `demo123` occurrences

A grep of the entire `evidence/` tree for `demo123` returns hits only
inside `discovery_open_subaccount_20260818T024824Z/{transcript,evidence}.json`.
All three sub-categories are expected discovery-time content, not
redaction failures:

1. **Public login-page help text** — `target_app/templates/login.html`
   publicly renders `Demo credentials: demo / demo123` for the demo
   operator. Every accessibility snapshot captured after navigating to
   `/login` includes that help label verbatim.
2. **Goal-string echoes** — the discovery `--goal` string (which the
   operator wrote) reads `"Sign in as demo/demo123 …"` to help the model
   pick the right credentials; that goal text is embedded in every
   user-turn message the model receives, so it appears in each `user`
   turn of `transcript.json`.
3. **Typed textbox value reflected in the a11y snapshot** — after the
   model executes `type textbox Password = "demo123"`, the next
   accessibility snapshot captures the current textbox value. The
   discovery loop *needs* the plaintext credential at that moment to
   drive the field; that is the deliberate contract of a
   `sensitivity=secret` ParamSpec at discovery time (redact everywhere
   downstream, permit the type action itself).

Catalog, replay, and escalation evidence — the surfaces where the
operator supplies `operator_password` as a `sensitivity=secret` param
input — contain **zero** occurrences of the literal value; every
occurrence has been redacted to `***REDACTED***`. See `REPORT.md →
Safety` for the redaction mechanism.
