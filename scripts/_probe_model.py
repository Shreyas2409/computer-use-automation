#!/usr/bin/env python3
"""Throwaway probe: test GPT-5-series models against chat.completions.create.

Tests three questions:
  a) Does the model exist?  (or 404 / InvalidRequestError)
  b) Does it accept max_tokens, or require max_completion_tokens?
  c) Does it accept tool_choice="required" and parallel_tool_calls=False?
"""

import json
import os
import sys
import traceback

# Load .env so OPENAI_API_KEY is available
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from openai import OpenAI  # noqa: E402

client = OpenAI()

SYSTEM = "You are a helpful assistant. Reply with a tool call."
TOOL = {
    "type": "function",
    "function": {
        "name": "echo",
        "description": "Echoes the input back.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The text to echo."}
            },
            "required": ["message"],
        },
    },
}
USER_MSG = "Echo the word 'hello'."

MODELS_TO_TRY = ["gpt-5.6-terra", "gpt-5.6", "gpt-5"]


def probe(model: str) -> dict:
    """Run the three-part probe against one model id."""
    result = {
        "model": model,
        "exists": False,
        "max_tokens_ok": None,
        "max_completion_tokens_ok": None,
        "tool_choice_required_ok": None,
        "parallel_tool_calls_false_ok": None,
        "errors": [],
    }

    base_kwargs = dict(
        model=model,
        tools=[TOOL],
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_MSG},
        ],
    )

    # ---- (a) Does the model exist, with max_tokens? ----
    print(f"\n{'='*60}")
    print(f"  Probing model: {model}")
    print(f"{'='*60}")

    print(f"\n  [a1] chat.completions.create with max_tokens=256 ...")
    try:
        resp = client.chat.completions.create(**base_kwargs, max_tokens=256)
        result["exists"] = True
        result["max_tokens_ok"] = True
        _show_response(resp)
    except Exception as e:
        err_str = str(e)
        result["errors"].append(f"max_tokens: {err_str}")
        print(f"       FAILED: {err_str[:300]}")
        # If 404 / model-not-found, no point testing further
        if "404" in err_str or "not found" in err_str.lower() or "does not exist" in err_str.lower():
            print("       → Model does not exist on this account. Skipping.")
            return result

    # ---- (a2) Try max_completion_tokens instead ----
    print(f"\n  [a2] chat.completions.create with max_completion_tokens=256 ...")
    try:
        resp = client.chat.completions.create(**base_kwargs, max_completion_tokens=256)
        result["exists"] = True
        result["max_completion_tokens_ok"] = True
        _show_response(resp)
    except Exception as e:
        err_str = str(e)
        result["errors"].append(f"max_completion_tokens: {err_str}")
        print(f"       FAILED: {err_str[:300]}")
        if "404" in err_str or "not found" in err_str.lower() or "does not exist" in err_str.lower():
            result["exists"] = False
            return result

    if not result["exists"]:
        print("       → Model not reachable via either token param. Skipping.")
        return result

    # ---- (b) Determine which token param to use going forward ----
    token_kwarg = {}
    if result["max_tokens_ok"]:
        token_kwarg = {"max_tokens": 256}
    elif result["max_completion_tokens_ok"]:
        token_kwarg = {"max_completion_tokens": 256}

    # ---- (c) tool_choice="required" + parallel_tool_calls=False ----
    print(f"\n  [c] tool_choice='required', parallel_tool_calls=False ...")
    try:
        resp = client.chat.completions.create(
            **base_kwargs,
            **token_kwarg,
            tool_choice="required",
            parallel_tool_calls=False,
        )
        result["tool_choice_required_ok"] = True
        result["parallel_tool_calls_false_ok"] = True
        _show_response(resp)
    except Exception as e:
        err_str = str(e)
        result["errors"].append(f"tool_choice+parallel: {err_str}")
        print(f"       FAILED: {err_str[:300]}")
        # Try them individually
        print(f"\n  [c1] tool_choice='required' alone ...")
        try:
            resp = client.chat.completions.create(
                **base_kwargs, **token_kwarg, tool_choice="required"
            )
            result["tool_choice_required_ok"] = True
            _show_response(resp)
        except Exception as e2:
            result["tool_choice_required_ok"] = False
            result["errors"].append(f"tool_choice alone: {e2}")
            print(f"       FAILED: {str(e2)[:300]}")

        print(f"\n  [c2] parallel_tool_calls=False alone ...")
        try:
            resp = client.chat.completions.create(
                **base_kwargs, **token_kwarg, parallel_tool_calls=False
            )
            result["parallel_tool_calls_false_ok"] = True
            _show_response(resp)
        except Exception as e3:
            result["parallel_tool_calls_false_ok"] = False
            result["errors"].append(f"parallel_tool_calls alone: {e3}")
            print(f"       FAILED: {str(e3)[:300]}")

    return result


def _show_response(resp):
    choice = resp.choices[0]
    print(f"       model_id returned: {resp.model}")
    print(f"       finish_reason: {choice.finish_reason}")
    msg = choice.message
    if msg.content:
        print(f"       text: {msg.content[:120]}")
    if msg.tool_calls:
        for tc in msg.tool_calls:
            print(f"       tool_call: {tc.function.name}({tc.function.arguments})")
    usage = resp.usage
    if usage:
        print(f"       usage: prompt={usage.prompt_tokens}, completion={usage.completion_tokens}, total={usage.total_tokens}")


def main():
    print("=" * 60)
    print("  GPT-5 series Chat Completions compatibility probe")
    print("=" * 60)

    results = []
    for model in MODELS_TO_TRY:
        r = probe(model)
        results.append(r)
        if r["exists"]:
            print(f"\n  ✓ {model} EXISTS — stopping search.")
            break
        else:
            print(f"\n  ✗ {model} not available, trying next...")

    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"\n  Model: {r['model']}")
        print(f"    exists:                    {r['exists']}")
        print(f"    max_tokens accepted:       {r['max_tokens_ok']}")
        print(f"    max_completion_tokens ok:  {r['max_completion_tokens_ok']}")
        print(f"    tool_choice=required ok:   {r['tool_choice_required_ok']}")
        print(f"    parallel_tool_calls=False: {r['parallel_tool_calls_false_ok']}")
        if r["errors"]:
            print(f"    errors:")
            for e in r["errors"]:
                print(f"      - {e[:200]}")
    print()


if __name__ == "__main__":
    main()
