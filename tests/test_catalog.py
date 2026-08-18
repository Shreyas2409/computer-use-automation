"""Agent-facing catalog tests.

Three surfaces are covered:

1. Listing hides drafts and exposes only the highest approved version per
   capability id.
2. Tool definitions generated from the schema are machine-consumable JSON
   Schema documents keyed off the real artifacts on disk.
3. Invoking by name via the catalog runs the replay engine end-to-end with
   an in-memory FakeSurface (no browser) and returns the typed contract.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cua.catalog import (
    CapabilityNotPublished,
    InvokeOptions,
    build_input_schema,
    build_output_schema,
    build_tool_definition,
    capability_signature,
    invoke_with_surface,
    list_published,
    param_to_json_schema,
    resolve_capability,
)
from cua.evidence import EvidenceWriter
from cua.replay import ParamBindingError
from cua.schema import Capability, ParamSpec

from tests.test_replay import FakeSurface  # reuse the same fake


REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO_ROOT / "artifacts"


# ---- Fixtures -----------------------------------------------------------


def _write_artifact(dir_: Path, cap: dict) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"v{cap['version_major']}.{cap['version_minor']}.json"
    path.write_text(json.dumps(cap))
    return path


def _real_cap_dict() -> dict:
    # Piggy-back on the committed real artifact for a valid Capability blob.
    return json.loads(
        (ARTIFACTS / "lookup_member_balance" / "v2.0.json").read_text()
    )


def _minimal_cap_dict(name: str = "published_cap") -> dict:
    """A minimal capability with no checkpoints — safe for FakeSurface.

    One navigate + one read step; two outputs extracted via the ladder. Uses
    the same ParamSpec / OutputSpec shapes as the real artifact so tool-def
    generation exercises the same code paths.
    """

    label = {
        "strategies": [
            {"kind": "label", "spec": {"label": "Balance"}, "durability": 90}
        ],
        "recorded_match": "label",
    }
    return {
        "schema_version": "1.0",
        "capability_id": name,
        "version_major": 1,
        "version_minor": 0,
        "title": "Look up member balance",
        "description": "Minimal fixture capability used in catalog tests.",
        "target": {
            "app_id": "test_app",
            "base_url": "http://localhost:8080",
            "tenant_id": "meridian",
            "target_version": "1.0",
            "surface_kind": "legacy_web",
        },
        "inputs": [
            {
                "name": "member_id",
                "type": "string",
                "required": True,
                "description": "Member id.",
                "example": "23456",
                "sensitivity": "internal",
            },
            {
                "name": "operator_password",
                "type": "string",
                "required": True,
                "description": "Secret password.",
                "example": "demo123",
                "sensitivity": "secret",
            },
        ],
        "outputs": [
            {
                "name": "checking_balance",
                "type": "currency",
                "description": "Balance in cents.",
                "extract": {
                    "locator": label,
                    "regex": r"\$([0-9,]+\.[0-9]{2})",
                    "transform": "currency_to_cents",
                },
            },
            {
                "name": "member_status",
                "type": "string",
                "description": "Servicing status.",
                "extract": {"locator": label, "transform": "strip"},
            },
        ],
        "steps": [
            {
                "index": 0,
                "action": "navigate",
                "value": "http://localhost:8080/members/23456",
                "on_error": [],
                "notes": "",
            }
        ],
        "outcomes": [
            {
                "name": "no_such_member",
                "detect": {
                    "kind": "text_match",
                    "contains": "No member found",
                    "case_sensitive": False,
                },
                "classification": "business_outcome",
                "terminal": True,
                "message": "No member found for that ID.",
            }
        ],
        "approval": "approved",
        "recorded_at": "2026-01-01T00:00:00Z",
        "recorded_by": "human_edit",
    }


@pytest.fixture
def temp_registry(tmp_path: Path) -> Path:
    """A temp artifacts root with two capabilities: one approved, one drafts-only.

    - ``published_cap/v1.0.json``: approved.
    - ``draft_only_cap/v1.0.json``: approval='draft'. Must NOT appear in
      list_published() and must NOT be resolvable by name.
    """

    root = tmp_path / "artifacts"

    approved = _minimal_cap_dict("published_cap")
    _write_artifact(root / "published_cap", approved)

    draft = _minimal_cap_dict("draft_only_cap")
    draft["approval"] = "draft"
    _write_artifact(root / "draft_only_cap", draft)

    return root


# ---- 1) Listing hides drafts --------------------------------------------


def test_list_published_hides_draft_only_capabilities(temp_registry: Path):
    caps = list_published(temp_registry)
    names = [c.capability_id for c in caps]
    assert "published_cap" in names
    assert "draft_only_cap" not in names  # provably absent


def test_list_published_returns_highest_approved(tmp_path: Path):
    """Given approved v1.0 and draft v2.0, list_published picks v1.0."""

    root = tmp_path / "artifacts"
    base = _minimal_cap_dict("mixed_cap")

    v1 = dict(base, version_major=1, version_minor=0, approval="approved")
    v2 = dict(base, version_major=2, version_minor=0, approval="draft")
    _write_artifact(root / "mixed_cap", v1)
    _write_artifact(root / "mixed_cap", v2)

    caps = list_published(root)
    assert len(caps) == 1
    assert caps[0].version_major == 1


def test_resolve_capability_refuses_draft_only(temp_registry: Path):
    with pytest.raises(CapabilityNotPublished):
        resolve_capability("draft_only_cap", artifacts_dir=temp_registry)


# ---- 2) Tool definitions from real on-disk artifacts --------------------


def _real_published() -> Capability:
    caps = list_published()
    assert caps, "expected at least one approved capability on disk"
    return next(c for c in caps if c.capability_id == "lookup_member_balance")


def test_tool_definition_matches_anthropic_shape():
    cap = _real_published()
    td = build_tool_definition(cap)
    assert set(td.keys()) >= {"name", "description", "input_schema"}
    assert td["name"] == cap.capability_id
    assert td["input_schema"]["type"] == "object"
    assert td["input_schema"]["additionalProperties"] is False
    assert "properties" in td["input_schema"]


def test_input_schema_reflects_paramspec_and_sensitivity():
    cap = _real_published()
    schema = build_input_schema(cap)
    for p in cap.inputs:
        assert p.name in schema["properties"], f"missing {p.name}"
        prop = schema["properties"][p.name]
        assert prop["x-sensitivity"] == p.sensitivity
        assert prop["description"] == p.description
    # invocation controls must always be present
    assert "version" in schema["properties"]
    assert "tenant_id" in schema["properties"]
    # required is exactly the declared required params (not tenant/version)
    required = set(schema["required"])
    assert required == {p.name for p in cap.inputs if p.required}


def test_output_schema_currency_becomes_integer_cents():
    cap = _real_published()
    schema = build_output_schema(cap)
    props = schema["properties"]
    for out in cap.outputs:
        assert out.name in props
        assert props[out.name]["description"] == out.description
        if out.type == "currency":
            # After ExtractSpec.currency_to_cents transform, outputs are ints.
            assert props[out.name]["type"] == "integer"


def test_capability_signature_hides_locators():
    cap = _real_published()
    sig = capability_signature(cap)
    # No ladder/locator/step data must leak through the agent-facing surface.
    blob = json.dumps(sig)
    assert "strategies" not in blob
    assert "recorded_match" not in blob
    assert "steps" not in blob
    # Business outcomes are exposed; hard-failure outcomes are not.
    outcome_names = {o["name"] for o in sig["business_outcomes"]}
    declared_business = {
        o.name for o in cap.outcomes if o.classification == "business_outcome"
    }
    assert outcome_names == declared_business


# ---- 3) Invoke by name via FakeSurface ----------------------------------


def _cap_dict_with_id(name: str) -> dict:
    return _minimal_cap_dict(name)


def test_invoke_by_name_runs_replay_engine(temp_registry: Path, tmp_path: Path):
    """The catalog-invoke path runs the same replay engine and returns typed JSON."""

    # An extract-values FakeSurface makes the engine return ReplaySuccess with
    # the extracted outputs. The FakeSurface doesn't care about the exact
    # ladder — it just returns the queued extract value.
    surface = FakeSurface(extract_values={0: "$1,234.56", 1: "Active"})
    evidence = EvidenceWriter(tmp_path / "evidence")
    result = asyncio.run(
        invoke_with_surface(
            "published_cap",
            {"member_id": "23456", "operator_password": "demo123"},
            surface,
            evidence,
            options=InvokeOptions(),
            artifacts_dir=temp_registry,
        )
    )
    assert result["status"] == "ok"
    assert result["capability_id"] == "published_cap"
    # Outputs came from the surface, not from anything hardcoded in the catalog.
    assert set(result["outputs"].keys()) == {"checking_balance", "member_status"}


def test_invoke_rejects_missing_required_param(
    temp_registry: Path, tmp_path: Path
):
    surface = FakeSurface()
    evidence = EvidenceWriter(tmp_path / "evidence")
    with pytest.raises(ParamBindingError):
        asyncio.run(
            invoke_with_surface(
                "published_cap",
                {},  # member_id missing
                surface,
                evidence,
                artifacts_dir=temp_registry,
            )
        )


def test_invoke_refuses_draft_capability(
    temp_registry: Path, tmp_path: Path
):
    surface = FakeSurface()
    evidence = EvidenceWriter(tmp_path / "evidence")
    with pytest.raises(CapabilityNotPublished):
        asyncio.run(
            invoke_with_surface(
                "draft_only_cap",
                {"member_id": "23456", "operator_password": "demo123"},
                surface,
                evidence,
                artifacts_dir=temp_registry,
            )
        )


def test_invoke_version_pin_selects_exact_version(tmp_path: Path):
    """Passing version='1.0' pins the exact recording even when 2.0 is approved."""

    root = tmp_path / "artifacts"
    base = _cap_dict_with_id("versioned_cap")
    _write_artifact(root / "versioned_cap", dict(base, version_major=1, version_minor=0))
    _write_artifact(root / "versioned_cap", dict(base, version_major=2, version_minor=0))

    surface = FakeSurface(extract_values={0: "$1.00", 1: "Active"})
    evidence = EvidenceWriter(tmp_path / "evidence")
    result = asyncio.run(
        invoke_with_surface(
            "versioned_cap",
            {"member_id": "23456", "operator_password": "demo123"},
            surface,
            evidence,
            options=InvokeOptions(version="1.0"),
            artifacts_dir=root,
        )
    )
    assert result["version"] == "1.0"


# ---- 4) Secret-sensitivity redaction on both surfaces --------------------


def _secret_param(example: str = "demo123") -> ParamSpec:
    return ParamSpec(
        name="operator_password",
        type="string",
        required=True,
        description="Password for the demo servicing operator.",
        example=example,
        sensitivity="secret",
    )


def _internal_param() -> ParamSpec:
    return ParamSpec(
        name="member_id",
        type="string",
        required=True,
        description="Member ID.",
        example="10001",
        sensitivity="internal",
    )


def test_param_to_json_schema_strips_example_for_secret():
    """Secret example must never make it into the published tool schema."""

    js = param_to_json_schema(_secret_param())
    assert "example" not in js, (
        "secret ParamSpec.example leaked into generated JSON Schema"
    )
    assert js["x-sensitivity"] == "secret"
    assert js["description"]


def test_param_to_json_schema_preserves_example_for_internal():
    """Non-secret examples still ride through — only 'secret' is stripped."""

    js = param_to_json_schema(_internal_param())
    assert js.get("example") == "10001"


def test_build_input_schema_hides_secret_example_from_published_tool(
    temp_registry: Path,
):
    """Full path: list_published → build_tool_definition never publishes secret."""

    caps = list_published(temp_registry)
    cap = next(c for c in caps if c.capability_id == "published_cap")
    td = build_tool_definition(cap)
    secret_prop = td["input_schema"]["properties"]["operator_password"]
    assert "example" not in secret_prop, (
        "secret example leaked into tool_definition.input_schema"
    )
    # The value 'demo123' must not appear anywhere in the serialized tool def.
    assert "demo123" not in json.dumps(td)


# ---- 5) Demo transcript writer redacts model tool_call arguments --------


def test_openai_arguments_redaction_strips_password_from_json_string():
    """demo_agent_invocation._redact_arguments_string must scrub secret fields
    from the raw arguments string OpenAI's chat completion returns."""

    from demo_agent_invocation import _redact_arguments_string

    raw = json.dumps({
        "member_id": "10001",
        "operator_password": "demo123",
    })
    redacted_str = _redact_arguments_string(raw)
    assert "demo123" not in redacted_str, (
        "openai tool_call.arguments still contains raw secret"
    )
    parsed = json.loads(redacted_str)
    assert parsed["operator_password"] == "***REDACTED***"
    assert parsed["member_id"] == "10001"


def test_openai_arguments_redaction_handles_bad_json_gracefully():
    """A malformed arguments string must pass through unchanged (never crash)."""

    from demo_agent_invocation import _redact_arguments_string

    assert _redact_arguments_string("") == ""
    assert _redact_arguments_string("not json {{") == "not json {{"


def test_anthropic_content_redaction_scrubs_tool_use_input():
    """demo_agent_invocation._redact_anthropic_content must scrub tool_use
    blocks' input dict while leaving text blocks and non-secret fields alone."""

    from demo_agent_invocation import _redact_anthropic_content

    content_dump = [
        {"type": "text", "text": "I'll look this up."},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "lookup_member_balance",
            "input": {
                "member_id": "10001",
                "operator_password": "demo123",
            },
        },
    ]
    out = _redact_anthropic_content(content_dump)
    blob = json.dumps(out)
    assert "demo123" not in blob, (
        "anthropic tool_use.input still contains raw secret"
    )
    tool_use = next(b for b in out if b["type"] == "tool_use")
    assert tool_use["input"]["operator_password"] == "***REDACTED***"
    assert tool_use["input"]["member_id"] == "10001"
    # Text block must not be mangled.
    text = next(b for b in out if b["type"] == "text")
    assert text["text"] == "I'll look this up."
