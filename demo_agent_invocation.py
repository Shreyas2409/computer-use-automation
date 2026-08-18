"""Demo: an agent consumes the catalog cold and answers a real question.

Flow:
    1. Cold-load the approved catalog via ``list_published`` — drafts are
       provably absent (see ``tests/test_catalog.py``).
    2. Translate each Capability to a provider-neutral tool definition.
    3. Give the model the tools and one plain-English question. It picks
       the tool, fills the typed args, and calls it.
    4. Each tool call runs the same replay engine that ``cua replay`` uses;
       the typed ReplayResult goes back to the model as a ``tool_result``.
    5. The model summarizes; the full transcript (question, catalog, turns,
       invocations, final answer) is committed to ``evidence/``.

Run:
    .venv/bin/python demo_agent_invocation.py

Requires an LLM provider key (``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``)
in ``.env`` and the target app on ``http://localhost:8080``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from cua.catalog import (
    InvokeOptions,
    build_tool_definition,
    invoke,
    list_published,
)
from cua.llm import default_model_for, select_provider, translate_tools, make_provider_client

REPO_ROOT = Path(__file__).resolve().parent
EVIDENCE_ROOT = REPO_ROOT / "evidence"
# The brief phrases the question as "member 23456's savings balance" but
# directs us to substitute a member that exists in target_app data (see
# ``target_app/data.py`` — 10001, 10002, 10003 are real). The artifact's
# output is ``checking_balance``; the model reconciles wording in its
# summary.
DEFAULT_QUESTION = "What is member 10001's checking balance?"
MAX_AGENT_TURNS = 6

SYSTEM_PROMPT = (
    "You are a servicing agent's assistant with access to real back-office "
    "tools. Answer the user's question by calling exactly one tool. Fill "
    "in every required argument from the user's question and from the "
    "credentials below. When the tool returns, translate the raw result "
    "into a short, human answer with any currency displayed in dollars.\n"
    "\n"
    "Credentials for the servicing console (demo environment only):\n"
    "  operator_password = demo123\n"
)

# No user follow-up after a tool_result: both providers will produce a
# text-only assistant message on the next call. Injecting a user turn here
# confuses gpt-4o into re-invoking the same tool.


def _redact_secrets_for_transcript(tool_input: dict) -> dict:
    """Never write operator_password to disk. Mirrors cua.policy redaction."""

    redacted = dict(tool_input)
    for key in list(redacted):
        if "password" in key or "secret" in key:
            redacted[key] = "***REDACTED***"
    return redacted


def _redact_arguments_string(arguments: str) -> str:
    """Redact secrets inside a raw model tool_call arguments JSON string.

    The OpenAI transcript records ``tool_calls[].arguments`` verbatim from the
    model; that string is a JSON blob that may carry operator_password. Parse,
    redact, re-serialise so ``turns[]`` gets the same treatment invocations[]
    already gets via ``_redact_secrets_for_transcript``.
    """

    if not arguments:
        return arguments
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments
    if not isinstance(parsed, dict):
        return arguments
    return json.dumps(_redact_secrets_for_transcript(parsed))


def _redact_anthropic_content(content_dump: list[dict]) -> list[dict]:
    """Redact tool_use.input inside an Anthropic assistant content dump.

    The Anthropic transcript stores each turn's ``content`` array; tool_use
    blocks carry the model's proposed arguments dict, which may include a
    secret param. Every other block type passes through unchanged.
    """

    out: list[dict] = []
    for block in content_dump:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            redacted = dict(block)
            raw_input = block.get("input")
            if isinstance(raw_input, dict):
                redacted["input"] = _redact_secrets_for_transcript(raw_input)
            out.append(redacted)
        else:
            out.append(block)
    return out


async def _invoke_one(name: str, tool_input: dict) -> dict:
    """Run one capability against a live browser via the replay engine."""

    return await invoke(
        name,
        tool_input,
        options=InvokeOptions(headless=True),
        evidence_root=EVIDENCE_ROOT,
    )


def _agent_facing_result(result: dict) -> dict:
    """Slice a ReplayResult down to what the LLM actually needs.

    The full result carries steps, rung_drift, evidence paths, etc. — useful
    for audit, noisy for a language model. Give the model only status,
    typed outputs, and business outcome; everything else stays on disk.
    """

    keys = ("status", "outputs", "outcome_name", "message", "exit_code")
    return {k: result[k] for k in keys if k in result}


async def run_demo(question: str, provider: str, model: str) -> Path:
    caps = list_published()
    if not caps:
        raise SystemExit("No approved capabilities in the catalog.")
    tools_ant = [build_tool_definition(c) for c in caps]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = EVIDENCE_ROOT / f"agent_catalog_invocation_{stamp}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = evidence_dir / "transcript.json"
    transcript: dict = {
        "question": question,
        "provider": provider,
        "model": model,
        "catalog": [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["input_schema"]}
            for t in tools_ant
        ],
        "turns": [],
        "invocations": [],
        "final_answer": None,
    }
    client = make_provider_client(provider, model)
    client.start(SYSTEM_PROMPT, tools_ant)
    await _shared_loop(client, question, transcript)
    transcript_path.write_text(json.dumps(transcript, indent=2, default=str))
    return transcript_path


async def _run_tool_call(name: str, args: dict, transcript: dict) -> dict:
    """Run one tool call, log to transcript, return the agent-facing slice."""

    print(f"[demo] agent calls {name!r} args={sorted(args)}", file=sys.stderr)
    result = await _invoke_one(name, args)
    transcript["invocations"].append({
        "name": name,
        "input": _redact_secrets_for_transcript(args),
        "result": result,
    })
    return _agent_facing_result(result)


def _redact_assistant_serialized(serialized: dict) -> dict:
    """Scrub secret params out of a Turn's raw provider payload before it is
    written to disk. OpenAI carries them in ``tool_calls[].arguments`` (a raw
    JSON string); Anthropic carries them in ``content[]`` tool_use blocks."""

    redacted = dict(serialized)
    if redacted.get("provider") == "openai" and redacted.get("tool_calls"):
        redacted["tool_calls"] = [
            {**tc, "arguments": _redact_arguments_string(tc.get("arguments", ""))}
            for tc in redacted["tool_calls"]
        ]
    elif redacted.get("provider") == "anthropic" and redacted.get("content"):
        redacted["content"] = _redact_anthropic_content(redacted["content"])
    return redacted


async def _shared_loop(client, question, transcript):
    user_text = question
    for turn_idx in range(MAX_AGENT_TURNS):
        turn = client.turn(user_text)
        transcript["turns"].append({
            "turn": turn_idx,
            "stop_reason": turn.stop_reason,
            "text": turn.text,
            "assistant_serialized": _redact_assistant_serialized(turn.assistant_serialized)
        })
        
        if turn.stop_reason != "tool_use":
            transcript["final_answer"] = turn.text
            return
            
        for tc in turn.tool_calls:
            out = await _run_tool_call(tc.name, tc.input, transcript)
            client.add_tool_result(tc.id, json.dumps(out))
            
        user_text = "tool results returned."


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    provider = select_provider(os.environ.get("DEMO_PROVIDER") or None)
    model = os.environ.get("CUA_MODEL") or default_model_for(provider)
    question = os.environ.get("DEMO_QUESTION", DEFAULT_QUESTION)
    print(f"[demo] provider={provider} model={model}", file=sys.stderr)
    path = asyncio.run(run_demo(question, provider, model))
    print(f"[demo] transcript committed: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
