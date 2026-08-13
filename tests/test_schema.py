"""Schema tests — brief test #1: artifact round-trips through JSON and validates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.schema import (
    Capability,
    ExtractSpec,
    LocatorLadder,
    LocatorStrategy,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    Step,
    TargetSpec,
    TextMatchDetector,
    UrlPatternDetector,
    ValueRef,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO_ROOT / "artifacts"
EXAMPLE = ARTIFACTS / "lookup_member_balance.capability.json"
SCHEMA_JSON = ARTIFACTS / "schema.json"


def _example_json() -> str:
    return EXAMPLE.read_text()


def test_example_capability_validates():
    cap = Capability.model_validate_json(_example_json())
    assert cap.capability_id == "lookup_member_balance"
    assert cap.target.tenant_id == "meridian"
    assert cap.schema_version == "1.0"


def test_example_capability_roundtrips_unchanged():
    raw = _example_json()
    cap = Capability.model_validate_json(raw)
    dumped = cap.model_dump_json()
    cap2 = Capability.model_validate_json(dumped)
    assert cap == cap2
    assert cap2.model_dump_json() == dumped


def test_schema_json_committed_matches_current():
    assert SCHEMA_JSON.exists(), "artifacts/schema.json must be committed"
    on_disk = json.loads(SCHEMA_JSON.read_text())
    current = Capability.model_json_schema()
    assert on_disk == current, (
        "artifacts/schema.json is stale — regenerate with "
        "`python -c 'from cua.schema import Capability; import json,pathlib; "
        "pathlib.Path(\"artifacts/schema.json\").write_text(json.dumps("
        "Capability.model_json_schema(), indent=2, sort_keys=True)+chr(10))'`"
    )


def test_invalid_extra_field_raises_clearly():
    data = json.loads(_example_json())
    data["nonsense"] = True
    with pytest.raises(ValidationError, match="nonsense"):
        Capability.model_validate(data)


def test_step_index_mismatch_raises():
    data = json.loads(_example_json())
    data["steps"][2]["index"] = 99
    with pytest.raises(ValidationError, match="steps\\[2\\].index"):
        Capability.model_validate(data)


def test_valueref_to_unknown_param_raises():
    data = json.loads(_example_json())
    data["steps"][4]["value"] = {"param": "not_declared"}
    with pytest.raises(ValidationError, match="not_declared"):
        Capability.model_validate(data)


def test_navigate_step_without_value_raises():
    data = json.loads(_example_json())
    data["steps"][0]["value"] = None
    with pytest.raises(ValidationError, match="navigate"):
        Capability.model_validate(data)


def test_click_step_with_value_raises():
    data = json.loads(_example_json())
    data["steps"][3]["value"] = "extra"
    with pytest.raises(ValidationError, match="click"):
        Capability.model_validate(data)


def test_recorded_match_must_be_in_ladder():
    with pytest.raises(ValidationError, match="recorded_match"):
        LocatorLadder(
            strategies=[LocatorStrategy(kind="css", spec={"selector": "#x"}, durability=40)],
            recorded_match="role_name",
        )


def test_outputspec_carries_no_value_field():
    field_names = set(OutputSpec.model_fields.keys())
    assert field_names == {"name", "type", "description", "extract"}, (
        "OutputSpec must store HOW to extract, never a captured value"
    )
    extract_fields = set(ExtractSpec.model_fields.keys())
    assert "value" not in extract_fields


def test_paramspec_sensitivity_field_present():
    field = ParamSpec.model_fields["sensitivity"]
    assert field is not None
    with pytest.raises(ValidationError):
        ParamSpec(
            name="x",
            type="string",
            required=True,
            description="d",
            example="e",
            sensitivity="top_secret",
        )


def test_outcome_classifications_are_the_declared_three():
    data = json.loads(_example_json())
    classifications = {o["classification"] for o in data["outcomes"]}
    assert classifications == {"business_outcome", "recoverable", "hard_failure"}
    with pytest.raises(ValidationError):
        OutcomeSpec(
            name="mystery",
            detect=TextMatchDetector(contains="x"),
            classification="unclassified",
            terminal=True,
            message="m",
        )


def test_valueref_serializes_as_param_dict():
    step = Step(
        index=0,
        action="type",
        target=LocatorLadder(
            strategies=[LocatorStrategy(kind="label", spec={"label": "X"}, durability=90)],
            recorded_match="label",
        ),
        value=ValueRef(param="member_id"),
    )
    assert step.model_dump()["value"] == {"param": "member_id"}


def test_minimal_capability_from_python_roundtrips():
    ladder = LocatorLadder(
        strategies=[LocatorStrategy(kind="label", spec={"label": "X"}, durability=90)],
        recorded_match="label",
    )
    cap = Capability(
        schema_version="1.0",
        capability_id="probe",
        version=1,
        title="Probe",
        description="Smallest possible capability for tests.",
        target=TargetSpec(
            app_id="probe_app",
            base_url="http://localhost:8080",
            tenant_id="meridian",
            surface_kind="web",
        ),
        inputs=[],
        outputs=[
            OutputSpec(
                name="page_title",
                type="string",
                description="Title of the landing page.",
                extract=ExtractSpec(locator=ladder),
            )
        ],
        steps=[
            Step(
                index=0,
                action="navigate",
                value="http://localhost:8080/",
            )
        ],
        outcomes=[
            OutcomeSpec(
                name="ok",
                detect=UrlPatternDetector(pattern="/"),
                classification="business_outcome",
                terminal=True,
                message="Landed.",
            )
        ],
        approval="draft",
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        recorded_by="human_edit",
    )
    dumped = cap.model_dump_json()
    assert Capability.model_validate_json(dumped) == cap
