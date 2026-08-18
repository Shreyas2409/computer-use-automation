"""Provider-portable LLM client for the discovery loop.

The discovery loop calls exactly one LLM. This module isolates the wire
shape so it can run against either the Anthropic Messages API or the
OpenAI Chat Completions function-calling API. The tool schema in
``cua/discover.py`` is authored in Anthropic's shape (``name`` +
``input_schema``); ``translate_tools`` converts it to OpenAI's
``function`` shape. Both providers are represented by the same
:class:`ProviderClient` interface: ``start(system, tools)`` opens a
conversation, ``turn(user_text)`` sends one user message and returns a
normalized :class:`Turn`, ``add_tool_result`` feeds a tool_result back
into the transcript, and ``compile_json(...)`` runs the one-shot final
call that proposes the caller contract.

The Anthropic and OpenAI SDKs are imported lazily so tests can mock the
underlying client without either dependency at collection time.

``cua/replay.py`` must import neither SDK; see the grep guard in
``tests/test_surface_and_ladder.py``.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5",
}

_KNOWN_PROVIDERS = ("anthropic", "openai")


@dataclass
class ToolCall:
    """One tool invocation the model asked for this turn."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    """Normalized one-turn response across providers."""

    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens" | "other"
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_serialized: dict[str, Any] = field(default_factory=dict)


# ---- Tool-schema translation --------------------------------------------


def translate_tools(tools: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Translate the Anthropic-shape TOOLS list to the target provider.

    Anthropic tool: ``{name, description, input_schema}``.
    OpenAI tool:    ``{type: "function", function: {name, description, parameters}}``.
    """

    if provider == "anthropic":
        # Passthrough (defensive copy).
        return [dict(t) for t in tools]
    if provider == "openai":
        out: list[dict[str, Any]] = []
        for t in tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
            )
        return out
    raise ValueError(f"Unknown provider for tool translation: {provider!r}")


# ---- Provider selection --------------------------------------------------


def _env_has(name: str) -> bool:
    value = os.environ.get(name, "")
    return bool(value and value.strip())


def select_provider(explicit: str | None) -> str:
    """Pick a provider. Explicit wins; otherwise auto-detect from env.

    Raises if the caller asked for an unknown provider or if no key is
    present when auto-detecting. Never returns a key value.
    """

    if explicit:
        if explicit not in _KNOWN_PROVIDERS:
            raise ValueError(
                f"Unknown provider {explicit!r}; choose from {_KNOWN_PROVIDERS}."
            )
        return explicit
    has_anth = _env_has("ANTHROPIC_API_KEY")
    has_oai = _env_has("OPENAI_API_KEY")
    if has_anth and not has_oai:
        return "anthropic"
    if has_oai and not has_anth:
        return "openai"
    if has_anth and has_oai:
        # Both present: default to the historically-named provider so the
        # existing docs and recorded runs keep working.
        return "anthropic"
    raise RuntimeError(
        "No LLM provider key found in environment. Set ANTHROPIC_API_KEY "
        "or OPENAI_API_KEY in .env, or pass --provider explicitly."
    )


def default_model_for(provider: str) -> str:
    """Return the fallback model id for ``provider`` (env-overridable upstream)."""

    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unknown provider: {provider!r}")
    return DEFAULT_MODELS[provider]


# ---- Provider client interface ------------------------------------------


class ProviderClient(ABC):
    """Uniform interface used by ``cua.discover`` for tool-use loops."""

    name: str

    @abstractmethod
    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        """Reset conversation state; store system prompt and translate tools."""

    @abstractmethod
    def turn(self, user_text: str, max_tokens: int = 1024) -> Turn:
        """Append a user turn, call the model, append the assistant reply."""

    @abstractmethod
    def add_tool_result(self, tool_use_id: str, content: str) -> None:
        """Record a tool_result the surface produced for one tool call."""

    @abstractmethod
    def compile_json(
        self, system: str, user_text: str, max_tokens: int = 2048
    ) -> str:
        """Single-shot text call for the compilation proposal."""


