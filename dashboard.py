"""Read-only dashboard over the artifact registry and evidence on disk.

Server-rendered, no frontend framework, no build step, no database. Every
view reads directly from ``artifacts/`` (the capability registry) and
``evidence/`` (structured logs + screenshots already written by discovery
and replay) — nothing here writes to either. If a view can't get something
from the existing evidence format, it says so rather than the writer being
changed to produce it.

Redaction already happened at write time (cua/evidence.py redacts
pii/secret-sensitivity values before anything touches disk); this file only
ever renders what's already on disk, so nothing here needs its own
redaction pass — but it also never reaches past the evidence file to fetch
a raw value from anywhere else.

Run:
    .venv/bin/python dashboard.py
Then open http://127.0.0.1:5051/
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, render_template_string, send_file

from cua.schema import Capability

REPO_ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
EVIDENCE_DIR = REPO_ROOT / "evidence"


# ---- Data loading (read-only) --------------------------------------------


def load_catalog() -> list[dict[str, Any]]:
    """Every capability version on disk, draft and approved alike."""

    entries: list[dict[str, Any]] = []
    if not ARTIFACTS_DIR.exists():
        return entries
    for cap_dir in sorted(ARTIFACTS_DIR.iterdir()):
        if not cap_dir.is_dir():
            continue
        for f in sorted(cap_dir.glob("v*.json")):
            try:
                data = json.loads(f.read_text())
                cap = Capability.model_validate(data)
            except Exception as exc:  # noqa: BLE001
                entries.append(
                    {
                        "capability_id": cap_dir.name,
                        "version": f.stem,
                        "title": f"(failed to load: {exc})",
                        "approval": "error",
                        "inputs": [],
                        "outputs": [],
                        "outcomes": [],
                        "recorded_by": None,
                    }
                )
                continue
            entries.append(
                {
                    "capability_id": cap.capability_id,
                    "version": f"{cap.version_major}.{cap.version_minor}",
                    "title": cap.title,
                    "approval": cap.approval,
                    "inputs": [
                        (p.name, p.type, p.sensitivity, p.required)
                        for p in cap.inputs
                    ],
                    "outputs": [(o.name, o.type) for o in cap.outputs],
                    "outcomes": [(o.name, o.classification) for o in cap.outcomes],
                    "recorded_by": cap.recorded_by,
                }
            )
    return entries


def _read_evidence_log(run_dir: Path) -> list[dict[str, Any]] | None:
    ev_file = run_dir / "evidence.json"
    if not ev_file.exists():
        return None
    try:
        return json.loads(ev_file.read_text())
    except Exception:  # noqa: BLE001
        return None


def _run_kind(name: str) -> str:
    if name.startswith("discovery_"):
        return "discovery"
    if name.startswith("replay_"):
        return "replay"
    return "other"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def summarize_run(run_dir: Path) -> dict[str, Any] | None:
    """One row for the run-history view. Returns None if unreadable."""

    log = _read_evidence_log(run_dir)
    if log is None:
        return None
    kind = _run_kind(run_dir.name)
    capability_id = "?"
    status = "unknown"
    start = log[0].get("timestamp") if log else None
    end = log[-1].get("timestamp") if log else None

    for entry in log:
        meta = entry.get("metadata") or {}
        if entry.get("message") in ("session_start", "replay_start") and meta.get(
            "capability_id"
        ):
            capability_id = meta["capability_id"]
            break

    if kind == "replay":
        for entry in reversed(log):
            if entry.get("message") == "replay_end":
                meta = entry.get("metadata") or {}
                status = meta.get("status", "unknown")
                break
    elif kind == "discovery":
        # Discovery's evidence format has no single canonical "final status"
        # field the way replay's replay_end does: a clean finish logs
        # "finish" then "artifact_written"; anything else (stuck/timeout/
        # max_steps/error) only logs "skip_compile" with the status in its
        # metadata. Best-effort parse; genuinely missing for a crash with no
        # log entry at all.
        if log and log[-1].get("message") == "artifact_written":
            status = "finished"
        else:
            for entry in reversed(log):
                if entry.get("message") == "skip_compile":
                    status = (entry.get("metadata") or {}).get("status", "unknown")
                    break

    duration_s = None
    t0, t1 = _parse_ts(start), _parse_ts(end)
    if t0 and t1:
        duration_s = round((t1 - t0).total_seconds(), 1)

    return {
        "dir": run_dir.name,
        "kind": kind,
        "capability_id": capability_id,
        "status": status,
        "start": start,
        "duration_s": duration_s,
    }


def load_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not EVIDENCE_DIR.exists():
        return runs
    for d in EVIDENCE_DIR.iterdir():
        if not d.is_dir():
            continue
        row = summarize_run(d)
        if row is not None:
            runs.append(row)
    runs.sort(key=lambda r: r["start"] or "", reverse=True)
    return runs


def load_run_detail(run_name: str) -> dict[str, Any] | None:
    run_dir = EVIDENCE_DIR / run_name
    if not run_dir.is_dir():
        return None
    log = _read_evidence_log(run_dir)
    if log is None:
        return None
    kind = _run_kind(run_name)

    steps: list[dict[str, Any]] = []
    recovery_events: list[dict[str, Any]] = []
    inputs_declared: list[str] = []
    final_status = "unknown"
    final_message = None

    for entry in log:
        meta = entry.get("metadata") or {}
        msg = entry.get("message")
        if msg in ("session_start", "replay_start"):
            inputs_declared = meta.get("param_names") or meta.get("params") or []
        if msg == "step_ok":
            steps.append(
                {
                    "step_index": entry.get("step_index"),
                    "action": entry.get("action"),
                    "status": "ok",
                    "matched_rung": meta.get("matched_rung"),
                    "recorded_rung": meta.get("recorded_rung"),
                    "drifted": (
                        meta.get("matched_rung") is not None
                        and meta.get("recorded_rung") is not None
                        and meta.get("matched_rung") != meta.get("recorded_rung")
                    ),
                    "checkpoint": meta.get("checkpoint"),
                    "duration_ms": meta.get("duration_ms"),
                    "recovered": meta.get("recovered"),
                    "error": None,
                }
            )
        elif msg in (
            "replay_hard_failure",
            "tool_execution_failed",
            "policy_blocked",
        ):
            steps.append(
                {
                    "step_index": entry.get("step_index"),
                    "action": entry.get("action"),
                    "status": "failed",
                    "matched_rung": None,
                    "recorded_rung": None,
                    "drifted": False,
                    "checkpoint": None,
                    "duration_ms": None,
                    "recovered": None,
                    "error": entry.get("error") or meta.get("observed"),
                }
            )
        elif msg == "recovery_triggered":
            recovery_events.append(
                {
                    "step_index": entry.get("step_index"),
                    "rule": meta.get("rule"),
                    "trigger": meta.get("trigger"),
                    "attempt": meta.get("attempt"),
                    "success": meta.get("success"),
                }
            )
        elif msg == "replay_end":
            final_status = meta.get("status", "unknown")
            result = meta.get("result") or {}
            final_message = result

    screenshots = sorted(p.name for p in run_dir.glob("*.png"))

    return {
        "dir": run_name,
        "kind": kind,
        "inputs_declared": inputs_declared,
        "steps": steps,
        "recovery_events": recovery_events,
        "final_status": final_status,
        "final_message": final_message,
        "screenshots": screenshots,
        "log": log,
    }


# ---- Templates -------------------------------------------------------------

_BASE_CSS = """
body { font-family: system-ui, sans-serif; margin: 1.5rem; color: #1a1a1a; }
h1 { font-size: 1.3rem; }
h2 { font-size: 1.05rem; margin-top: 1.5rem; }
nav a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; margin: 0.75rem 0; }
th, td { border: 1px solid #ccc; padding: 0.35rem 0.6rem; text-align: left; font-size: 0.9rem; vertical-align: top; }
th { background: #eee; }
.draft { color: #a05a00; font-weight: bold; }
.approved { color: #0a6b1e; font-weight: bold; }
.status-ok, .status-finished, .status-ReplaySuccess { color: #0a6b1e; font-weight: bold; }
.status-business_outcome, .status-ReplayBusinessOutcome { color: #0a5b9b; font-weight: bold; }
.status-escalated, .status-ReplayEscalated { color: #a05a00; font-weight: bold; }
.status-failed, .status-timeout, .status-stuck, .status-max_steps, .status-ReplayHardFailure, .status-crashed { color: #a00000; font-weight: bold; }
.drift-yes { color: #a05a00; font-weight: bold; }
.drift-no { color: #666; }
code { background: #f3f3f3; padding: 0.1rem 0.3rem; }
.small { color: #666; font-size: 0.85rem; }
"""

_NAV = """
<nav><a href="/">Catalog</a> <a href="/runs">Run history</a></nav>
"""

_CATALOG_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Catalog</title>
<style>{{ css }}</style></head><body>
""" + _NAV + """
<h1>Capability catalog (draft + approved)</h1>
<table>
<tr><th>Capability</th><th>Version</th><th>Title</th><th>Approval</th>
    <th>Inputs</th><th>Outputs</th><th>Outcomes</th><th>Recorded by</th></tr>
{% for c in caps %}
<tr>
  <td>{{ c.capability_id }}</td>
  <td>{{ c.version }}</td>
  <td>{{ c.title }}</td>
  <td class="{{ 'approved' if c.approval == 'approved' else 'draft' }}">{{ c.approval }}</td>
  <td>{% for n,t,s,r in c.inputs %}<code>{{ n }}</code>:{{ t }}{% if s in ('pii','secret') %} <span class="small">({{ s }})</span>{% endif %}{% if not r %} <span class="small">optional</span>{% endif %}<br>{% endfor %}</td>
  <td>{% for n,t in c.outputs %}<code>{{ n }}</code>:{{ t }}<br>{% endfor %}</td>
  <td>{% for n,cls in c.outcomes %}<code>{{ n }}</code> <span class="small">({{ cls }})</span><br>{% endfor %}</td>
  <td class="small">{{ c.recorded_by or '' }}</td>
</tr>
{% endfor %}
</table>
</body></html>
"""

_RUNS_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Run history</title>
<style>{{ css }}</style></head><body>
""" + _NAV + """
<h1>Run history (newest first)</h1>
<table>
<tr><th>Start</th><th>Kind</th><th>Capability</th><th>Status</th><th>Duration (s)</th><th></th></tr>
{% for r in runs %}
<tr>
  <td>{{ r.start or '?' }}</td>
  <td>{{ r.kind }}</td>
  <td>{{ r.capability_id }}</td>
  <td class="status-{{ r.status }}">{{ r.status }}</td>
  <td>{{ r.duration_s if r.duration_s is not none else '?' }}</td>
  <td><a href="/runs/{{ r.dir }}">detail</a></td>
</tr>
{% endfor %}
</table>
{% if not runs %}<p>No runs found under evidence/.</p>{% endif %}
</body></html>
"""

_RUN_DETAIL_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><title>Run detail</title>
<style>{{ css }}</style></head><body>
""" + _NAV + """
<h1>Run: {{ run.dir }}</h1>
<p class="small">kind={{ run.kind }} | final status: <span class="status-{{ run.final_status }}">{{ run.final_status }}</span></p>

<h2>Declared inputs</h2>
<p class="small">Names only — values are redacted before anything reaches disk.</p>
<p>{% for n in run.inputs_declared %}<code>{{ n }}</code> {% endfor %}</p>

{% if run.final_message %}
<h2>Result summary</h2>
<pre>{{ run.final_message | tojson(indent=2) }}</pre>
<p class="small">Note: replay's evidence.json does not persist extracted output
values (cua/replay.py's _result_summary omits `outputs`) — only step
telemetry and this summary. Output values are only ever printed to stdout
by the CLI, not written to disk. Not fixed here per "do not change the
evidence format" — flagged instead.</p>
{% endif %}

<h2>Step timeline</h2>
<table>
<tr><th>#</th><th>Action</th><th>Status</th><th>Matched rung</th><th>Recorded rung</th><th>Drift?</th><th>Checkpoint</th><th>Duration (ms)</th><th>Error</th></tr>
{% for s in run.steps %}
<tr>
  <td>{{ s.step_index }}</td>
  <td>{{ s.action }}</td>
  <td>{{ s.status }}</td>
  <td>{{ s.matched_rung or '' }}</td>
  <td>{{ s.recorded_rung or '' }}</td>
  <td class="{{ 'drift-yes' if s.drifted else 'drift-no' }}">{{ 'yes' if s.drifted else 'no' }}</td>
  <td>{{ s.checkpoint or '' }}</td>
  <td>{{ s.duration_ms if s.duration_ms is not none else '' }}</td>
  <td class="small">{{ s.error or '' }}</td>
</tr>
{% endfor %}
</table>

<h2>Recovery events</h2>
{% if run.recovery_events %}
<table>
<tr><th>Step</th><th>Rule</th><th>Trigger</th><th>Attempt</th><th>Success</th></tr>
{% for e in run.recovery_events %}
<tr><td>{{ e.step_index }}</td><td>{{ e.rule }}</td><td>{{ e.trigger }}</td><td>{{ e.attempt }}</td><td>{{ e.success }}</td></tr>
{% endfor %}
</table>
{% else %}<p class="small">None.</p>{% endif %}

<h2>Screenshots</h2>
{% if run.screenshots %}
<ul>{% for s in run.screenshots %}<li><a href="/runs/{{ run.dir }}/file/{{ s }}">{{ s }}</a></li>{% endfor %}</ul>
{% else %}<p class="small">None.</p>{% endif %}

<h2>Full log</h2>
<p><a href="/runs/{{ run.dir }}/file/evidence.json">evidence.json</a></p>
</body></html>
"""


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def catalog_view():
        return render_template_string(_CATALOG_PAGE, css=_BASE_CSS, caps=load_catalog())

    @app.get("/runs")
    def runs_view():
        return render_template_string(_RUNS_PAGE, css=_BASE_CSS, runs=load_runs())

    @app.get("/runs/<run_name>")
    def run_detail_view(run_name: str):
        detail = load_run_detail(run_name)
        if detail is None:
            abort(404)
        return render_template_string(_RUN_DETAIL_PAGE, css=_BASE_CSS, run=detail)

    @app.get("/runs/<run_name>/file/<path:filename>")
    def run_file(run_name: str, filename: str):
        # Serve only files that already exist inside this run's own evidence
        # directory — no path outside evidence/<run_name>/ is reachable.
        run_dir = (EVIDENCE_DIR / run_name).resolve()
        if not str(run_dir).startswith(str(EVIDENCE_DIR.resolve())):
            abort(404)
        target = (run_dir / filename).resolve()
        if not str(target).startswith(str(run_dir)) or not target.exists():
            abort(404)
        return send_file(target)

    return app


def main() -> int:
    app = create_app()
    print("[dashboard] ready at http://127.0.0.1:5051/")
    app.run(host="127.0.0.1", port=5051, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
