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
    OverlayPatch,
    ParamSpec,
    Step,
    TargetSpec,
    TenantOverlay,
    TextMatchDetector,
    UrlPatternDetector,
    ValueRef,
    classify_version_bump,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO_ROOT / "artifacts"
EXAMPLE = ARTIFACTS / "lookup_member_balance" / "v1.0.json"
SCHEMA_JSON = ARTIFACTS / "schema.json"


def _example_json() -> str:
    return EXAMPLE.read_text()


def test_example_capability_validates():
    cap = Capability.model_validate_json(_example_json())
    assert cap.capability_id == "lookup_member_balance"
    assert cap.target.tenant_id == "meridian"
    assert cap.schema_version == "1.0"
    assert (cap.version_major, cap.version_minor) == (1, 0)
    assert cap.target.target_version == "2024.03"


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
        version_major=1,
        version_minor=0,
        title="Probe",
        description="Smallest possible capability for tests.",
        target=TargetSpec(
            app_id="probe_app",
            base_url="http://localhost:8080",
            tenant_id="meridian",
            target_version="2024.03",
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


def _load_example() -> Capability:
    return Capability.model_validate_json(_example_json())


def test_version_bump_minor_when_only_recording_changes():
    """Adding a ladder rung and a recovery rule leaves the caller contract intact."""
    before = _load_example()
    after_data = json.loads(_example_json())
    after_data["steps"][1]["target"]["strategies"].append(
        {"kind": "text", "spec": {"text": "Username"}, "durability": 60}
    )
    after_data["steps"][3]["on_error"].append(
        {
            "on": {"kind": "text_match", "contains": "Please try again"},
            "do": "retry_with_backoff",
            "max_attempts": 2,
        }
    )
    after = Capability.model_validate(after_data)
    assert classify_version_bump(before, after) == "minor"


def test_version_bump_major_when_output_removed():
    """Dropping a declared output reshapes the caller contract."""
    before = _load_example()
    after_data = json.loads(_example_json())
    after_data["outputs"] = [
        o for o in after_data["outputs"] if o["name"] != "member_status"
    ]
    after = Capability.model_validate(after_data)
    assert classify_version_bump(before, after) == "major"


def test_version_bump_major_when_outcome_removed():
    before = _load_example()
    after_data = json.loads(_example_json())
    after_data["outcomes"] = [
        o for o in after_data["outcomes"] if o["name"] != "member_restricted"
    ]
    after = Capability.model_validate(after_data)
    assert classify_version_bump(before, after) == "major"


def test_version_bump_none_when_only_version_numbers_change():
    before = _load_example()
    after_data = json.loads(_example_json())
    after_data["version_minor"] = 7
    after = Capability.model_validate(after_data)
    assert classify_version_bump(before, after) == "none"


def test_tenant_overlay_validates():
    overlay = TenantOverlay(
        overlay_version=1,
        base_capability="lookup_member_balance",
        base_version="2.0",
        tenant_id="summit",
        patches=[
            OverlayPatch(path="target.base_url", value="http://localhost:8080/servicing"),
            OverlayPatch(
                path="steps[1].target.strategies[0].spec.name",
                value="Find Member",
            ),
        ],
    )
    dumped = overlay.model_dump_json()
    assert TenantOverlay.model_validate_json(dumped) == overlay


def test_tenant_overlay_rejects_bad_base_version():
    from pydantic import ValidationError as _VE

    with pytest.raises(_VE, match="base_version"):
        TenantOverlay(
            overlay_version=1,
            base_capability="lookup_member_balance",
            base_version="2",
            tenant_id="summit",
            patches=[OverlayPatch(path="target.base_url", value="x")],
        )


def test_tenant_overlay_requires_at_least_one_patch():
    from pydantic import ValidationError as _VE

    with pytest.raises(_VE):
        TenantOverlay(
            overlay_version=1,
            base_capability="lookup_member_balance",
            base_version="2.0",
            tenant_id="summit",
            patches=[],
        )
