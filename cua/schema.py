"""Capability artifact schema (Pydantic v2).

The Capability is the reusable, typed record a discovery run compiles into and
the replay engine executes against. Everything the engine needs at runtime is
here; nothing captured at record time (values, screenshots, transient state) is
allowed to leak in. See `Build Brief` Phase 2 for the design invariants.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---- Literals -------------------------------------------------------------

ParamType = Literal["string", "integer", "currency", "date", "enum"]
Sensitivity = Literal["public", "internal", "pii", "secret"]
SurfaceKind = Literal["web", "legacy_web", "desktop"]
ActionKind = Literal[
    "navigate", "click", "type", "select", "read", "wait_for", "assert"
]
LocatorKind = Literal[
    "role_name", "label", "text", "table_cell", "css", "coordinates"
]
OutcomeClass = Literal["business_outcome", "recoverable", "hard_failure"]
Approval = Literal["draft", "approved"]
RecordedBy = Literal["llm_discovery", "human_edit"]
RecoveryAction = Literal[
    "dismiss_dialog", "retry_with_backoff", "reauth", "escalate"
]
Transform = Literal["strip", "currency_to_cents", "to_int", "to_lower"]


class _Strict(BaseModel):
    """All artifact models forbid unknown fields — invalid input fails loud."""

    model_config = ConfigDict(extra="forbid")


# ---- Values ---------------------------------------------------------------


class ValueRef(_Strict):
    """A reference to a declared input parameter, resolved at replay time.

    Serialized as ``{"param": "<name>"}``. This is the *only* way a Step can
    carry a runtime-supplied value; everything else must be a literal known at
    record time.
    """

    param: str = Field(min_length=1)


# A Step's value is either a JSON scalar literal or a ValueRef. `bool` precedes
# `int` in left-to-right union resolution because bool is an int subclass.
StepValue = Annotated[
    Union[ValueRef, bool, int, float, str, None],
    Field(union_mode="left_to_right"),
]


# ---- Locator ladder -------------------------------------------------------


class LocatorStrategy(_Strict):
    """One rung of the locator ladder.

    `spec` is intentionally an open dict — its keys depend on `kind` (e.g.
    role_name uses ``{"role": "button", "name": "Search"}``; table_cell uses
    ``{"table_id": "...", "row_header": {...}, "column": "Balance"}``).
    """

    kind: LocatorKind
    spec: dict[str, Any]
    durability: int = Field(ge=0, le=100)


class LocatorLadder(_Strict):
    """Ordered list of strategies + the rung that matched at record time.

    Replay tries `strategies` top-down; a mismatch between `recorded_match` and
    the rung that resolves at replay is the drift signal for later analysis.
    """

    strategies: list[LocatorStrategy] = Field(min_length=1)
    recorded_match: LocatorKind

    @model_validator(mode="after")
    def _recorded_match_in_ladder(self) -> "LocatorLadder":
        kinds = {s.kind for s in self.strategies}
        if self.recorded_match not in kinds:
            raise ValueError(
                f"recorded_match {self.recorded_match!r} is not one of the "
                f"ladder kinds {sorted(kinds)}"
            )
        return self


# ---- Detectors (discriminated union) --------------------------------------


class TextMatchDetector(_Strict):
    kind: Literal["text_match"] = "text_match"
    contains: str = Field(min_length=1)
    case_sensitive: bool = False


class ElementPresentDetector(_Strict):
    kind: Literal["element_present"] = "element_present"
    locator: LocatorLadder


class UrlPatternDetector(_Strict):
    kind: Literal["url_pattern"] = "url_pattern"
    pattern: str = Field(min_length=1)  # regex, matched against page URL


Detector = Annotated[
    Union[TextMatchDetector, ElementPresentDetector, UrlPatternDetector],
    Field(discriminator="kind"),
]


# ---- Steps & recovery -----------------------------------------------------


class Checkpoint(_Strict):
    """Post-step verification the replay engine must observe before moving on."""

    description: str = Field(min_length=1)
    detect: Detector


class RecoveryRule(_Strict):
    """Bounded, explicit recovery. Unbounded retry is a bug."""

    on: Detector
    do: RecoveryAction
    max_attempts: int = Field(ge=1, le=5)


class Step(_Strict):
    index: int = Field(ge=0)
    action: ActionKind
    target: LocatorLadder | None = None
    value: StepValue = None
    checkpoint: Checkpoint | None = None
    on_error: list[RecoveryRule] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _action_shape(self) -> "Step":
        if self.action == "navigate":
            if self.target is not None:
                raise ValueError("navigate step must not have a locator target")
            if self.value is None:
                raise ValueError(
                    "navigate step requires a URL value (literal or ValueRef)"
                )
        else:
            if self.target is None:
                raise ValueError(
                    f"{self.action!r} step requires a locator target"
                )
        if self.action in ("type", "select") and self.value is None:
            raise ValueError(f"{self.action!r} step requires a value")
        if (
            self.action in ("click", "read", "wait_for", "assert")
            and self.value is not None
        ):
            raise ValueError(f"{self.action!r} step must not carry a value")
        return self


# ---- Target / params / outputs / outcomes ---------------------------------


class TargetSpec(_Strict):
    app_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    surface_kind: SurfaceKind


class ParamSpec(_Strict):
    """A declared input.

    `sensitivity` above ``internal`` must never reach logs or evidence — the
    redaction that enforces that lives in Wave 3; this field carries the
    contract.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    required: bool = True
    description: str = Field(min_length=1)
    example: Any
    sensitivity: Sensitivity


class ExtractSpec(_Strict):
    """How to extract an output. Never a captured value."""

    locator: LocatorLadder
    regex: str | None = None
    transform: Transform | None = None


class OutputSpec(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    description: str = Field(min_length=1)
    extract: ExtractSpec


class OutcomeSpec(_Strict):
    """A declared business or failure outcome.

    Classification structurally separates expected business outcomes from
    crashes: a `business_outcome` returns exit code 0 at replay; a
    `hard_failure` does not; a `recoverable` triggers bounded recovery before
    the engine gives up.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    detect: Detector
    classification: OutcomeClass
    terminal: bool
    message: str = Field(min_length=1)


# ---- Capability -----------------------------------------------------------


class Capability(_Strict):
    schema_version: Literal["1.0"]
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    target: TargetSpec
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    approval: Approval
    recorded_at: datetime
    recorded_by: RecordedBy

    @model_validator(mode="after")
    def _cross_field(self) -> "Capability":
        for i, s in enumerate(self.steps):
            if s.index != i:
                raise ValueError(
                    f"steps[{i}].index must be {i}, got {s.index}"
                )
        input_names = {p.name for p in self.inputs}
        for s in self.steps:
            v = s.value
            if isinstance(v, ValueRef) and v.param not in input_names:
                raise ValueError(
                    f"steps[{s.index}].value references unknown param "
                    f"{v.param!r}; declared inputs: {sorted(input_names)}"
                )
        for group, items in (
            ("inputs", self.inputs),
            ("outputs", self.outputs),
            ("outcomes", self.outcomes),
        ):
            names = [x.name for x in items]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate name in {group}: {names}")
        return self
