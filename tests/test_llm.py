"""Unit tests for the provider-portable LLM adapter (cua/llm.py).

Covers three behaviours the discovery loop depends on without touching a
live API:

* ``translate_tools`` maps the Anthropic-shape TOOLS list to OpenAI's
  ``{type: "function", function: {...}}`` shape and vice versa.
* ``select_provider`` picks the right provider from an explicit flag or
  auto-detects from which API key is present in the environment.
* :class:`OpenAIClient` and :class:`AnthropicClient` normalize a tool-use
  turn (stop_reason, text, tool_calls) and a compile-time JSON call from
  a mock SDK client — so the discovery loop can drive either provider
  through the same interface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from cua.discover import TOOLS
from cua.llm import (
    AnthropicClient,
    OpenAIClient,
    ProviderClient,
    ToolCall,
    Turn,
    default_model_for,
    make_provider_client,
    select_provider,
    translate_tools,
)


# ---------- tool-schema translation --------------------------------------


def test_translate_tools_openai_shape_matches_function_calling_spec():
    translated = translate_tools(TOOLS, "openai")
    assert len(translated) == len(TOOLS)
    for src, dst in zip(TOOLS, translated):
        assert dst["type"] == "function"
        fn = dst["function"]
        assert fn["name"] == src["name"]
        assert fn["description"] == src["description"]
        assert fn["parameters"] == src["input_schema"]
    # The finish tool must survive translation — the loop depends on it.
    assert any(d["function"]["name"] == "finish" for d in translated)


def test_translate_tools_anthropic_is_passthrough_copy():
    translated = translate_tools(TOOLS, "anthropic")
    assert translated == TOOLS
    # Defensive copy — mutating the result must not touch the source.
    translated[0]["name"] = "MUTATED"
    assert TOOLS[0]["name"] != "MUTATED"


def test_translate_tools_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown provider"):
        translate_tools(TOOLS, "cohere")


# ---------- provider selection -------------------------------------------


def test_select_provider_explicit_flag_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert select_provider("openai") == "openai"
    assert select_provider("anthropic") == "anthropic"


def test_select_provider_auto_detects_openai_when_only_that_key_present(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    assert select_provider(None) == "openai"


def test_select_provider_auto_detects_anthropic_when_only_that_key_present(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert select_provider(None) == "anthropic"


def test_select_provider_prefers_anthropic_when_both_keys_present(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    assert select_provider(None) == "anthropic"


def test_select_provider_raises_when_no_keys_and_no_flag(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="No LLM provider key"):
        select_provider(None)


def test_select_provider_rejects_unknown_flag():
    with pytest.raises(ValueError, match="Unknown provider"):
        select_provider("cohere")


def test_default_model_for_returns_provider_specific_id():
    assert default_model_for("anthropic").startswith("claude")
    assert default_model_for("openai").startswith("gpt")
    with pytest.raises(ValueError):
        default_model_for("cohere")


# ---------- mock-client tool-use turn (OpenAI shape) --------------------


class _FakeOpenAIClient:
    """Minimal stand-in exposing ``chat.completions.create``.

    Returns a canned response object shaped like the OpenAI SDK's
    ``ChatCompletion`` — one choice, one tool_call, no text.
    """

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

        class _Chat:
            def __init__(inner, outer: "_FakeOpenAIClient") -> None:
                inner._outer = outer

            class _Completions:
                def __init__(inner, outer: "_FakeOpenAIClient") -> None:
                    inner._outer = outer

                def create(inner, **kwargs: Any) -> Any:
                    inner._outer.calls.append(kwargs)
                    return inner._outer._responses.pop(0)

            @property
            def completions(inner) -> Any:
                return _Chat._Completions(inner._outer)

        self.chat = _Chat(self)


def _openai_tool_call_response(name: str, args: dict[str, Any]) -> Any:
    tool_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    choice = SimpleNamespace(finish_reason="tool_calls", message=message)
    return SimpleNamespace(choices=[choice])


def _openai_text_response(text: str) -> Any:
    message = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(finish_reason="stop", message=message)
    return SimpleNamespace(choices=[choice])


def test_openai_client_turn_normalizes_tool_call_and_records_history():
    fake = _FakeOpenAIClient(
        [_openai_tool_call_response("click", {"role": "button", "name": "Sign In", "why": "log in"})]
    )
    provider = OpenAIClient("gpt-test", client=fake)
    provider.start("SYS", TOOLS)

    turn = provider.turn("first user turn", max_tokens=128)

    assert isinstance(turn, Turn)
    assert turn.stop_reason == "tool_use"
    assert turn.text == ""
    assert len(turn.tool_calls) == 1
    tc = turn.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.name == "click"
    assert tc.input == {"role": "button", "name": "Sign In", "why": "log in"}

    # System message is seeded once; then user, then assistant with tool_calls.
    roles = [m["role"] for m in provider.messages]
    assert roles == ["system", "user", "assistant"]
    asst = provider.messages[-1]
    assert asst["tool_calls"][0]["function"]["name"] == "click"

    # translate_tools was applied — request carried OpenAI-shape tools.
    request_tools = fake.calls[0]["tools"]
    assert all(t["type"] == "function" for t in request_tools)


def test_openai_client_add_tool_result_uses_tool_role():
    fake = _FakeOpenAIClient([_openai_tool_call_response("finish", {"summary": "done"})])
    provider = OpenAIClient("gpt-test", client=fake)
    provider.start("SYS", TOOLS)
    turn = provider.turn("go", max_tokens=64)
    provider.add_tool_result(turn.tool_calls[0].id, "finished")
    last = provider.messages[-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "call_1"
    assert last["content"] == "finished"


def test_openai_client_compile_json_returns_message_text():
    fake = _FakeOpenAIClient([_openai_text_response('{"title": "x"}')])
    provider = OpenAIClient("gpt-test", client=fake)
    raw = provider.compile_json("SYS", "USER", max_tokens=256)
    assert raw == '{"title": "x"}'
    call = fake.calls[0]
    # System prompt is threaded through the messages list, not a top-level key.
    assert call["messages"][0] == {"role": "system", "content": "SYS"}
    assert call["messages"][1] == {"role": "user", "content": "USER"}
    assert "tools" not in call


# ---------- mock-client tool-use turn (Anthropic shape) -----------------


class _FakeAnthropicClient:
    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

        class _Messages:
            def __init__(inner, outer: "_FakeAnthropicClient") -> None:
                inner._outer = outer

            def create(inner, **kwargs: Any) -> Any:
                inner._outer.calls.append(kwargs)
                return inner._outer._responses.pop(0)

        self.messages = _Messages(self)


class _AnthTool:
    type = "tool_use"

    def __init__(self, tool_id: str, name: str, input_data: dict[str, Any]):
        self.id = tool_id
        self.name = name
        self.input = input_data

    def model_dump(self) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


class _AnthText:
    type = "text"

    def __init__(self, text: str):
        self.text = text

    def model_dump(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


def _anth_response(blocks: list[Any], stop_reason: str = "tool_use") -> Any:
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


def test_anthropic_client_turn_normalizes_tool_use():
    fake = _FakeAnthropicClient(
        [_anth_response([_AnthTool("tu_1", "type_text", {"role": "textbox", "name": "Member ID", "text": "10001", "why": "search"})])]
    )
    provider = AnthropicClient("claude-test", client=fake)
    provider.start("SYS", TOOLS)
    turn = provider.turn("user turn", max_tokens=128)

    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls[0].name == "type_text"
    assert turn.tool_calls[0].input["text"] == "10001"

    # Anthropic path threads system via the SDK kwarg (not as a message).
    call = fake.calls[0]
    assert call["system"] == "SYS"
    assert call["messages"][0] == {"role": "user", "content": "user turn"}
    assert call["messages"][-1]["role"] == "assistant"


def test_anthropic_client_add_tool_result_uses_content_block():
    fake = _FakeAnthropicClient([_anth_response([_AnthTool("tu_1", "finish", {"summary": "done"})])])
    provider = AnthropicClient("claude-test", client=fake)
    provider.start("SYS", TOOLS)
    turn = provider.turn("go", max_tokens=64)
    provider.add_tool_result(turn.tool_calls[0].id, "finished")
    last = provider.messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "tu_1"


def test_anthropic_client_compile_json_returns_joined_text():
    fake = _FakeAnthropicClient(
        [_anth_response([_AnthText('{"title":'), _AnthText(' "x"}')], stop_reason="end_turn")]
    )
    provider = AnthropicClient("claude-test", client=fake)
    raw = provider.compile_json("SYS", "USER", max_tokens=256)
    assert raw == '{"title": "x"}'


# ---------- factory + protocol conformance ------------------------------


def test_make_provider_client_dispatches_by_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    # Both providers should construct without touching the network — the
    # SDK constructors only validate config until a call is made.
    a: ProviderClient = make_provider_client("anthropic", "claude-x")
    o: ProviderClient = make_provider_client("openai", "gpt-x")
    assert a.name == "anthropic"
    assert o.name == "openai"
    with pytest.raises(ValueError):
        make_provider_client("cohere", "x")