class AnthropicClient(ProviderClient):
    """Anthropic Messages API adapter."""

    name = "anthropic"

    def __init__(self, model: str, *, client: Any = None) -> None:
        self.model = model
        if client is None:
            from anthropic import Anthropic  # noqa: WPS433

            client = Anthropic()  # picks up ANTHROPIC_API_KEY
        self._client = client
        self.system: str = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        self.system = system
        self.tools = translate_tools(tools, "anthropic")
        self.messages = []

    def turn(self, user_text: str, max_tokens: int = 1024) -> Turn:
        self.messages.append({"role": "user", "content": user_text})
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for b in resp.content:
            btype = getattr(b, "type", "")
            if btype == "text":
                text_parts.append(getattr(b, "text", "") or "")
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=b.id, name=b.name, input=dict(b.input))
                )
        try:
            asst_dump = [b.model_dump() for b in resp.content]
        except Exception:
            asst_dump = [{"text": str(b)} for b in resp.content]
        self.messages.append({"role": "assistant", "content": asst_dump})

        stop_reason = getattr(resp, "stop_reason", None) or ""
        norm_stop = _normalize_anthropic_stop(stop_reason)
        return Turn(
            stop_reason=norm_stop,
            text="".join(text_parts),
            tool_calls=tool_calls,
            assistant_serialized={
                "provider": "anthropic",
                "stop_reason": stop_reason,
                "content": asst_dump,
            },
        )

    def add_tool_result(self, tool_use_id: str, content: str) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }
                ],
            }
        )

    def compile_json(
        self, system: str, user_text: str, max_tokens: int = 2048
    ) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        return "".join(
            getattr(b, "text", "") or ""
            for b in resp.content
            if getattr(b, "type", "") == "text"
        ).strip()


class OpenAIClient(ProviderClient):
    """OpenAI Chat Completions function-calling adapter."""

    name = "openai"

    def __init__(self, model: str, *, client: Any = None) -> None:
        self.model = model
        if client is None:
            from openai import OpenAI  # noqa: WPS433

            client = OpenAI()  # picks up OPENAI_API_KEY
        self._client = client
        self.system: str = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        self.system = system
        self.tools = translate_tools(tools, "openai")
        self.messages = [{"role": "system", "content": system}]

    def turn(self, user_text: str, max_tokens: int = 1024) -> Turn:
        self.messages.append({"role": "user", "content": user_text})
        resp = self._client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens * 4,
            tools=self.tools,
            tool_choice="required",
            parallel_tool_calls=False,
            messages=self.messages,
        )
        choice = resp.choices[0]
        msg = choice.message
        text = (msg.content or "").strip()
        raw_tool_calls = getattr(msg, "tool_calls", None) or []
        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.function
            args_raw = fn.arguments or "{}"
            try:
                parsed = json.loads(args_raw)
            except json.JSONDecodeError:
                parsed = {}
            tool_calls.append(
                ToolCall(id=tc.id, name=fn.name, input=parsed)
            )

        asst_entry: dict[str, Any] = {
            "role": "assistant",
            "content": text or "",
        }
        if raw_tool_calls:
            asst_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in raw_tool_calls
            ]
        self.messages.append(asst_entry)

        norm_stop = _normalize_openai_finish(getattr(choice, "finish_reason", None))
        return Turn(
            stop_reason=norm_stop,
            text=text,
            tool_calls=tool_calls,
            assistant_serialized={
                "provider": "openai",
                "finish_reason": getattr(choice, "finish_reason", None),
                "text": text,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    }
                    for tc in raw_tool_calls
                ],
            },
        )

    def add_tool_result(self, tool_use_id: str, content: str) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_use_id,
                "content": content,
            }
        )

    def compile_json(
        self, system: str, user_text: str, max_tokens: int = 2048
    ) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_tokens * 4,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
        )
        return (resp.choices[0].message.content or "").strip()


def _normalize_anthropic_stop(stop_reason: str) -> str:
    if stop_reason == "tool_use":
        return "tool_use"
    if stop_reason == "end_turn":
        return "end_turn"
    if stop_reason == "max_tokens":
        return "max_tokens"
    return "other"


def _normalize_openai_finish(finish_reason: str | None) -> str:
    if finish_reason in ("tool_calls", "function_call"):
        return "tool_use"
    if finish_reason == "stop":
        return "end_turn"
    if finish_reason == "length":
        return "max_tokens"
    return "other"


def make_provider_client(provider: str, model: str) -> ProviderClient:
    """Factory: build a ProviderClient bound to a real SDK client."""

    if provider == "anthropic":
        return AnthropicClient(model)
    if provider == "openai":
        return OpenAIClient(model)
    raise ValueError(f"Unknown provider: {provider!r}")
