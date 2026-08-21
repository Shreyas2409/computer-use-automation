# Adaptation Log

## Scope cut (decided, not reopening)

Given the time budget, only three capabilities are recorded for Meridian
Core: `mc_lookup_balance` (done), `mc_transfer` (in progress), and
`mc_place_hold` as teller1 (planned — deliberately chosen last because one
run produces both the permission-denied business outcome and the escalation
demo in a single recording). **Open New Share and Update Member Information
are explicitly out of scope** — not attempted, not partially built. Business
outcomes for insufficient funds, HOLD-share rejection, and same-share
rejection are exercised as replays of `mc_transfer` with different inputs
against member 100987, not separate recordings; likewise member-not-found
against `mc_lookup_balance`.

Every edit to an existing core module (`cua/`), with the one-line reason
config/data alone couldn't reach it. New files (artifacts, policy.yaml
entries, launcher scripts) are not logged here — only changes to files that
existed before this adaptation.

| File | Change | Why config couldn't do it |
|---|---|---|
| `cua/replay.py` | `_policy_verdict` now derives the policy verb from the clicked element's label (via a new `_click_label` helper) instead of always using the generic action kind (`"click"`). | `policy.yaml`'s `require_confirmation_patterns` (Submit/Confirm/Transfer/...) is matched via substring against `Action.verb`. At replay, `verb` was always `step.action`, one of 7 fixed literals — no policy.yaml value can ever match, because the field being matched never contains a button label. `discover.py` already derives verb from the clicked element's name for the same gate; this makes replay consistent with it. No schema change — the label is read from data already recorded in the locator ladder. |
| `cua/catalog.py` | `InvokeOptions` gained `escalation_broker`/`escalation_timeout_s`; `invoke()` threads them into `ReplayConfig`; `create_app()` accepts a caller-supplied broker and applies it to every `/invoke` request. | No config value can inject a Python object (a broker instance) into a constructor call. Even the CLI only escalates when `--enable-escalation` builds a broker and threads it through (`cua/__main__.py:293-311`) — `catalog.py` had no equivalent knob at all, so any capability invoked through the API could never escalate, only hard-fail. `catalog.py` stays broker-agnostic (accepts one, doesn't construct it), matching its existing `evidence_root`/`artifacts_dir` pattern; a separate new launcher script owns building the broker via the existing `_start_operator()` helper. |
| `cua/schema.py`, `cua/locate.py`, `cua/surface/web.py`, `cua/discover.py` | New `label_cell` locator kind: when `role_name` resolution finds no accessible name at all for an input/select/textarea, fall back to locating the control via the table row containing its exact adjacent label text (anchored via `xpath=ancestor::tr[1]` from the label cell — not a page-wide `tr.filter(has=...)`, which on a nested-table layout also matches every ancestor row and collapses every field onto the first control in document order; found and fixed via direct empirical testing against the live target). | Meridian Core's form fields are `<td class="lbl">Label:</td>` next to a bare `<input>` — no `<label for>`, `aria-label`, or `aria-labelledby` anywhere on the entire site (verified across signon, member search, transfer, open-share, update, hold). Real Chromium computes an empty accessible name for these regardless of implementation; no config/data can invent a label association the markup doesn't have. This is the largest core change of the adaptation — flagged per the brief's own framing as the clearest signal of where the original core was tuned to a cleaner-markup target. |
| `cua/surface/observation.py` | `accessibleName()`'s custom shim now implements the standard HTML-AAM rule that `<input type="submit"\|"button"\|"reset">` takes its accessible name from its `value` attribute, checked before falling through to placeholder/title/textContent. | Not a Meridian Core–specific gap — a real spec deviation in this repo's Playwright-1.60+ replacement for the removed `page.accessibility.snapshot()` (real Chromium has always computed this correctly; this custom shim didn't). Directly caused a hard crash during discovery: the model, unable to see any name for the classic `<input type=submit value="Sign On">` button, called `click(role="button", name="")`; the empty string is falsy in `build_ladder`'s `if role and name:` check (silently dropping the role_name rung) while Playwright's non-exact `get_by_role(name="")` matches every button as a substring, together crashing in `build_ladder`'s "CSS cannot be primary" guard once no other rung had data. Verified via direct script: the button now reports `"Sign On"` and resolves uniquely through the existing, unmodified `role_name` mechanism — no locator-kind or discovery-logic change needed. |
| `cua/discover.py` | The stuck-detector (`MAX_UNCHANGED_OBS = 3` identical observation hashes in a row) now skips flagging "stuck" when the actions connecting those identical observations were all successful non-mutating actions (`read`, `wait_for`). | Also not Meridian-Core-specific — a generic discovery-loop heuristic gap. A capability that legitimately reads several values off one static page (e.g. multiple table rows, optionally waiting for an element first) produces identical observations by design each time — that's correct behavior, not stalling, but the original detector had no exception for it (the original demo's 1-2-output capability never exercised 3+ consecutive same-page reads, so this never surfaced). `self.steps` only records successfully-completed actions, so the exemption only fires when these are genuinely succeeding repeatedly; a real stall (repeated failures or repeated no-op clicks/types/selects/navigates) still trips the detector unchanged. First pass only exempted `read`; a run adding a `wait_for` before the read tripped it again, so the exemption was broadened to cover both non-mutating kinds. Considered raising `MAX_UNCHANGED_OBS` instead — rejected, since it doesn't fix the root cause (any capability needing more reads would still eventually trip it) and weakens the genuine anti-loop protection for actual click/type stalls. |

| `cua/surface/web.py` | `select()` now selects a `<select>` option by its `value` attribute (`select_option(value=...)`) instead of its visible label (`select_option(label=...)`). | Not Meridian-Core-specific — any target whose dropdown option labels embed live/changing data (here: a share's current balance, e.g. `"100234-S0070 - Share Draft (Checking) ($212.54)"`) breaks label-matching, because the recorded label text is a point-in-time snapshot that won't match once the balance changes, and can never be parameterized against a stable input (the balance is baked into the same string as the identifier). The site's `<option value="100234-S0070">...</option>` carries the stable share ID as its `value` attribute — matching on that lets `from_share`/`to_share` actually work as reusable inputs, which the recorded-outcome plan (replaying one Transfer capability against different members/shares) depends on. One line; no config value can change which Playwright matching mode a hardcoded method call uses. |

| `cua/surface/web.py` | `_label_cell_locator` (and its callers `_locator_for`/`_resolve_strategy`) made async; now checks whether the row has an input/select/textarea and, if not, falls back to the row's second `<td>` — the plain-text value cell. | Needed to *read* label:value data rows (e.g. a confirmation page's "Confirmation:" / "CN123456" pair), not just fill form fields — the same `<td class="lbl">Label:</td><td>Value</td>` row shape used throughout Meridian Core, but on the output-extraction side. The existing `label_cell` only ever looked for a control to click/type into; a plain read-only row has none, so it always resolved to nothing. No config fix possible: needing an async row-content check to decide the fallback is a real code-level branch, not a data value. Verified directly against a real row (`Member No.:` → `100234`) before use. |

| `cua/locate.py`, `cua/surface/web.py`, `cua/discover.py` | New shared `label_cell_pattern()` helper: matches a label's text with trailing punctuation/whitespace normalized (optional trailing `:`/`.`), used by both `WebSurface._label_cell_locator` (replay) and `_resolve_label_cell` (discovery) instead of an exact string match. | Labels on Meridian Core are inconsistently punctuated — some end in a colon, some don't (a row labeled by a bare share ID, `"100234-S0070:"`, vs. a caller-supplied value `"100234-S0070"` with no colon) — found when `mc_transfer` v2.0's resulting-balance output failed to resolve on a real end-to-end replay. Fixed once, centrally, rather than patching the one locator spec that broke: any label lookup on this site benefits, including ones not yet hit. Verified against both the failing case and a same-prefix-different-suffix case (`"100234-S0070-3:"`) to confirm no false-positive collisions between similar share IDs. |

| `cua/surface/web.py` | `_label_cell_locator` gained `column_offset` (spec field, default `1`, preserving prior behavior): when falling back to a plain value cell, returns the row's `column_offset`-th `<td>` instead of always the second. | Found via the catalog verification pass: `mc_lookup_balance`'s `balance_row_2`/`balance_row_3` outputs were anchored on the exact dollar figure seen at discovery time; once live balances drifted (this server's data changes between sessions), those locators silently fell through every rung to the `css: {"selector": "td"}` last resort, which returned the *first `<td>` on the whole page* (the site header) instead of failing — a wrong answer from a financial capability, not a clean error. Re-anchoring on the row's stable share ID (via `label_cell`, the same mechanism already proven for `mc_transfer`) fixes it, but the member-record table has 4 columns (Share ID \| Type \| Balance \| Status), not the 2-column label:value shape `label_cell`'s fallback assumed — needed the offset to land on "Balance" instead of always "Type". Verified against the live target through the actual catalog HTTP endpoint (not just replay CLI) before and after: was returning page-header text, now returns the current, correct balance. |
| `cua/__main__.py` | `--inject-mode`'s argparse `choices` widened to add Meridian Core's actual fault vocabulary (`validation`, `permission`, `timeout`, `maintenance`, `server`) alongside the original demo target's (`notfound`, `slow`, `500`, `expired`, `dialog`, `denied`, `escalate`). | Found while building the four-outcome evidence package: `--inject-mode server` was rejected outright by argparse — the choices list was hardcoded to the *original* target's fault kinds, not Meridian Core's, even though `_maybe_inject` itself just appends whatever string is given verbatim to the URL and doesn't care what it is. Config alone can't widen an argparse validation list. Kept the choices restriction (rather than removing it) to preserve the typo-catching safety net; just broadened the vocabulary it accepts. |

## Known limitation — logged, not fixed (time budget)

**The `css: {"selector": "td"}` last-resort rung can never fail, which defeats
rung-drift detection for exactly the cases that most need it.**

Found in the same pass as the `balance_row_2`/`balance_row_3` bug above: when
every higher-durability rung in a ladder misses, the recorded `css` fallback
being an over-broad selector like `"td"` (any table cell) with `.first`
*always* resolves to *something* — there is no way for it to come up empty,
so replay proceeds as if resolution succeeded, silently returning whatever
that "first td" happens to be. The entire point of rung-drift telemetry is
that a miss should be observable; an unconditionally-matching fallback rung
defeats that for any capability whose lower rungs happen to be this broad.

This is separate from (and more serious than) the specific balance-column bug
above: fixing that one bug doesn't fix the general hazard — any other output
or step recorded with an equally broad `css` selector as its last rung has
the same silent-failure shape.

**Considered fix**: reject an over-broad CSS selector at record time, the
same way `build_ladder()` already refuses to let `css` be the *primary* rung
(`cua/locate.py`) — e.g. flag single-tag-name selectors (`"td"`, `"div"`,
`"a"`) with no further qualifier as too broad to serve even as a fallback,
forcing the recording to fall back further (`coordinates`) or fail loudly
instead of silently matching everything on the page.

**Not implemented** — logged here per explicit direction to document rather
than build it if time doesn't allow. Not fixed today; flagged for whoever
picks this up next, and called out explicitly in the write-up under how
locators can fail.

## Considered and deliberately NOT made

**`cua/schema.py` / `cua/replay.py` — a `ValueRef` extension for step-to-step
data dependencies (e.g. `{"from_step": 4, "output": "txn_token"}`), to carry
Meridian Core's per-transaction hidden token from the page where it's read to
the request where it's submitted.**

Investigated because the target brief describes the token as something that
"must be read off the page and submitted with the transaction," which reads
like a step-N-produces / step-N+M-consumes dependency the existing `ValueRef`
(param-only) can't express.

Verified against the live target instead of assuming: recon'd all four write
flows (Funds Transfer, Open Share, Update Member Info, Place Hold). In every
one, the confirm/review page echoes the token (and every field value) back as
a hidden `<input>` inside a real HTML `<form>`. Because replay drives an
actual Playwright browser and every action (click/type/select) operates on
live DOM state through the locator ladder — not on values serialized out of
the artifact — clicking the real "Continue"/"Post..." buttons submits that
hidden field automatically as part of the browser's native form POST. No step
ever needs to manually read the token and re-type it elsewhere.

Net: the dependency the schema change would have solved doesn't occur
anywhere on this target. Building it would have added a schema-version bump,
a new replay resolution path, and a new secret-handling seam for a case this
target never presents. Left out on that basis, not for lack of time.

## Open finding — not resolved, not to be overstated

HOLD-share and same-share transfers were rejected at the review step earlier
in the session against member 100234, but did not reproduce against 100987 —
both proceeded to the policy-gated post step and escalated. Whether this is
per-member state, share-pair dependent, or something else was not
investigated further given the time budget.

The more useful result sits alongside it: in both cases the policy gate
stopped an irreversible post against a live financial target and escalated
rather than proceeding. That is the guardrail working on a real transaction —
better evidence than another validation message would have been. Only
`insufficient_funds` is confirmed as a review-step business outcome on this
target; `member_not_found` (on `mc_lookup_balance`) is the second confirmed
business outcome. Two is sufficient to demonstrate the taxonomy the brief
asks for (business outcome / recoverable / hard failure / escalated).

## Chatbot escalation hang — found, fixed, verified (40-minute time-boxed fix)

Found while verifying that each of the four evidence runs "reads clearly
through the chatbot": a transfer request would open the intervention
correctly (`PENDING_HANDOFF`), and `take_control` + `hand_back` would both
return `200` and move the lease to `RESUMING` — but the engine never woke
up to finish; the lease sat at `RESUMING` forever. The underlying
capabilities and the CLI escalation path worked correctly every time; this
was specific to the chatbot's request model.

**Root cause**: `EscalationBroker.wait_for_handback()` (`cua/escalate.py`)
awaited `self._handback_event.wait()` — a notification-based wait.
`hand_back()` (called from the operator console's Flask handler thread)
calls `self._handback_event.set()` directly, not via
`loop.call_soon_threadsafe(...)`. The CLI never hit this because the whole
replay is one long-lived `asyncio.run()` for the process's entire life, so
the event loop `await`-ing the event never goes away. `chatbot.py` calls
`asyncio.run(_one_turn_reply(...))` fresh *per request*, so the broker's
`asyncio.Event` ends up bound to a transient, per-request loop; a
cross-thread `.set()` on it is not guaranteed to wake that specific loop.

**Fix chosen — polling, not cross-thread signalling (`cua/escalate.py`,
`wait_for_handback` only)**: replaced the `asyncio.wait_for(event.wait(),
timeout)` body with a bounded poll loop (50ms interval) checking
`self._handback_event.is_set()` and `self._cancelled` directly. Proposed
against the alternative (capture the waiting coroutine's event loop and
route `hand_back()`'s signal through `call_soon_threadsafe` onto that
specific loop) and chose polling because it has no cross-thread race to
get right under time pressure, is correct by construction regardless of
how many event loops have come and gone, and the added latency (up to
50ms to notice a handback) is irrelevant next to a human needing seconds
to click approve. This also fixed a *second*, pre-existing issue as a
side effect: `test_cancel_pending_wakes_waiter_with_lease_error`'s name
implied cancellation should wake the waiter promptly, but the original
code only checked `self._cancelled` *after* `.wait()` returned — which
never happened until the full timeout elapsed, since `cancel_pending()`
never sets the handback event. Polling checks `_cancelled` every cycle, so
cancellation now actually wakes the waiter within ~50ms as the test name
always claimed.

**Verified**: all 148 tests pass (`test_escalate.py`'s 12 tests run in
0.13s, confirming the poll interval doesn't slow anything down materially).
CLI regression-checked end to end after the change (full escalation cycle,
correct outputs, exit 0 — no regression). Then the actual bug: fired a
transfer request at the chatbot's `/chat` endpoint, approved it via the
operator console's real HTTP API while it waited, and got back a complete,
correct reply with a confirmation number (`CN480141`) and both resulting
balances matching the arithmetic. Chatbot-driven escalation now works.

**Not chased**: the conversation-poisoning issue (a killed/interrupted
request leaves a dangling unanswered `tool_calls` message, poisoning the
shared chatbot conversation for every request after it until restart) is
a separate, real fragility in `chatbot.py`'s single-shared-conversation
design. It is not resolved by this fix — the design still shares one
conversation across all requests, and a *different* kind of mid-flight
failure (e.g. a capability crash) would still poison it — it is just far
less likely to be hit now that escalations complete instead of hanging,
since hanging-then-killed requests were what triggered it in practice
today. Left as a known limitation per explicit instruction not to chase
it unless the first fix resolved it as a side effect, which it did not.

## HOLD-share rejection — new declared outcome on `mc_transfer` (data-only, 20-minute time-boxed)

Hand-edited `artifacts/mc_transfer/v2.1.json` (new file; `v2.0.json` left
unchanged) to add a declared `OutcomeSpec` for the HOLD-share rejection
noted as an open finding above. No code changed — `cua/replay.py`'s
`match_outcome()` and `classify_version_bump()` already supported this;
the artifact just never declared it.

**Wording verified live, not paraphrased**: signed on as `teller1`, opened
member 100234 (all shares HOLD except `100234-S0070`, confirmed via the
member-record page's Status column), and submitted a transfer from a HOLD
share. Raw HTML of the rejection:
`<font class="err">The transaction could not be validated:</font><ul><li>Source
share is HOLD and cannot be debited.</li></ul>`. Outcome `detect.contains`
uses this exact sentence.

**Outcome added**: `hold_share_debit_blocked`, `classification:
business_outcome`, `terminal: true`, appended after the existing
`insufficient_funds` entry in the `outcomes` array. `classify_version_bump()`
run programmatically against the old/new artifact confirms `"minor"`
("new outcome added" — the documented rule in `cua/schema.py`'s
`classify_version_bump` docstring) — bumped `version_minor` 0 → 1,
`recorded_by: "human_edit"`, new `recorded_at` timestamp.

**The rejection page can carry multiple `<li>` items at once — confirmed
live, not assumed.** Selecting the same HOLD share as both `from_share` and
`to_share` produced two list items together in one `<ul>`: `"Source and
destination shares must differ."` and `"Source share is HOLD and cannot be
debited."`. `match_outcome()` (`cua/replay.py`) is a linear first-match scan
over `Capability.outcomes` — **whichever declared outcome sits earlier in
the array wins**, not whichever `<li>` is more specific or more relevant to
what actually went wrong. This doesn't create an active ambiguity today
(only `hold_share_debit_blocked` and `insufficient_funds` are declared, and
their `detect.contains` strings can't both match the same page), but it is
a real precedence rule that matters the moment a `same_share_rejected`
outcome is ever added — order in the artifact's `outcomes` array, not
specificity, decides which one is reported when two fire together. Flagging
this now rather than after someone adds that second outcome and gets a
surprising precedence result.

**Verified by replay**: `cua replay --capability-id mc_transfer
--param from_share=100234-S0001 --param to_share=100234-S0070 ...` (S0001
is HOLD, S0070 is OPEN) → `status: business_outcome`, `outcome_name:
hold_share_debit_blocked`, `exit_code: 0`, evidence written to
`evidence/replay_mc_transfer_meridian_core_20260820T220913Z/`. Previously
this same input hard-failed (exit 2, "system error" via the chatbot).

**Verified through the chatbot**: same scenario via `/chat` returned a
plain-language reply ("Transfer rejected: the source share 100234-S0001 is
on hold and cannot be debited... would you like me to escalate that?"),
not a system-error message.

**Step 5, run after the time box, on member 100987 (100234 no longer has
two OPEN shares to test with): the transaction succeeded live; the
capability's own run report did not — and the cause is a separate,
pre-existing bug unrelated to the v2.1 outcome edit.**

`cua replay --capability-id mc_transfer --param from_share=100987-S0001-4
--param to_share=100987-MMKT-5 --param amount=1.00 --enable-escalation`,
approved via a real `take_control`/`hand_back` through the operator
console. Post Transfer (step 14) succeeded — the target's own confirmation
page (`step_14_failed.png`, captured by the engine's own failure-evidence
path) shows `TRANSACTION COMPLETE`, `Confirmation: CN480147`,
`100987-S0001-4: $4.00 (new balance)`, `100987-MMKT-5: $6.00 (new
balance)` — arithmetically exact ($5.00 − $1.00 / $5.00 + $1.00). But the
run's own status is `failed`, `exit_code: 2`: output extraction for
`from_share_resulting_balance` raised `no value found` immediately after,
and replay never reached `confirmation_number`/`to_share_resulting_balance`
because extraction runs in output-declaration order and stops at the first
failure.

**Root cause, found by inspecting `v2.1.json`'s output locators**:
`from_share_resulting_balance` and `to_share_resulting_balance` are each a
single-rung `label_cell` ladder anchored on a *literal* share ID string
baked in at the original discovery session — `100234-S0070` and
`100234-S0001-3` — with no fallback rung. Confirmed this is not something
`ValueRef` could already fix: `cua/schema.py`'s `ValueRef` docstring states
plainly it is "the *only* way a **Step** can carry a runtime-supplied
value" — output `ExtractSpec`/`LocatorStrategy.spec` isn't in that path at
all, so there is no existing mechanism to bind a label locator to the
caller's `from_share`/`to_share` input. Replaying against member 100987's
shares, no cell on the real confirmation page ever contains the literal
text `100234-S0070`, so the ladder's only rung misses and extraction fails
outright — correctly, per the architecture (an undeclared/unreachable
locator fails loudly rather than guessing), just not usefully for a demo
member other than the one this capability happened to be recorded against.

This is the same *bug class* already logged above for
`mc_lookup_balance`'s `balance_row_2`/`balance_row_3` (an output anchored
to a discovery-time literal that drifts), but goes one level deeper: that
fix re-anchored on the row's *share ID* instead of its *dollar amount* —
correct within one member, but still a literal, still member-specific, so
it doesn't generalize across members either. Making `mc_transfer`'s
resulting-balance outputs genuinely parameterized would need a real schema
change (letting a locator's `label_text` accept a `ValueRef`, then
resolving it in `cua/locate.py`/`cua/surface/web.py` at extraction time) —
not a data-only edit, and not attempted here; flagged for the same "what
I'd build next" list as the other structural findings.

**Net for what was asked**: the transfer itself, the escalation gate, and
the operator approval all worked correctly against a live financial
target with a fresh member/share pair under the v2.1 artifact — good
evidence the outcome edit didn't touch the write path. But I can't report
"status ok, exit 0" for this run, because it wasn't; I'm reporting exit 2
with the specific, pre-existing, unrelated cause instead of rounding up.
The chatbot leg of step 5 was not run: it would drive the identical
extraction code path and fail the same way for the same reason, so running
it would spend another live transaction to reconfirm a bug already found,
not add new information. Held for direction on whether to build the real
fix first.

**Follow-up, same session — retried with the exact recorded share pair
instead of building the ValueRef fix.** Given direction to try
`100234-S0070` → `100234-S0001-3` (the literal pair the v2.1 output
locators are anchored to) rather than fix the general parameterization gap
now: confirmed live first that HOLD only blocks the *source* (debit) side
— an OPEN→HOLD transfer reaches `CONFIRM FUNDS TRANSFER` with no
validation error, so crediting a HOLD share is allowed. `100234-S0001-3`
is HOLD today but only as a destination, so this pair is currently viable.

- **CLI, `--enable-escalation`, real `take_control`/`hand_back`**: `status:
  ok`, `exit_code: 0`, `confirmation_number: CN480162`,
  `from_share_resulting_balance: 4104` ($41.04), `to_share_resulting_balance:
  205751` ($2,057.51) — exact arithmetic against the $42.04/$2,056.51
  starting balances. Confirms the v2.1 outcome addition does not break the
  happy path when the output locators' literal anchors are actually present
  on the page.
- **Chatbot, same pair, timed end to end**: message sent 23:01:52 UTC →
  escalation reached `PENDING_HANDOFF` 23:02:04 (12s, automation driving the
  form) → approved via a real `take_control`+`hand_back` at 23:02:09 (5s,
  simulating operator reaction) → final chatbot reply 23:02:15 (6s to post,
  extract, and summarize). **23 seconds total, send to final reply.** Reply:
  `confirmation_number: CN480164`, resulting balances `$39.04` /
  `$2,059.51`, stated in plain language, not a raw JSON dump.

**A genuine, unexplained $1.00 drift, flagged and not chased further.**
The chatbot run's own confirm-page screenshot
(`evidence/replay_mc_transfer_meridian_core_20260820T230201Z/intervention_iv_8e98d789dadada30.png`)
shows starting balances of `$40.04`/`$2,058.51` at 23:02:03 — one dollar
off from where the CLI run (CN480162) left the same two shares thirty
seconds earlier (`$41.04`/`$2,057.51`), with no action of mine in between.
Both runs' own extracted outputs are internally consistent with what each
run's own confirm page showed it at the time — this isn't an extraction or
posting bug in either run. It's consistent with this being a shared,
externally-modified live demo target (matches the direct observation
already made this session that "balances and statuses have changed
repeatedly today") rather than anything in `cua`. Not investigated further
— would require tight repeated live probing to isolate, and risks more
real (if small) transactions for a question outside this capability's
scope.

## Chatbot fabricating escalations — fixed via system prompt, verified live (highest-priority pre-demo fix)

**Found**: after `mc_transfer` returned `business_outcome`/
`hold_share_debit_blocked`, the chatbot offered to escalate; saying "yes"
produced a reply claiming an escalation was submitted, a supervisor
notified to clear the hold, and offering to auto-retry once cleared. None
of it happened — no intervention existed in the operator console, no
capability clears a hold, no mechanism retries on a later condition. Root
cause: `SYSTEM_PROMPT` in `chatbot.py` told the model how to phrase a
result once a tool returned one, but never constrained it to *only*
report tool results or *only* offer real capabilities — so on a request it
had no tool for, it narrated an outcome instead of declining.

**Fix — `chatbot.py`, `SYSTEM_PROMPT` only, no other code changed.** Added
four constraints: (1) report only what a tool call actually returned,
never describe an untaken action; (2) offer only capabilities in the tool
list, no invented retries/notifications/hold-clearing; (3) the model
cannot initiate escalation on request — it's raised by the policy gate
during a run — and must say so plainly with an explanation when asked;
(4) on a business outcome, state it and stop, may suggest a different
valid invocation, never imply an unperformed state change.

**Restarted the running `chatbot.py` process** (killed PID 11416, relaunched)
since the old process's LLM client had `SYSTEM_PROMPT` baked into its
already-`.start()`-ed conversation from session startup — editing the file
alone would not have changed the live process's behavior.

**Verified live, three ways:**

1. `POST /chat` "Transfer $50 from share 100234-S0001 to 100234-S0070..."
   (S0001 is HOLD) → `"Transfer not posted. The source share is on HOLD
   and cannot be debited. Select a share that is not on hold, or clear
   the hold before retrying."` — states the outcome, no escalation offer,
   no notification claim. (The "clear the hold" phrase is the capability's
   own declared `OutcomeSpec.message`, relayed verbatim in spirit — not an
   offer to perform it.)
2. `POST /chat` "Please escalate this to a supervisor." →
   `"I can't escalate requests on demand. Escalation only happens
   automatically when a write action I run hits the policy gate and needs
   supervisor confirmation. Your transfer was rejected before that stage
   because the source share is on hold.\n\nIf you'd like, I can:\n- Try
   the transfer from a different source share...\n- Look up the member's
   shares and balances..."` — declines plainly, explains the real trigger,
   offers only real capabilities (`mc_transfer` retry, `mc_lookup_balance`).
3. Real escalation must still work — not over-constrained into declining
   genuine ones. First attempt (`100987-S0001-4` → `100987-MMKT-5`) parked
   correctly (`PENDING_HANDOFF`, real `intervention_id`, real evidence
   dir) and resumed on approval, but the chatbot's final reply — `"Transfer
   not posted. The operation failed — status 'failed', exit code 2"` —
   turned out to be *materially wrong*: the evidence screenshot
   (`evidence/replay_mc_transfer_meridian_core_20260820T230635Z/step_14_failed.png`)
   shows the transfer actually posted (`CN480165`, exact resulting
   balances). This is **not a recurrence of the fabrication bug** — the
   model accurately relayed the tool's own `status: failed`; the tool
   itself was wrong, for the same already-logged reason as before
   (`from_share_resulting_balance`/`to_share_resulting_balance` still
   anchored to member 100234's literal share IDs, so extraction crashes
   post-post on any other member, folding a successful write into an
   overall `failed` result). Re-ran with the pair known not to hit that
   bug (`100234-S0070` → `100234-S0001-3`): parked, approved, resumed,
   final reply `"Success — transfer posted... Confirmation number:
   CN480166... Resulting balances: from-share 3804, to-share 206051"` —
   clean, true, and complete. Escalation itself was never the problem in
   attempt 1; a second live confirmation ruled out any doubt.

**Not fixed, stated as next work per instruction**: this is a prompt-level
control. It constrains what the model chooses to say but nothing verifies
the claim against the actual invocation record before the reply is sent.
The structural fix — a response validator checking every claimed action
against `_agent_facing_result()`'s actual `status`/`outputs` before
returning the reply to the user — is not built. Also unresolved by this
fix: attempt 1 above shows the chatbot can still state something false
when the *tool's own result* is wrong, even with perfect prompt
compliance; that's a data-correctness problem in `mc_transfer`'s output
locators (already logged above), not a prompt problem, and a response
validator checking against a wrong invocation record wouldn't catch it
either.

## Over-broad CSS fallback rung — fixed at both resolve time and record time, verified across all five seed members

**Measured, not assumed, before touching anything.** Two read-only
investigations first: (1) confirmed `mc_lookup_balance` is reachable via
the chatbot's live `/chat` path (no auth on that route), not just the CLI
— `list_published()` exposes every approved capability with no
tenant/environment filter, and `cua.catalog.create_app()` (the one place
that *would* need auth) is never actually launched anywhere in the repo.
(2) Ran `mc_lookup_balance` against all five seed members
(`100234`/`100987`/`101555`/`102777`/`103001` — the last three found via
the target's own search-page hint text, not guessed) and hand-resolved
each output's locator ladder directly against the live page to see the
real value, not just the pass/fail exit code. Result: `share_id_row_1`,
`balance_row_1`, `share_id_row_2` on **four of five** members resolved to
the literal string `'MERIDIAN CORE\n      \xa0\xa0Member Services
Platform \xa0 v4.2.1\n      Cornerstone Financial Systems™'` — the page
header, byte-identical every time, served as if it were a share ID or a
dollar balance. `balance_row_2`/`balance_row_3` failed loud instead only
because those two happen to be single-rung `label_cell` outputs with no
`css` fallback.

**Root cause**: `WebSurface._locator_for` (`cua/surface/web.py`), `kind ==
"css"` branch, returned `self.page.locator(spec["selector"]).first`
unconditionally. `{"selector": "td"}` matches every `<td>` on the page —
measured live: 12 on the signon page, 87 on a member-detail page, 15 on
the funds-transfer form. `.first` always resolves to *something*, so a
genuine miss (higher rungs' `role_name`/`text` correctly finding nothing,
because the recorded text doesn't exist on this member's page) looked
identical to success.

**Fix, resolve time (`cua/surface/web.py`, `_locator_for`)**: count
matches via `.count()` before taking `.first`; anything other than
exactly one match is now treated as unresolved (returns `None`, same as
any other rung miss). **Threshold: exactly 1, not "a small number."**
Justified empirically, not chosen arbitrarily — surveyed every `css`
strategy across every artifact in the repo (both targets) before picking
it: every legitimate one (`#ctl00_MainContent_txtUsername`,
`#ctl00_MainContent_grdAccounts tr td:nth-child(3)`, etc.) is an ID
selector or a scoped compound selector, and every one of those already
resolves to exactly one element on its page. Only the bare single-tag
selectors used across the `mc_*` capabilities (`td`, `input`, `a`,
`select`) ever need `.first` to break a tie — and measured live, `select`
matches 2 elements on the funds-transfer form specifically (`From Share`
and `To Share` — two different fields, not two interchangeable copies of
the same one), which is direct proof that `.first` among >1 matches is
not "probably fine," it's a coin flip between genuinely different
elements. A looser threshold (e.g. "reject if >5 matches") would still
permit exactly this coin flip on smaller pages; only `== 1` has zero
ambiguity.

**Fix, record time (`cua/locate.py`, `build_ladder()`)**: added
`_BARE_TAG_SELECTOR = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*$")`; a `css`
value matching it now raises `ValueError` with an actionable message,
exactly the same pattern already used to refuse CSS as the *primary*
rung. **Did both, deliberately, not one instead of the other**: the
resolve-time check is what actually protects the ~90 `css:td`/`css:input`/
`css:a`/`css:select` strategies already sitting in artifacts tonight — a
record-time-only fix does nothing for data already on disk. The
record-time check is what stops a *future* discovery session from writing
the same landmine again, with an immediate, actionable error during
recording instead of a silent one that only detonates later against a
different member's page. Verified the record-time guard directly:
`build_ladder(role="cell", name="x", css="td")` and `css="input"` both
raise; `css="#foo"`, `css="table#x tr:nth-child(2)"`, `css=".some-class"`
all still pass through unchanged.

**Verified — full matrix, as asked:**

1. All 148 existing tests still pass, unchanged.
2. `mc_lookup_balance` against `100987`/`101555`/`102777`/`103001`: all
   four now raise `LocatorNotFoundError: no rung resolved:
   kinds=['role_name', 'text', 'css', 'coordinates']` — a clean, loud
   hard failure naming the step and every rung tried. Never header text
   again on any of the four.
3. `mc_lookup_balance` against `100234`: **now also fails** — see the
   dedicated section immediately below. Not glossed over.
4. `mc_transfer` regression check (`from_share=100234-S0001` [HOLD],
   `to_share=100234-S0070`, no escalation needed): `status:
   business_outcome`, `outcome_name: hold_share_debit_blocked`, `exit: 0`
   — byte-identical to pre-fix behavior.
5. `mc_place_hold` regression check (`operator_id=teller1`,
   `share=100234-S0070`): first attempt used a guessed `reason=fraud_review`
   and hard-failed on `select_option` — a bad param on my part, unrelated
   to the fix (the `label_cell` locator resolved the `<select>` correctly;
   the *option value* `fraud_review` doesn't exist). Retried with the
   correct code (`reason=FRAUD`, read off the step's recorded option text):
   `status: business_outcome`, `outcome_name: supervisor_override_required`,
   `exit: 0` — byte-identical to pre-fix behavior.
6. Latency: **unchanged**. Confirmed via code
   (`DEFAULT_TIMEOUT_MS = 6000`) and step-level timing data that the ~12s
   was always `role_name` (6s visibility-wait timeout, element doesn't
   exist) + `text` (another 6s timeout) *before* the `css` rung is ever
   reached — the fix only changes what happens once `css` *is* reached
   (instant reject vs. instant wrong-accept), which was never where the
   time went. Post-fix step timing on `100987`: steps 0–7 (navigate
   through the member-select click) total 763ms combined; step 8 (the
   first `read`, now a clean immediate failure) shows `duration_ms: 0` in
   the per-step log, with the ~12s appearing in the run's total instead —
   i.e. the cost lives inside the rung-timeout cascade the log doesn't
   break out per-rung, not in anything the fix touches.

**New finding, surfaced by verifying the fix, not caused by it — `100234`
itself now fails too.** Traced precisely before reporting anything:
`mc_lookup_balance`'s six recorded `read` steps (index 8–13, *separate*
from and independent of the `outputs` extraction phase that actually
populates the returned result) are:

| step | field | anchor |
|---|---|---|
| 8 | share ID, row 1 | `100234-S0001` (stable) |
| 9 | balance, row 1 | `$1,499.00` (a snapshot) |
| 10 | share ID, row 2 | `100234-S0070` (stable) |
| 11 | balance, row 2 | `$226.55` (a snapshot) |
| 12 | share ID, row 3 | `100234-S0001-3` (stable) |
| 13 | balance, row 3 | `$2,025.00` (a snapshot) |

The three share-ID reads (8/10/12) are anchored to a stable identifier and
still resolve fine. The three balance reads (9/11/13) are anchored to the
*exact dollar figure seen at the original discovery session* — and this
target's balances drift live, including from tonight's own repeated $1
transfers against `100234-S0070` specifically. Step 9 (`$1,499.00`,
`100234-S0001`) still passes only because that share is on HOLD and
hasn't moved all night. Step 11 (`$226.55`, `100234-S0070`) fails —
confirmed live, `100234-S0070` is `$26.04` right now, nowhere close.
Step 13 (`$2,025.00`, `100234-S0001-3`) never got attempted (step 11's
failure aborts the run first) but live `100234-S0001-3` is `$2,070.51` —
also long drifted, so it would almost certainly fail too if reached.

**This was already broken, silently, before tonight's fix** — the same
`css:td` fallback was propping up step 11 (and would have propped up step
13) exactly the same way it was propping up the four other members' outputs,
just not yet caught here because the drift on `100234-S0070` specifically
had to accumulate past `$226.55` before it would ever have been visible,
and nobody had cross-checked *these particular steps'* resolved value
against a live probe before tonight. The resolve-time fix didn't introduce
this — it refused to keep silently returning header text for it, the same
way it now refuses to for every other member.

**Left unresolved, deliberately, not silently patched.** Re-anchoring
steps 9/11/13 onto their row's stable share-ID text (the same
`label_cell`-with-`column_offset` technique already proven correct for
the `outputs` phase's `balance_row_2`/`balance_row_3`) is a small,
well-precedented, obviously-correct-shaped fix — but it is new scope
beyond tonight's authorization (the CSS-fallback fix and its verification
matrix), and the explicit instruction tonight was to convert silent-wrong
into loud-fail, not to make any additional member (including revalidating
`100234`'s own read steps) work. Flagged for direction rather than fixed
on the spot.

## "mc_lookup_balance signing on with wrong credentials" — traced to chatbot tool-routing, not the artifact; fixed in the prompt

**Reported**: `mc_lookup_balance` was recorded against the original
target's credentials (`demo`/`demo123`); Meridian Core needs
`teller1`/`password`.

**Diagnosed the artifact before touching anything**, per instruction.
Checked all three versions on disk:
```
$ grep -rn "demo123\|\"demo\"" artifacts/mc_lookup_balance/
(no matches — v1.0, v1.1, v1.2 all clean)
```
Step 1 (`type` into "Operator ID:"): `value` is the literal string
`"teller1"`, not a `ValueRef`, not a default — hardcoded correctly at
every version. Step 2 (`type` into "Password:"): `value` is
`{"param": "operator_password"}`, correctly sourced from the caller.
`ParamSpec.operator_password.example` is the generic placeholder
`<OPERATOR_PASSWORD>`. `chatbot.py`'s `SYSTEM_PROMPT` already stated
`operator_password = password (operator id: teller1)` correctly. The
artifact and the prompt's stated credentials were both already right, in
every place credentials could live.

`demo123` is real — it's `lookup_member_balance`'s (the original
take-home target's capability) `ParamSpec.operator_password.example`,
confirmed via `grep`. That capability is published (`approval: approved`
on all four of its versions) and exposed to the chatbot exactly like the
`mc_*` ones, with no tenant/target disambiguation anywhere in
`list_published()` or the system prompt — the same routing ambiguity
already found and reported (not yet fixed) two conversations ago, when
"Look up the balance for member 100234" first misrouted to
`lookup_member_balance` against `localhost:8080`.

**Confirmed live that this is exactly what happened again**: checked the
chatbot's own evidence directories by modification time and found
`evidence/replay_lookup_member_balance_meridian_20260821T045240Z` —
`capability_id: lookup_member_balance`, `base_url: http://localhost:8080`
— timestamped moments before the report. Restarted the chatbot (to load
current code, not a stale in-memory import from before tonight's other
fixes) and retried the identical ambiguous phrasing: it correctly reached
`mc_lookup_balance` this time. Non-deterministic tool selection, not a
credentials bug — the artifact was never the problem.

**Fix — `chatbot.py`, `SYSTEM_PROMPT` only.** Added a capability-selection
paragraph: every `mc_*`-prefixed tool targets Meridian Core and should be
preferred for member/balance/transfer/share/hold requests; plain-named
tools (`lookup_member_balance`, `open_subaccount`) are left over from a
different, unrelated demo application on a different host with separate
credentials, and must not be used for a Meridian Core request even when
the name/description sounds similar. Restarted the chatbot process again
to load the new prompt (killed and relaunched, same as every prior
`chatbot.py` edit tonight — the running process's LLM client has the
prompt baked into its `.start()`-ed conversation, so editing the file
alone doesn't change live behavior).

**Verified**: sent the identical ambiguous phrasing ("Look up the balance
for member 100234") four times total across this fix (one before
restarting, three after) — all four correctly invoked `mc_lookup_balance`,
confirmed via each run's own `evidence.json` `replay_start` metadata
(`capability_id`, `base_url`), not the chatbot's reply text alone:
```
replay_mc_lookup_balance_meridian_core_20260821T045857Z -> mc_lookup_balance https://web-sample.interface-hiring.com
replay_mc_lookup_balance_meridian_core_20260821T050100Z -> mc_lookup_balance https://web-sample.interface-hiring.com
replay_mc_lookup_balance_meridian_core_20260821T050113Z -> mc_lookup_balance https://web-sample.interface-hiring.com
replay_mc_lookup_balance_meridian_core_20260821T050118Z -> mc_lookup_balance https://web-sample.interface-hiring.com
```
`mc_transfer` (`business_outcome`/`hold_share_debit_blocked`, exit 0) and
`mc_place_hold` (`business_outcome`/`supervisor_override_required`, exit
0) both still sign on correctly — checked their own sign-on steps
directly too: `mc_transfer` step 1 is the literal `"teller1"`;
`mc_place_hold` steps 1/2 are both `ValueRef`s to `operator_id`/
`operator_password` (a genuine parameter there, by design, so it can
replay as either `teller1` or `super1`). Neither carries the defect that
was never actually in `mc_lookup_balance` either.

**Not fixed — the routing ambiguity itself is a prompt-level mitigation,
not a structural one**, same caveat as the chatbot-fabrication fix
earlier tonight. A future model, a reworded request, or a new published
capability with an overlapping name could still misroute; nothing
verifies which capability actually ran against which claim beyond what
this prompt happens to steer today. The cleaner structural fix — scoping
the chatbot's tool list to only `mc_*` capabilities at the `list_published`
call site, or unpublishing the legacy capabilities entirely — was not
done tonight; flagged as the same class of "prompt constrains, doesn't
guarantee" gap as the escalation-fabrication finding.

## Live pre-demo check — Meridian Core open shares, escalation park/screenshot verified

Checked live (`teller1`), not from memory: member `100987` currently has
three OPEN shares — `100987-S0001-4` ($5.00), `100987-MMKT-5` ($10.00),
`100987-MMKT-7` ($23.00). Didn't need to check `100234` further once
`100987` already had a usable pair.

Ran a real transfer through the chatbot (`100987-S0001-4` →
`100987-MMKT-5`, $1.00) and watched it live: correctly routed to
`mc_transfer` (routing fix above holds), reached the policy gate, parked
at `PENDING_HANDOFF`. Operator console (`/state`) shows a genuine
intervention — `intervention_id: iv_acbdc50d856cc440`, real
`evidence_dir`, `step_index: 14`,
`url: https://web-sample.interface-hiring.com/members/100987/transfer/review`.
Opened the screenshot directly (not just checked the file exists): it
renders the actual `CONFIRM FUNDS TRANSFER` page — `Member: 100987 -
Turing, Alan`, `From: 100987-S0001-4 ($5.00)`, `To: 100987-MMKT-5
($10.00)`, `Amount: $1.00`, sitting at `Post Transfer`, correctly parked
before any commit. Left it pending rather than approving it — this was a
pre-demo readiness check, not a request to complete the transfer.
