"""Thin conversational driver over the capability catalog.

One page, one endpoint — a demo driver, not a second product. A user types a
request in plain language; the model (via ``cua.llm``, no separate client)
picks a capability from the published catalog, supplies typed args, and the
capability runs through ``cua.catalog.invoke()`` — the exact function the
Flask ``/capabilities/<name>/invoke`` endpoint and the CLI use. This file
never imports ``cua.replay``, ``cua.surface``, or ``cua.policy`` — the
capability API is the only door in, so the same policy gate, evidence
writer, and escalation broker apply as everywhere else. The response states
the outcome in plain language: confirmation numbers on success, or on the
unhappy path what went wrong and whether it escalated.

Run:
    .venv/bin/python chatbot.py
Then open http://127.0.0.1:5050/

Escalations open the operator console this also starts, at
http://127.0.0.1:8765/.

Requires an LLM provider key (ANTHROPIC_API_KEY or OPENAI_API_KEY) in .env.
"""

from __future__ import annotations

import asyncio
import json
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from cua.__main__ import _start_operator
from cua.catalog import InvokeOptions, build_tool_definition, invoke, list_published
from cua.llm import default_model_for, make_provider_client, select_provider

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_ROOT = os.path.join(REPO_ROOT, "evidence")
MAX_AGENT_TURNS = 6

