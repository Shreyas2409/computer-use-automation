"""Flask operator console — plain page + JSON control endpoints.

Deliberately minimal per the brief: polling screenshot, "Take control" /
"Hand back" buttons, and a single scripted-intervention endpoint. Browser
work is delegated to a handler the caller registers on the broker before
starting the server, so this module never imports Playwright.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template_string, request, send_file

from cua.escalate import EscalationBroker, LeaseError


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


_CONSOLE_HTML = """<!doctype html>
<html>
<head>
<title>Operator console</title>
<style>
  body { font-family: Verdana, Arial, sans-serif; font-size: 12px; margin: 12px; }
  header { background: #003366; color: white; padding: 8px; }
  #screenshot { border: 1px solid #999; max-width: 1024px; margin-top: 8px; }
  button { padding: 6px 12px; margin-right: 8px; }
  #transitions td { padding: 2px 6px; border-bottom: 1px solid #eee; }
</style>
</head>
<body>
<header><b>Operator console</b> &mdash; escalation surface</header>
<div id="state">Loading…</div>
<img id="screenshot" src="/screenshot?ts=0" onerror="this.style.display='none'" />
<h4>Transition log</h4>
<table id="transitions"><tbody></tbody></table>
<script>
async function refresh() {
  const r = await fetch('/state');
  const s = await r.json();
  const p = s.pending;
  const btns =
    `<button onclick="post('/take_control')">Take control</button>` +
    `<button onclick="post('/hand_back')">Hand back</button>`;
  document.getElementById('state').innerHTML =
    `<p><b>Lease</b>: ${s.lease.state} (holder=${s.lease.holder}) ${btns}</p>` +
    (p ? `<p><b>Reason</b>: ${p.reason}<br/><b>Step</b>: ${p.step_index} <b>Capability</b>: ${p.capability_id}@${p.version} (${p.tenant_id})</p>` : `<p><i>no pending intervention</i></p>`);
  const tbody = document.querySelector('#transitions tbody');
  tbody.innerHTML = s.lease.transitions.map(t =>
    `<tr><td>${t.at}</td><td>${t.from_state}→${t.to_state}</td><td>${t.actor}</td><td>${t.reason}</td></tr>`
  ).join('');
  const img = document.getElementById('screenshot');
  img.style.display = ''; img.src = '/screenshot?ts=' + Date.now();
}
async function post(url) {
  const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  await r.json(); refresh();
}
setInterval(refresh, 1000); refresh();
</script>
</body></html>
"""


def _serialize_state(broker: EscalationBroker) -> dict[str, Any]:
    pending = broker.pending()
    pending_dict: dict[str, Any] | None = None
    if pending is not None:
        pending_dict = asdict(pending)
        # Never leak the raw resume token to the polling UI; it's still
        # accessible internally to callers holding the broker reference.
        pending_dict.pop("resume_token", None)
    return {
        "lease": {
            "state": broker.lease.state,
            "holder": broker.lease.holder,
            "transitions": broker.lease.transitions_as_dicts(),
        },
        "pending": pending_dict,
        "action_log": broker.action_log(),
    }


def _run_sync(coro: Any) -> Any:
    """Run an awaitable to completion whether or not a loop is already active."""

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():  # pragma: no cover — Flask sync handlers, not nested
        return asyncio.run_coroutine_threadsafe(coro, loop).result()
    return loop.run_until_complete(coro)


def create_app(broker: EscalationBroker) -> Flask:
    """Build a Flask app bound to ``broker``. One broker per app instance."""

    app = Flask(__name__)
    app.config["broker"] = broker

    def _broker() -> EscalationBroker:
        return app.config["broker"]

    @app.get("/")
    def index() -> Any:
        return render_template_string(_CONSOLE_HTML)

    @app.get("/state")
    def state() -> Any:
        return jsonify(_serialize_state(_broker()))

    @app.get("/screenshot")
    def screenshot() -> Any:
        pending = _broker().pending()
        if pending is not None and pending.screenshot_path:
            p = Path(pending.screenshot_path)
            if p.exists() and p.stat().st_size > 0:
                return send_file(p, mimetype="image/png")
        # Return a tiny placeholder rather than 404 so the console poll
        # never spams the console log with error rows.
        return send_file(BytesIO(_PNG_1x1), mimetype="image/png")

    @app.post("/take_control")
    def take_control() -> Any:
        try:
            row = _broker().take_control(actor=_actor())
        except LeaseError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True, "transition": asdict(row)})

    @app.post("/hand_back")
    def hand_back() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            row = _broker().hand_back(
                actor=_actor(),
                notes=str(payload.get("notes", "")),
                resume_token=payload.get("resume_token"),
            )
        except LeaseError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        return jsonify({"ok": True, "transition": asdict(row)})

    @app.post("/intervene")
    def intervene() -> Any:
        command = request.get_json(silent=True) or {}
        try:
            entry = _run_sync(_broker().invoke_action(command))
        except LeaseError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({"ok": True, "entry": entry})

    return app


def _actor() -> str:
    return (request.get_json(silent=True) or {}).get("actor", "operator")


# 1x1 transparent PNG — kept inline so we do not depend on a bundled asset.
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "create_app"]
