# Adapting the core to Meridian Core

This is the write-up for pointing the take-home's core — discover once,
replay deterministically, gate every action, escalate what needs a human —
at a new, unrelated target: **Meridian Core**, a server-rendered credit-union
servicing console at `web-sample.interface-hiring.com`. Every claim below is
checkable against the code or against `ADAPTATION_LOG.md`, which I kept as a
running, unedited record of every core-module change and why config alone
couldn't reach it. Where I haven't verified something, I say so.

## What adapting took

The strongest true fact first: **the capability catalog served the new
target with zero code changes.** `cua/catalog.py` — list published
capabilities, build typed tool definitions, invoke by name, strip secret
examples, keep drafts invisible — worked against Meridian Core the moment a
Meridian Core capability existed on disk. I proved this directly: I hand-ran
`create_app()` in a Python shell and hit `GET /capabilities` and
`POST /capabilities/<name>/invoke` through Flask's test client before I'd
touched a single line of `cua/catalog.py`. It had never actually been
launched by anything in the codebase before this session (no CLI subcommand,
no test called `create_app()`), so "zero code changes" is also "first time
this code path had ever been exercised" — worth knowing, not just worth
claiming.

That's the adaptation-quality argument in one sentence: the *contract layer*
(what a capability is, how it's versioned, how a caller invokes it) needed
nothing. What needed real, structural changes was the *perception layer* —
finding things on a page with no test IDs, no `<label for>` bindings, and no
accessible names on form fields; buttons and links carry real text and
resolve normally — plus a handful of genuine, if small, gaps the original
target never exercised. I did not pad this list and I did not trim it; this
is everything in `ADAPTATION_LOG.md`.

**Structural changes to existing core modules**, in the order I hit them:

- **`cua/replay.py` — policy verb now comes from the clicked element's
  label, not the generic action kind.** `policy.yaml`'s
  `require_confirmation_patterns` matches against `Action.verb`, but replay
  built `verb` from `step.action` (`"click"`, `"type"`, ...) — one of seven
  fixed literals that can never contain a button's text. No config value
  could ever match, because the field being matched structurally couldn't
  carry a label. Discovery already derived verb from the clicked element's
  name for the same gate; this made replay consistent with it.
- **`cua/catalog.py` — `invoke()` can now accept and thread through an
  escalation broker.** The API layer had no way to receive one at all —
  even the CLI only escalates when `--enable-escalation` builds a broker —
  so any capability invoked through the catalog could never escalate, only
  hard-fail. This is what made the chatbot's escalation path possible at
  all, later.
- **A new `label_cell` locator kind** (`cua/schema.py`, `cua/locate.py`,
  `cua/surface/web.py`, `cua/discover.py`) — the single largest change, and
  the clearest design lesson of this adaptation. The locator ladder's top
  rungs (`role_name`, `label`) are an accessibility-first bet: they assume a
  form field has *some* computed name, whether from real `<label for>`,
  `aria-label`, or `aria-labelledby`. That assumption is sound against the
  original target and fails completely here — Meridian Core's fields are
  `<td class="lbl">Label:</td>` next to a bare `<input>`, with none of those
  three bindings anywhere on the entire site. Real Chromium computes an
  empty accessible name for every one of them, so `role_name` resolution had
  nothing to match, no matter how the shim was written. `label_cell` is the
  fallback that assumption needed: it finds the row containing exact
  adjacent label text and acts on the paired cell, trading the accessibility
  tree for table structure as the source of truth. Getting this right took
  real debugging, not just writing it: my first version used a page-wide
  `tr.filter(has=...)`, which on a nested-table layout also matches every
  *ancestor* row (the whole page is tables-in-tables), collapsing every
  field onto the first control in document order regardless of which label
  was searched for. Anchoring on the label cell and walking up via
  `xpath=ancestor::tr[1]` fixed it. Verified directly against the live page
  before trusting it.
- **`cua/surface/observation.py` — the accessibility-tree shim now
  implements the standard rule that `<input type="submit">` takes its name
  from its `value` attribute.** Not Meridian-Core-specific — a real spec
  deviation in this repo's Playwright-1.60+ replacement for the removed
  `page.accessibility.snapshot()`. It caused a hard crash: the model,
  unable to see any name for the Sign On button, called
  `click(role="button", name="")`; the empty string is falsy in
  `build_ladder`'s `if role and name:` check, silently dropping the
  `role_name` rung, while Playwright's non-exact `get_by_role(name="")`
  matches every button as a substring — together crashing in
  `build_ladder`'s "CSS cannot be primary" guard once nothing else had
  data.
- **`cua/discover.py` — the stuck-detector now tolerates runs of
  non-mutating actions.** It declared a session stuck after three identical
  observations in a row, with no exception for `read`/`wait_for` — but
  reading several values off one static page is *supposed* to leave the
  page unchanged each time. The original demo's capability only ever had
  1–2 outputs and never hit this; Meridian Core's multi-share balance
  lookup does.
- **`cua/surface/web.py` — `select()` now matches by the `<option>`'s
  `value` attribute, not its visible label.** Meridian Core's dropdown
  option text embeds the live balance (`"100234-S0070 - Share Draft
  (Checking) ($212.54)"`) — a point-in-time snapshot that breaks the moment
  the balance changes, and can't be parameterized against a stable input
  since the identifier and the balance are baked into the same string. The
  site's own `<option value="100234-S0070">` carries the stable id.
- **`cua/surface/web.py` — `label_cell` extended to read plain data rows,
  not just fill form fields**, and later given a `column_offset` field
  (default 1, so nothing existing breaks) for wider rows. The first
  extension was needed to read a confirmation page's `"Confirmation:" /
  "CN123456"` pairs. The second was needed because I initially assumed
  every label:value row was two columns; the member-record table is four
  (Share ID | Type | Balance | Status), and the fallback landed on "Type"
  instead of "Balance" until I added the offset. Caught this one via the
  catalog's own HTTP endpoint returning the page's own header text as a
  "balance" — silently wrong, not a clean failure (more on this below).
- **A shared `label_cell_pattern()` helper** (`cua/locate.py`), normalizing
  trailing punctuation, because labels on this site are inconsistently
  punctuated — a bare share ID used as a row label can carry a trailing
  colon (`"100234-S0070:"`) that a caller-supplied value doesn't. Fixed
  once, centrally, after it broke one real output — not patched per-string,
  because the same inconsistency could hit any other label lookup on the
  site.
- **`cua/__main__.py` — `--inject-mode`'s choices widened** to add Meridian
  Core's actual fault vocabulary (`validation`, `permission`, `timeout`,
  `maintenance`, `server`) alongside the original target's
  (`notfound`, `slow`, `500`, `expired`, `dialog`, `denied`, `escalate`).
  The list was hardcoded to the old target's words even though the
  mechanism (`_maybe_inject`) just appends whatever string it's given —
  it doesn't interpret it. `--inject-mode server` was flatly rejected until
  I widened this.
- **`cua/escalate.py` — `wait_for_handback()` now polls instead of awaiting
  a cross-thread-signalled `asyncio.Event`.** Found very late, verifying
  the chatbot: escalation approvals hung forever through the chatbot
  (worked every time through the CLI). `hand_back()` calls
  `self._handback_event.set()` directly from the operator console's Flask
  thread, not via `loop.call_soon_threadsafe`. The CLI never hit this
  because one `asyncio.run()` runs the whole process; the chatbot opens a
  fresh event loop per request, so the broker's `Event` ends up bound to a
  loop that's already gone by the time a later request's `hand_back()`
  fires. Fixed with a bounded 50ms poll rather than a cross-thread-safe
  signal, specifically because polling has no race to get right under a
  hard 40-minute time box — verified end to end afterward (see escalation
  section below).
- **`policy.yaml` (config, not code, but load-bearing)** — new domain and
  route allowlist entries; `destructive_verbs`/`require_confirmation_patterns`
  reclassified so irreversible-but-legitimate actions (transfer, post,
  hold) route through confirmation instead of being blocked outright; and,
  found twice independently, two commit buttons that matched *no* existing
  pattern at all (see finding #1 below).

**What I deliberately did not build**: a `ValueRef` extension so a step
could reference a value captured by an earlier step (e.g.
`{"from_step": 4, "output": "txn_token"}`). The brief's phrasing — "a
per-transaction hidden token that must be read off the page and submitted
with the transaction" — reads like exactly this kind of step-N-produces /
step-N+M-consumes dependency. I verified against the live target instead of
assuming: recon'd all four write flows, and in every one the confirm page
echoes the token back as a hidden `<input>` in a real `<form>`. Because
replay drives an actual Playwright browser and every action operates on
live DOM state, clicking the real "Continue" → "Post ..." buttons submits
whatever token is currently on the page automatically, as part of the
browser's native form POST — no read-and-carry step needed anywhere. I
designed for the harder case the brief describes and found the browser
already handled it. This is now proven, not just argued: I ran the actual
Post Transfer step through a real escalation approval and it completed
cleanly (confirmation number `CN480122`, and again `CN480141` from the
chatbot), across two different, sequentially-minted tokens.

## The capability API / task contract

`cua/catalog.py` is the whole contract, and it predates this session:
`GET /capabilities` returns every *approved* capability's name, typed input
schema (name, type, required, sensitivity, a placeholder example — never a
secret's real value), declared outputs, and declared business outcomes.
`POST /capabilities/<name>/invoke` takes a JSON body of typed args, runs the
exact same `run_replay()` path the CLI uses, and returns a structured result
with `status` (`ok` / `business_outcome` / `failed` / `escalated`),
`outputs`, `outcome_name` when applicable, `exit_code`, and `evidence_dir`.
A caller — human, script, or the chatbot — never touches Playwright, the
policy gate, or the replay engine directly; it calls a name with typed
arguments and gets typed data back. I verified this boundary holds by
grep: `chatbot.py` imports `cua.catalog` (the capability API),
`cua.llm` (the provider-neutral model client — no separate model client
constructed), and `cua.__main__._start_operator` (a thin helper that
launches the operator console thread, reused rather than duplicated). It
never imports `cua.replay`, `cua.surface`, or `cua.policy`.

Five capabilities are registered for Meridian Core:
`mc_lookup_balance`, `mc_transfer` (v2.0 — full sign-on → select member →
share/amount/memo → review → post, escalation-gated, five typed outputs),
`mc_open_share`, `mc_update_member`, and `mc_place_hold` (v2.0, recorded as
`super1` with `operator_id` as a genuine parameter, so the same recording
can be replayed as `teller1` to get a clean permission-denied outcome
instead of a second recording). Every one is draft until a human approves
it — I approved each by hand after verifying it, exactly the gate the
schema's `approval` field is there to enforce.

## Driving this legacy UI reliably

The locator ladder (`role_name → label → text → table_cell → label_cell →
css → coordinates`) is unchanged in concept; `label_cell` is a new rung, not
a new mechanism. What changed is which rungs actually do work on this site:
`role_name` resolves buttons and links fine (their text is real, semantic
button/link text) but never resolves form fields, since none have a real
accessible name. `label_cell` picks up every field `role_name` can't reach.

Two things I want to be honest about rather than gloss over:

- **The `css: {"selector": "td"}` last-resort rung could never fail — fixed,
  measured before and after.** When every higher rung missed, this
  fallback — "any `<td>` on the page, first match" — always resolved to
  *something*, so a genuine miss looked like success. Measured precisely,
  not just hit once: run `mc_lookup_balance` against all five seed members
  (`100234`, `100987`, `101555`, `102777`, `103001`), and **four of five**
  came back with `share_id_row_1`, `balance_row_1`, and `share_id_row_2`
  all silently equal to the page header — `'MERIDIAN CORE ... Cornerstone
  Financial Systems™'` — served as if it were a share ID or a dollar
  balance. Only member `100234`, the one this capability was recorded
  against, came back correct. Same root cause was also a latency cost:
  each of the six `read` steps burned ~12s on every non-`100234` member
  (two higher rungs — `role_name`, `text` — each exhausting their own 6s
  visibility-wait timeout before falling through) versus sub-500ms for
  every click/type/navigate step.

  **Fixed both ends of the lifecycle.** Resolve time
  (`cua/surface/web.py`, `WebSurface._locator_for`): a `css` rung now
  counts its matches before taking `.first`; anything other than exactly
  one match is treated as a miss, not a match. Not "a small number" —
  exactly 1, because a locator rung's whole job is to identify *one*
  specific element, and every legitimate `css` strategy already recorded
  in this repo (ID selectors, scoped compound selectors like `#id
  tr:nth-child(3)`) already resolves to exactly one; only the over-broad
  bare-tag selectors (`td`: 12–87 matches per page measured; `select`: 2
  matches on the funds-transfer form, where the two matches are the *From*
  and *To* fields — genuinely different elements, not safely
  interchangeable) ever needed `.first` to break a tie. Record time
  (`cua/locate.py`, `build_ladder()`): a bare single-tag selector with no
  id/class/attribute/combinator qualifier is now rejected the same way CSS
  is already refused as *primary* — stops a future recording from writing
  the same landmine, which the resolve-time fix alone wouldn't. Did both,
  not one: resolve-time is the only thing that protects the ~90 `css:td`/
  `css:input`/`css:a` strategies already sitting in artifacts tonight;
  record-time is what stops a new one from being written.

  Verified: all 148 tests still pass. `mc_lookup_balance` against
  `100987`/`101555`/`102777`/`103001` now returns a clean, loud
  `LocatorNotFoundError` naming the step and the rungs that were tried,
  never header text again. `mc_transfer` and `mc_place_hold` regression-
  checked post-fix — unchanged (`hold_share_debit_blocked` and
  `supervisor_override_required` business outcomes fire exactly as
  before). Read latency is unchanged — confirmed the ~12s was always
  `role_name`+`text` each timing out (6s × 2) *before* the css rung is
  ever reached, not the css rung itself, so tightening it couldn't have
  changed that cost either way.

  **One thing this fix did not paper over, and I'm not claiming it did**:
  the same verification pass found `100234` itself now fails too — a
  *separate*, previously-undetected bug in three of the six recorded
  `read` steps (not the `outputs` extraction phase, which already anchors
  `balance_row_2`/`balance_row_3` on the stable share ID). Those three
  steps are anchored to the exact dollar figure seen at the original
  discovery session (`$1,499.00`, `$226.55`, `$2,025.00`) — and on a
  target whose balances drift live, that anchor rots. One of the three
  (`$1,499.00`, the HOLD share) happened to still match tonight only
  because that specific share hasn't moved all night; the other two no
  longer match anything on the live page (confirmed live: `100234-S0070`
  is `$26.04` now, nowhere near `$226.55`). This was *already* silently
  returning header text on `100234` itself whenever that balance drifted
  — the new resolve-time check just refuses to keep hiding it. Left
  unresolved, flagged, not silently patched — re-anchoring those three
  steps is a real edit beyond tonight's authorized scope.
- **`select()` matches by DOM `value`, not visible label**, precisely
  because visible labels here embed live data. I didn't verify whether any
  *other* target's dropdowns would regress under this change — I don't
  have one to test against, and I'm not going to claim I checked something
  I didn't.

## Runtime and exceptional states

The most interesting thing I learned adapting this core: **over-permissive
matchers convert detectable conditions into wrong answers, and it happened
three separate times, same shape each time.** (1) The
`css: {"selector": "td"}` fallback rung (above, under "Driving this legacy
UI reliably") returned page-header text as a balance instead of failing.
(2) A Place Hold outcome detector matched
"SUPERVISOR OVERRIDE REQUIRED," but that exact text also appears as a
warning banner on the form page shown to *every* operator, supervisor
included — it would have falsely rejected `super1` too, before I caught it
by actually running the supervisor path and watching it fail for the wrong
reason. (3) The discovery compilation prompt required declaring a success
outcome, and for a capability that ends in silent reads with no further page
transition, the model picked a static table heading that was already true
on page load — the recorded "success" detector fired before any of the six
reads it was supposed to follow ever ran. All three are the same failure
shape: a detector that's too broad matches something true all the time, and
a real distinction (found vs. not found, teller vs. supervisor, before vs.
after extraction) gets silently erased.

A related but distinct finding, surfaced adding the HOLD-share outcome
below: outcome resolution (`match_outcome()` in `cua/replay.py`) is a
first-match-in-declaration-order scan over `Capability.outcomes` —
deterministic, but positional rather than semantic. The rejection page can
render multiple `<li>` errors at once (confirmed live: selecting the same
HOLD share as both source and destination produces "Source and destination
shares must differ." and "Source share is HOLD and cannot be debited."
together in one `<ul>`), so the moment a second outcome is declared whose
detector could co-match a page the first outcome also matches, which one
gets reported depends on where it sits in the array, not which is more
specific to what's actually wrong. Not ambiguous today — only one outcome's
detector text can currently match a HOLD page — but one added outcome away
from being so. Whether this is "the same finding" as the over-permissive-
matcher pattern above: only in one sentence, not more — both are the
detection layer being quietly wrong instead of loudly failing, but the
mechanisms are different (a matcher too broad vs. an arbiter that's too
simple), so I'm keeping them as two findings rather than forcing them into
one.

I produced and committed four runs, each confirmed rendering in the
dashboard and (except where noted) reading clearly through the chatbot:

1. **Clean success** — `mc_transfer`, full escalation-approved post,
   `confirmation_number: CN480122`, resulting balances arithmetically
   correct, exit 0.
2. **Business outcome** — `?inject=notfound` on the member-detail page.
   This needed a real fix, not just a test: the injected page says
   "RECORD NOT FOUND — The requested member record could not be located on
   this host," genuinely different wording from an organic empty search
   ("No member records matched your search."), so it needed its own
   declared outcome (`member_record_not_found`). Result: `business_outcome`,
   exit 0, not a crash.
3. **Hard failure** — `?inject=server` produces a real "APPLICATION ERROR,
   Reference: ERR-E138DBB1" page with no matching outcome; replay reports
   it with step index, what was expected, what was observed, and a
   screenshot.
4. **Escalation** — the real `require_confirmation` gate on Post Transfer,
   parked with full context (intervention id, reason, a real screenshot),
   left to time out unapproved, exactly as designed.

I also tried `?inject=maintenance` and I'm reporting honestly: **it does
not recover.** `recovery_events` comes back empty and the run ends in a
clean hard failure. None of today's recorded capabilities declare an
`on_error` rule for this interstitial — the engine did exactly what it was
told, which is nothing, because nothing was declared. This is a real gap in
capability data, not a claim I can round up to "the recovery path works."

I also went looking for HOLD-share and same-share transfer rejections,
since the brief implies both are enforced. Here's what I actually found,
worded precisely: **HOLD-share and same-share transfers were rejected at
the review step earlier in the session against member 100234, but did not
reproduce against 100987 — both proceeded to the policy-gated post step and
escalated.** Whether this is per-member state, share-pair dependent, or
something else, I did not investigate further given the time budget — I
tried twice, confirmed the share really was marked `[HOLD]` on the record
page both times, and stopped rather than keep guessing. The more useful
result sits right alongside the inconclusive one: in both cases the policy
gate stopped an irreversible post against a live financial target and
escalated rather than proceeding. That's the guardrail actually working on
a real transaction, which is better evidence than a validation message
would have been. Two business outcomes are fully confirmed on this target
(`insufficient_funds`, `member_not_found`) — enough to demonstrate the
taxonomy the brief asks for, not padded into three or four to look more
complete than the evidence supports.

**Update, same session:** a third — `hold_share_debit_blocked` on
`mc_transfer` (v2.1, minor bump) — is now declared and confirmed the same
way: wording verified live (`"Source share is HOLD and cannot be
debited."`), replay returns `business_outcome`/exit 0 instead of the prior
hard failure, and the chatbot states it in plain language instead of
"system error." See `ADAPTATION_LOG.md` for the fix, the version-bump
justification, and a precedence finding it surfaced (the rejection page can
show multiple errors at once; `match_outcome()` reports whichever declared
outcome is listed first in the artifact, not whichever is most specific).

## How safety, evidence, and escalation survived

**Policy gate**: every action still goes through it, in the API and the
chatbot exactly as in the CLI — I didn't add a second path around it
anywhere; `chatbot.py` and `dashboard.py` both only ever call into
`cua.catalog`. But building for this target exposed a real, structural
weakness in the gate itself, not just a missing pattern:

**Finding — the policy gate fails open on unrecognized actions.** Twice,
independently, a real commit button matched *no* pattern in
`require_confirmation_patterns` at all: Open New Share's confirm button is
labeled "Open Share," and Place Hold's is "Apply Hold" — neither contains
"Post," "Confirm," "Submit," "Save," or "Approve." Until I found and added
each one by hand, both irreversible actions would have sailed through
replay as plain `allow`. An allowlist of verb patterns is a denylist in
disguise: anything not on the list gets through, not stopped. The correct
fix is inverting the default — anything that reaches a
confirmation-shaped step (a click following a review/confirm screen, or
more generally any write-committing action) should be gated *unless*
explicitly marked safe, not gated only when it happens to match a string
someone thought to add. I'm stating this as next work, not fixing it now:
inverting a default like this needs its own careful pass across every
existing capability to make sure nothing legitimate gets newly blocked.

**Finding — over-permissive matchers convert detectable conditions into
wrong answers.** This is the most interesting thing I learned adapting this
core, and it opens the "Runtime and exceptional states" section above rather
than being repeated in full here.

**Finding — behaviour lives in the artifact, so omissions degrade to clean
failures rather than wrong answers, and that's the safe direction.** An
undeclared business outcome doesn't get invented at replay time — it just
becomes a hard failure, reported with step index and observed text.
`?inject=maintenance` isn't secretly handled by some fallback — it fails
cleanly because no capability declares a recovery rule for it. The engine
never guesses on my behalf. I'd rather report this plainly than round it up
into "the recovery system works" when today's five capabilities simply
don't exercise it.

**Finding — hardcoded vocabularies from the original target did not
survive the move**, in two places: `--inject-mode`'s argparse choices, and
`policy.yaml`'s original verb-pattern set. Both assumed the old app's
words; neither carried over automatically, and both needed a config or
near-config change once Meridian Core's actual vocabulary showed up.

**Evidence**: every replay run still writes its structured log and
screenshots to `evidence/` exactly as before — I didn't touch
`cua/evidence.py`'s format at all, per explicit instruction, and where the
dashboard needed something the current format doesn't record (actual
output *values* — `_result_summary()` in `cua/replay.py` omits `outputs`
entirely; only stdout ever sees them), I said so in the dashboard's own UI
rather than quietly work around it or change the writer.

**Escalation**: works end to end through the CLI (proven repeatedly, across
Transfer, Open Share, Update Member Info, and Place Hold) and, after the
fix described above, through the chatbot too — verified live: fired a real
transfer at the chatbot, approved it through the operator console's actual
HTTP API while it waited, got back a complete reply with a confirmation
number. One related fragility I found and did not fix, on instruction: if a
request is killed or crashes mid-tool-call, the chatbot's single shared LLM
conversation is left with a dangling, unanswered tool call, and every
request after it fails until the process restarts. This is a real design
weakness in `chatbot.py`'s single-conversation model, not resolved by the
escalation fix — just far less likely to be triggered now that escalations
complete instead of hanging.

**Finding — the chatbot fabricated an escalation.** Verified live: after
`mc_transfer` returned `business_outcome`/`hold_share_debit_blocked` and the
chatbot offered to escalate, saying "yes" produced "Escalation submitted: A
supervisor has been notified to review and clear the hold on share
100234-S0001... Would you like me to automatically retry the $50 transfer
once the hold is lifted?" None of that was true. No intervention was
created — the operator console showed nothing. No supervisor was notified.
There is no capability that clears a hold, and no mechanism that retries a
transfer when a condition changes later. The model invented an action, a
notification, and a follow-on capability that doesn't exist, about a
financial transaction, because the system prompt never constrained it to
its actual tool set — it knew escalation exists as a concept in this
architecture and narrated one on request instead of saying it couldn't.

The guardrails hold at the action layer: the policy gate, the capability
API, and replay cannot be bypassed by the conversational layer, and
nothing the chatbot said caused any state change. But the conversational
layer can still make false claims about what happened. An agent that
fabricates a supervisor notification about a financial transaction is a
real risk even when it has no power to act, because a user believes it.

**Fixed via the system prompt** (`chatbot.py`, `SYSTEM_PROMPT`) — a
prompt-level control, not a structural one: the model may report only what
a tool call actually returned, may offer only capabilities present in its
tool list, cannot initiate an escalation on request (escalation is raised
by the policy gate during a run, never on demand — the model must say so
plainly and explain what would actually trigger one), and on a business
outcome states it and stops, without implying any state change it can't
perform.

Verified live, three ways, after restarting the chatbot to pick up the new
prompt: (1) a transfer from a HOLD share now states the rejection plainly
with no escalation offer; (2) explicitly asking it to "escalate this to a
supervisor" now gets "I can't escalate requests on demand... escalation
only happens automatically when a write action I run hits the policy gate
and needs supervisor confirmation," plus an offer limited to real
capabilities (retry with a different share, look up balances) — no
fabrication; (3) a real escalation — transfer between two open shares
hitting the policy gate — still parks with a genuine intervention id and
evidence dir in the operator console, and still completes normally on
approval (confirmation `CN480166`, exact resulting balances). The fix
didn't over-constrain the model into refusing genuine escalations, only
fabricated ones.

**Next work, stated plainly**: constraining the model to its tool set is
necessary but insufficient — it's a prompt-level control, and prompts can
be worked around or drift as models change. The structural version would
be a response validator that checks every claim the reply makes about an
action taken against the actual invocation record before the reply is
sent, rather than trusting the model to self-constrain. Not built today.

**Finding — the chatbot non-deterministically picked the wrong capability,
and it looked like a credentials bug from the outside.** Reported as
"`mc_lookup_balance` is signing on with the wrong credentials — it was
recorded against the original target (`demo`/`demo123`); Meridian Core
uses `teller1`/`password`." Investigated the artifact first, not the
symptom: every version of `mc_lookup_balance` on disk (`v1.0`–`v1.2`) has
the operator ID hardcoded correctly as the literal `"teller1"` in its
sign-on step, and `operator_password` is a `ValueRef` correctly sourced
from the caller — zero occurrences of `demo`/`demo123` anywhere in the
artifact, and the chatbot's own system prompt already stated the correct
credentials. `demo123` belongs to a different, unrelated capability
(`lookup_member_balance`, the original take-home target, a different host
entirely) that's *also* published and exposed to the chatbot with no
disambiguation from the `mc_*` ones. Confirmed live, not guessed: the same
chatbot session had already invoked `lookup_member_balance` against
`localhost:8080` moments before the report — the wrong capability, wrong
target, and (naturally) credentials that don't apply to Meridian Core.
Retried the identical ambiguous phrasing that triggered it and got a
different (correct) capability the very next time — non-deterministic,
not reliably reproducible, and the artifact was never actually at fault.

**Fixed at the layer that was actually wrong** (`chatbot.py`,
`SYSTEM_PROMPT`, not the artifact — hand-editing a correct artifact
because a symptom pointed elsewhere would have been the wrong fix): added
explicit capability-selection guidance — every `mc_*`-prefixed tool
targets Meridian Core and should be preferred for any member/balance/
transfer/share/hold request; plain-named tools left over from the
original demo target (`lookup_member_balance`, `open_subaccount`) target
an unrelated host with separate credentials and must not be used for a
Meridian Core request even when the wording sounds similar. Verified: the
same ambiguous phrasing that previously misrouted now correctly reaches
`mc_lookup_balance` 4 times in a row (confirmed via each run's own
`evidence.json` `capability_id`/`base_url`, not just the reply text).
`mc_transfer` and `mc_place_hold` sign on correctly too — checked their
own sign-on steps directly; neither carries the same defect.

**Finding — the chatbot reported "generic system error" on an escalation
that had actually been approved, because it was throwing away the only
fields that explained why.** Reported as an escalation-handling bug: a
run parked, was approved five seconds later, and the chatbot still said
failure — not a timeout. Diagnosed before touching anything, per
instruction, with a faithful live reproduction (same `invoke()` call
`chatbot.py` makes, own operator console so the live demo wasn't
disturbed): **waiting behavior was already correct** — `invoke()` blocked
20.7 seconds wall-clock through park → approval → resume → continued
execution, all inside the one call; it never returns early on
`"escalated"`. The actual raw result was `status: "failed"`, with a real
`resumed` intervention in it — the approval had worked; a separate,
already-known bug (`mc_transfer`'s resulting-balance outputs still
anchored to member `100234`'s literal share IDs) threw immediately
afterward. The bug was `chatbot.py`'s `_agent_facing_result()`: its field
whitelist kept only `status`/`outputs`/`outcome_name`/`message`/
`exit_code` — for this result that's `{"status": "failed",
"outcome_name": null, "exit_code": 2}` and nothing else, so the model
wasn't being vague on purpose, it had nothing else to say.

**Fixed by widening the whitelist**, not by inventing new logic: forward
`intervention_id`, `reason`, `resumable`, `interventions`, `error`,
`observed`, `expected`, `step_index`, `action` when the result actually
has them. Added prompt guidance for how to read the two shapes this
unlocks: a genuine `escalated` status (the approval window really did
close — name the `intervention_id`, point at the operator console) versus
a `resumed`-then-`failed` result (the approval worked; state the real
error instead of a generic one). Verified against the exact reported
scenario: the chatbot now names the approving operator, the real
intervention id, and the exact underlying error — and correctly declines
to guess whether the transfer actually posted, rather than fabricating
either a confirmation or a definitive failure claim the tool result
doesn't support. All 148 tests still pass.

## Dashboard and chatbot

Both are server-rendered, no framework, no build step, no database — the
dashboard reads directly from `artifacts/` and `evidence/` on disk; the
chatbot's `/chat` handler calls `cua.catalog.invoke()` and nothing lower.
The dashboard's three views (capability catalog with drafts and approved
visibly distinct; run history newest-first with parsed status/duration;
per-run detail with the step timeline, matched-vs-recorded rung and drift,
recovery events, and links to the real screenshots/logs already on disk)
were built and verified against real runs, not sample data — every claim in
the "runtime and exceptional states" section above was confirmed rendering
correctly in the dashboard before I reported it done.

## Cuts and next steps

**What was actually left out, and why**: the policy gate's fail-open default
on unrecognized actions was found and diagnosed but not fixed; the
HOLD-share/same-share inconsistency between members was found but not
investigated further; the chatbot's conversation-poisoning fragility was
found but not fixed. All three are named findings above, each with a
concrete proposed fix I didn't have time to build and verify properly under
the session's time budget — not things I judged unimportant. (The over-broad
CSS fallback rung was in this list too; it's now built and verified instead
— see "Driving this legacy UI reliably" above.)

**New, found while verifying the CSS fix, not yet fixed**: three of
`mc_lookup_balance`'s six recorded `read` steps are anchored to the exact
dollar figure seen at the original discovery session rather than a stable
identifier, so they rot as `100234`'s own balances drift — the CSS fix
correctly stopped hiding this, which means `mc_lookup_balance` currently
hard-fails even on `100234` until these three steps are re-anchored.
Flagged, not silently patched — see `ADAPTATION_LOG.md`.

**Not a cut**: Open New Share and Update Member Information were briefly
descoped, then explicitly brought back in before any of the above work
started; both are recorded, hazard-checked, and verified exactly like the
other three capabilities. Noting this only because the earlier scope
decision is still visible in `ADAPTATION_LOG.md` and I don't want it read as
something left undone.

**What I'd build next, in the order I'd do it**: re-anchor `mc_lookup_balance`'s
three dollar-figure-anchored `read` steps onto the stable share ID (same
technique already used for `balance_row_2`/`balance_row_3`'s output
locators), since that's what's blocking `100234` itself right now; invert
the policy gate's default for confirmation-shaped actions (closes a whole
*class* of "found it twice, there could be a third" gaps rather than one
instance); parameterize `mc_lookup_balance`'s and `mc_transfer`'s output
locators so they work against any member, not just the one they were
recorded against (a real schema change — a locator's `label_text`
accepting a `ValueRef` — not attempted tonight); give the chatbot
per-request (or recoverable) conversation state instead of one shared,
unrecoverable one; and actually chase down why HOLD-share rejection
reproduced for one member and not another, since right now I only have two
data points and an honest "didn't investigate further."