SYSTEM_PROMPT = (
    "You are a credit-union teller's assistant with access to real "
    "back-office tools for the Meridian Core servicing console. Answer the "
    "user's request by calling exactly one capability tool. Fill in every "
    "required argument from the user's request and from the credentials "
    "below. When the tool returns, translate the raw structured result "
    "into a short, plain-language answer — a success stated with the "
    "actual values (including any confirmation number), a business "
    "outcome (e.g. member not found, insufficient funds) stated plainly, "
    "or a clear note that the action was escalated to a human operator or "
    "failed, with why.\n"
    "\n"
    "Capability selection: every tool whose name starts with 'mc_' targets "
    "Meridian Core, the live servicing console this assistant is for — "
    "prefer these for any member/balance/transfer/share/hold request. Your "
    "tool list may also include capabilities with plain names (e.g. "
    "'lookup_member_balance', 'open_subaccount') left over from a "
    "different, unrelated demo application on a different host with its "
    "own separate credentials — do not call these for a Meridian Core "
    "request, even if their name or description sounds similar. If asked "
    "to look up a balance, transfer funds, open a share, place a hold, or "
    "update a member on Meridian Core, always use the matching 'mc_*' "
    "tool.\n"
    "\n"
    "Hard constraints on what you may say, not just what you may do:\n"
    "- Report ONLY what a tool call actually returned. Never describe an "
    "action, notification, or state change that no tool result reported. "
    "If you did not call a tool for it, it did not happen.\n"
    "- Offer ONLY the capabilities in your tool list. Never invent a "
    "retry-when-condition-changes feature, a notification, a hold-clearing "
    "action, or any other follow-up capability that is not one of your "
    "tools.\n"
    "- You cannot initiate an escalation on request. Escalation is raised "
    "automatically by the policy gate while a capability is running, when "
    "an action needs human confirmation — never because a user asked you "
    "to escalate. If a user asks you to escalate something after the fact "
    "(e.g. a rejected transfer), say plainly that you cannot do that, and "
    "explain that escalation only happens automatically when a write "
    "action you invoke hits the policy gate.\n"
    "- On a business outcome (rejection, not-found, insufficient funds, "
    "etc.), state the outcome and stop. You may suggest a different valid "
    "invocation (e.g. a different source share), but never imply any "
    "state change you cannot actually perform.\n"
    "\n"
    "Demo operator credentials (test environment only):\n"
    "  operator_password = password   (operator id: teller1)\n"
)

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Servicing Assistant (demo)</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.1rem; color: #333; }
  #log { border: 1px solid #ccc; border-radius: 6px; padding: 0.75rem; min-height: 240px;
         margin-bottom: 0.75rem; overflow-y: auto; max-height: 60vh; }
  .row { margin: 0.4rem 0; white-space: pre-wrap; }
  .you { color: #0645ad; }
  .assistant { color: #1a1a1a; }
  .pending { color: #888; font-style: italic; }
  #row { display: flex; gap: 0.5rem; }
  #msg { flex: 1; padding: 0.5rem; }
  button { padding: 0.5rem 1rem; }
</style>
</head>
<body>
  <h1>Servicing Assistant — demo driver over the capability catalog</h1>
  <div id="log"></div>
  <div id="row">
    <input id="msg" autocomplete="off" placeholder="e.g. what is member 100234's balance">
    <button id="send">Send</button>
  </div>
<script>
const log = document.getElementById('log');
const msg = document.getElementById('msg');
document.getElementById('send').onclick = send;
msg.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });

function append(cls, text) {
  const div = document.createElement('div');
  div.className = 'row ' + cls;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

async function send() {
  const text = msg.value.trim();
  if (!text) return;
  append('you', 'you: ' + text);
  msg.value = '';
  const pending = append('pending', 'working...');
  try {
    const r = await fetch('/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text}),
    });
    const data = await r.json();
    pending.remove();
    append('assistant', 'assistant: ' + data.reply);
  } catch (err) {
    pending.remove();
    append('assistant', 'assistant: (request failed: ' + err + ')');
  }
}
</script>
</body>
</html>
"""


async def _invoke_one(name: str, tool_input: dict, broker) -> dict:
    """Run one capability via the same invoke() path the Flask API uses."""

    return await invoke(
        name,
        tool_input,
        options=InvokeOptions(headless=True, escalation_broker=broker),
        evidence_root=EVIDENCE_ROOT,
    )


def _agent_facing_result(result: dict) -> dict:
    keys = ("status", "outputs", "outcome_name", "message", "exit_code")
    return {k: result[k] for k in keys if k in result}


async def _run_tool_call(name: str, args: dict, broker) -> dict:
    result = await _invoke_one(name, args, broker)
    return _agent_facing_result(result)


async def _one_turn_reply(client, user_text: str, broker) -> str:
    """Run the tool-use loop for one user message; return the final reply."""

    for _ in range(MAX_AGENT_TURNS):
        turn = client.turn(user_text)
        if turn.stop_reason != "tool_use":
            return turn.text
        for tc in turn.tool_calls:
            out = await _run_tool_call(tc.name, tc.input, broker)
            client.add_tool_result(tc.id, json.dumps(out))
        user_text = "tool results returned."
    return "(stopped after max turns without a final answer)"


def create_app() -> Flask:
    load_dotenv(os.path.join(REPO_ROOT, ".env"), override=False)
    provider = select_provider(os.environ.get("DEMO_PROVIDER") or None)
    model = os.environ.get("CUA_MODEL") or default_model_for(provider)

    caps = list_published()
    if not caps:
        raise SystemExit("No approved capabilities in the catalog yet.")
    tools = [build_tool_definition(c) for c in caps]

    broker, _operator_thread = _start_operator("127.0.0.1", 8765)

    # Single shared conversation client: this is a thin, single-user demo
    # driver, not a multi-session product — state is deliberately simple.
    client = make_provider_client(provider, model)
    client.start(SYSTEM_PROMPT, tools)

    app = Flask(__name__)

    @app.get("/")
    def index():
        return _PAGE

    @app.post("/chat")
    def chat():
        payload = request.get_json(force=True, silent=True) or {}
        user_text = str(payload.get("message", "")).strip()
        if not user_text:
            return jsonify({"reply": "Say something first."})
        reply = asyncio.run(_one_turn_reply(client, user_text, broker))
        return jsonify({"reply": reply})

    return app


def main() -> int:
    app = create_app()
    print("[chatbot] ready at http://127.0.0.1:5050/")
    print("[chatbot] operator console at http://127.0.0.1:8765/ (for escalations)")
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
