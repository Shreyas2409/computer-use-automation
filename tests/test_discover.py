"""Tests for the discovery loop's pure/deterministic helpers.

The end-to-end loop is exercised by a live recorded run (see
``evidence/discovery_*``); these tests cover the pieces that don't need
Anthropic or Playwright to run: a11y tree flattening, observation
hashing, capability assembly from a recorded trajectory, ValueRef
substitution, and the write-artifact immutability guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.discover import (
    DiscoveryConfig,
    RecordedStep,
    assemble_capability,
    flatten_a11y_tree,
    format_tree_for_model,
    observation_hash,
    write_artifact,
)
from cua.locate import build_ladder
from cua.schema import Capability, ParamSpec, ValueRef


def _spec(name: str, sensitivity: str = "internal") -> ParamSpec:
    return ParamSpec(
        name=name,
        type="string",
        required=True,
        description=f"{name} value",
        example="example",
        sensitivity=sensitivity,  # type: ignore[arg-type]
    )


def _sample_config(tmp_path: Path | None = None) -> DiscoveryConfig:
    return DiscoveryConfig(
        goal="Look up member balance",
        base_url="http://localhost:8080",
        tenant="meridian",
        capability_id="discovered_member_balance",
        app_id="meridian_servicing_console",
        target_version="2024.03",
        params={"member_id": "10001", "operator_password": "demo123"},
        param_specs={
            "member_id": _spec("member_id", "internal"),
            "operator_password": _spec("operator_password", "secret"),
        },
    )


def _sample_recorded_steps() -> list[RecordedStep]:
    ladder_field = build_ladder(role="textbox", name="Username", css="#u")
    ladder_pw = build_ladder(role="textbox", name="Password", css="#p")
    ladder_signin = build_ladder(role="button", name="Sign In", css="#s")
    ladder_search = build_ladder(role="textbox", name="Member ID", css="#m")
    ladder_search_btn = build_ladder(role="button", name="Search", css="#g")
    ladder_balance = build_ladder(
        role="cell", name="$1,234.56", css="#ctl00_grdAccounts td"
    )
    return [
        RecordedStep(0, "navigate", "navigate", None, "http://localhost:8080/login",
                     url_before="", url_after="http://localhost:8080/login",
                     why="Start at login"),
        RecordedStep(1, "type", "type_text", ladder_field, "demo",
                     role="textbox", name="Username", why="Username",
                     url_before="http://localhost:8080/login",
                     url_after="http://localhost:8080/login"),
        RecordedStep(2, "type", "type_text", ladder_pw, "demo123",
                     role="textbox", name="Password", why="Password",
                     url_before="http://localhost:8080/login",
                     url_after="http://localhost:8080/login"),
        RecordedStep(3, "click", "click", ladder_signin, None,
                     role="button", name="Sign In", why="Submit login",
                     url_before="http://localhost:8080/login",
                     url_after="http://localhost:8080/members"),
        RecordedStep(4, "type", "type_text", ladder_search, "10001",
                     role="textbox", name="Member ID", why="Enter member id",
                     url_before="http://localhost:8080/members",
                     url_after="http://localhost:8080/members"),
        RecordedStep(5, "click", "click", ladder_search_btn, None,
                     role="button", name="Search", why="Search",
                     url_before="http://localhost:8080/members",
                     url_after="http://localhost:8080/members"),
        RecordedStep(6, "read", "read_text", ladder_balance, None,
                     read_value="$1,234.56", role="cell", name="$1,234.56",
                     why="Read balance",
                     url_before="http://localhost:8080/members/10001",
                     url_after="http://localhost:8080/members/10001"),
    ]


def test_flatten_a11y_tree_prunes_junk_and_caps():
    tree = {
        "role": "WebArea", "name": "Page",
        "children": [
            {"role": "button", "name": "Search", "children": []},
            {"role": "generic", "name": "chrome", "children": [
                {"role": "textbox", "name": "Member ID"},
            ]},
        ],
    }
    nodes = flatten_a11y_tree(tree)
    kinds = [n["role"] for n in nodes]
    assert "button" in kinds and "textbox" in kinds
    assert "generic" not in kinds  # pruned
    assert format_tree_for_model(nodes)  # renders non-empty


def test_observation_hash_is_stable_and_url_sensitive():
    tree = [{"role": "button", "name": "Go", "value": None, "depth": 1}]
    h1 = observation_hash("http://localhost/x", tree)
    h2 = observation_hash("http://localhost/x", list(tree))
    h3 = observation_hash("http://localhost/y", tree)
    assert h1 == h2
    assert h1 != h3


def test_assemble_capability_binds_params_and_outputs():
    cfg = _sample_config()
    steps = _sample_recorded_steps()
    proposal = {
        "title": "Lookup Member Balance",
        "description": "Signs in, searches for a member, reads balance.",
        "inputs": [
            {"name": "member_id", "type": "string", "required": True,
             "description": "5-digit member id.", "example": "NNNNN",
             "sensitivity": "internal"},
            {"name": "operator_password", "type": "string", "required": True,
             "description": "Operator password.", "example": "<secret>",
             "sensitivity": "secret"},
        ],
        "outputs": [
            {"name": "balance", "type": "currency",
             "description": "Displayed balance.",
             "from_read_step_index": 6},
        ],
        "outcomes": [
            {"name": "balance_read", "classification": "business_outcome",
             "terminal": True, "message": "Balance read",
             "detector": {"kind": "url_pattern",
                          "pattern": r"/members/\d+"}},
            {"name": "member_not_found", "classification": "business_outcome",
             "terminal": True, "message": "Member not found",
             "detector": {"kind": "text_match",
                          "contains": "No member found"}},
        ],
    }
    cap = assemble_capability(cfg, steps, proposal)
    assert isinstance(cap, Capability)
    assert cap.capability_id == "discovered_member_balance"
    assert {p.name for p in cap.inputs} == {"member_id", "operator_password"}
    # user-supplied ParamSpec sensitivity overrides the model's guess
    pw = next(p for p in cap.inputs if p.name == "operator_password")
    assert pw.sensitivity == "secret"
    # ValueRef substitution: typed values become {"param": name}
    typed_values = [s.value for s in cap.steps if s.action == "type"]
    assert any(isinstance(v, ValueRef) and v.param == "member_id" for v in typed_values)
    assert any(isinstance(v, ValueRef) and v.param == "operator_password" for v in typed_values)
    # Output extract references the recorded read step's ladder
    assert len(cap.outputs) == 1
    assert cap.outputs[0].extract.locator.recorded_match == "role_name"
    # Outcomes were assembled with the right detector kinds
    kinds = {o.detect.kind for o in cap.outcomes}
    assert {"text_match", "url_pattern"} <= kinds
    # Round-trip through JSON — schema validates the whole thing
    round_tripped = Capability.model_validate_json(cap.model_dump_json())
    assert round_tripped == cap


def test_write_artifact_refuses_to_overwrite(tmp_path: Path):
    cfg = _sample_config()
    steps = _sample_recorded_steps()
    proposal = {
        "title": "T", "description": "D",
        "inputs": [{"name": "member_id", "type": "string", "required": True,
                    "description": "d", "example": "N", "sensitivity": "internal"}],
        "outputs": [],
        "outcomes": [{"name": "ok", "classification": "business_outcome",
                      "terminal": True, "message": "ok",
                      "detector": {"kind": "text_match", "contains": "ok"}}],
    }
    cap = assemble_capability(cfg, steps, proposal)
    path = write_artifact(cap, tmp_path)
    assert path.exists() and path.read_text().startswith("{")
    with pytest.raises(FileExistsError):
        write_artifact(cap, tmp_path)
