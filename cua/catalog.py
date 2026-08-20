"""Agent-facing capability catalog.

Exposes ``approved`` artifacts from the registry as callable, typed tools:

- ``list_published(artifacts_dir=None)`` — enumerate every capability that has
  at least one approved version on disk; drafts are invisible by construction.
- ``capability_signature(cap)`` — the caller-visible surface (name, version,
  params with types+sensitivity, outputs, declared business outcomes).
- ``build_tool_definition(cap)`` — machine-generate an Anthropic-shaped tool
  definition straight from the Pydantic schema (ParamSpec → JSON Schema,
  description → tool description, OutputSpec → return shape).
- ``invoke(name, params, ...)`` — validate args against ParamSpec, load
  (optionally compose an overlay) and run the deterministic replay engine,
  returning the typed ReplayResult as a JSON-safe dict.
- ``create_app(...)`` — a thin Flask app: ``GET /capabilities`` (published
  only) and ``POST /capabilities/<name>/invoke`` (evidence-producing).

The module deliberately does NOT import Playwright or Anthropic at import
time; the replay path pulls the browser driver only when a real invocation
runs, and demo scripts import the model SDK themselves.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cua.escalate import EscalationBroker
from cua.evidence import EvidenceWriter
from cua.registry import _get_artifacts_dir, _parse_version, compose, load
from cua.replay import (
    ParamBindingError,
    ReplayConfig,
    ReplaySurface,
    bind_params,
    replay_with_surface,
    run_replay,
)
from cua.result import ReplayResult, result_to_dict
from cua.schema import (
    Capability,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    ParamType,
    TenantOverlay,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence"


# ---- Errors -------------------------------------------------------------


class CatalogError(Exception):
    """Base class for catalog-side failures."""


class CapabilityNotPublished(CatalogError):
    """Raised when a caller asks for a capability with no approved version."""


class OverlayNotFound(CatalogError):
    """Raised when a tenant overlay is requested but not present on disk."""


# ---- Discovery on disk --------------------------------------------------


def _artifacts_root(artifacts_dir: Path | str | None) -> Path:
    return Path(artifacts_dir) if artifacts_dir else _get_artifacts_dir()


def _capability_dirs(artifacts_dir: Path) -> list[Path]:
    if not artifacts_dir.exists():
        return []
    return sorted(p for p in artifacts_dir.iterdir() if p.is_dir())


def _version_files(cap_dir: Path) -> list[tuple[str, Path]]:
    """Return ``[(version_str, path), ...]`` sorted ascending by (major, minor)."""

    out: list[tuple[str, Path]] = []
    for item in cap_dir.glob("v*.json"):
        m = re.match(r"v(\d+\.\d+)\.json$", item.name)
        if m:
            out.append((m.group(1), item))
    out.sort(key=lambda t: _parse_version(t[0]))
    return out


def _load_at(path: Path) -> Capability:
    return Capability.model_validate(json.loads(path.read_text()))


def _highest_approved(cap_dir: Path) -> Capability | None:
    """Return the highest-versioned Capability whose approval == 'approved'.

    Returns None if the capability directory contains only drafts (which is
    exactly the drafts-are-invisible guarantee this catalog owes agents).
    """

    for _version, path in reversed(_version_files(cap_dir)):
        cap = _load_at(path)
        if cap.approval == "approved":
            return cap
    return None


def list_published(artifacts_dir: Path | str | None = None) -> list[Capability]:
    """Return the highest-approved Capability for every capability id on disk.

    Drafts-only capabilities are omitted, not marked. This is the whole point
    of the catalog: callers should never see a capability they can't invoke.
    """

    root = _artifacts_root(artifacts_dir)
    caps: list[Capability] = []
    for cap_dir in _capability_dirs(root):
        cap = _highest_approved(cap_dir)
        if cap is not None:
            caps.append(cap)
    caps.sort(key=lambda c: c.capability_id)
    return caps


# ---- Schema → JSON Schema / Anthropic tool ------------------------------


_PARAM_TYPE_TO_JSON: dict[ParamType, dict[str, Any]] = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    # currency is carried as a string ("$1,234.56") in inputs; replay outputs
    # convert to cents via the ExtractSpec transform. Agents supply strings.
    "currency": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "enum": {"type": "string"},
}


def param_to_json_schema(p: ParamSpec) -> dict[str, Any]:
    """Translate one ParamSpec to a JSON Schema property fragment.

    Sensitivity is preserved as ``x-sensitivity`` so agents that render tool
    definitions to humans can highlight PII/secret fields without needing an
    out-of-band lookup.
    """

    js: dict[str, Any] = dict(_PARAM_TYPE_TO_JSON.get(p.type, {"type": "string"}))
    js["description"] = p.description
    js["x-sensitivity"] = p.sensitivity
    # Secret examples must never reach the generated tool schema: the catalog
    # is what agents *see*, so example="demo123" on a secret param would be
    # published straight to callers (and echoed back into transcripts).
    if p.example is not None and p.sensitivity != "secret":
        js["example"] = p.example
    return js


def build_input_schema(cap: Capability) -> dict[str, Any]:
    """Build the JSON Schema for a capability's ParamSpec inputs.

    ``version`` and ``tenant_id`` are declared as optional invocation-side
    controls so callers can pin a major or route to a specific tenant overlay
    without needing a separate endpoint.
    """

    properties: dict[str, Any] = {
        p.name: param_to_json_schema(p) for p in cap.inputs
    }
    properties["version"] = {
        "type": "string",
        "description": (
            "Optional version pin. 'N' picks the highest approved N.x; "
            "'N.M' pins an exact version. Defaults to highest approved."
        ),
    }
    properties["tenant_id"] = {
        "type": "string",
        "description": (
            "Optional tenant id. When set, a matching TenantOverlay under "
            "artifacts/<capability>/overlays/<tenant>.v*.json is composed "
            "onto the base capability before replay."
        ),
    }
    required = [p.name for p in cap.inputs if p.required]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_output_schema(cap: Capability) -> dict[str, Any]:
    """Return the shape of a successful replay's ``outputs`` field.

    The full ReplayResult contract (status, evidence_dir, steps, ...) is
    invariant across capabilities, so we describe only the capability-specific
    outputs here; agents can consume ``status`` and ``outputs`` uniformly.
    """

    props: dict[str, Any] = {}
    for o in cap.outputs:
        base = dict(_PARAM_TYPE_TO_JSON.get(o.type, {"type": "string"}))
        # currency outputs are cents-typed after the currency_to_cents transform.
        if o.type == "currency":
            base = {"type": "integer"}
        base["description"] = o.description
        props[o.name] = base
    return {
        "type": "object",
        "properties": props,
        "required": sorted(props.keys()),
    }


def _business_outcomes(cap: Capability) -> list[dict[str, Any]]:
    return [
        {
            "name": oc.name,
            "message": oc.message,
            "classification": oc.classification,
            "terminal": oc.terminal,
        }
        for oc in cap.outcomes
        if oc.classification == "business_outcome"
    ]


def capability_signature(cap: Capability) -> dict[str, Any]:
    """The public, agent-visible surface of a capability.

    Everything a caller needs to decide whether to invoke, on what inputs,
    with what outputs, and which business outcomes to expect. No locators,
    no step timelines, no recorded selectors.
    """

    return {
        "name": cap.capability_id,
        "title": cap.title,
        "description": cap.description,
        "version": f"{cap.version_major}.{cap.version_minor}",
        "target": {
            "app_id": cap.target.app_id,
            "tenant_id": cap.target.tenant_id,
            "target_version": cap.target.target_version,
            "surface_kind": cap.target.surface_kind,
        },
        "params": [
            {
                "name": p.name,
                "type": p.type,
                "required": p.required,
                "description": p.description,
                "sensitivity": p.sensitivity,
                "example": p.example,
            }
            for p in cap.inputs
        ],
        "outputs": [
            {"name": o.name, "type": o.type, "description": o.description}
            for o in cap.outputs
        ],
        "business_outcomes": _business_outcomes(cap),
    }


def build_tool_definition(cap: Capability) -> dict[str, Any]:
    """Anthropic-shaped tool definition generated from the capability schema.

    Matches the ``tools=[...]`` shape the Messages API accepts: ``name``,
    ``description``, ``input_schema``. ``output_schema`` is included as a
    non-standard hint for calling agents that want to render return types.
    """

    return {
        "name": cap.capability_id,
        "description": (
            f"{cap.title}. {cap.description}"
            f" (v{cap.version_major}.{cap.version_minor}"
            f", tenant={cap.target.tenant_id})"
        ),
        "input_schema": build_input_schema(cap),
        "output_schema": build_output_schema(cap),
    }


# ---- Overlay resolution -------------------------------------------------


_OVERLAY_RE = re.compile(r"^(?P<tenant>[a-z0-9_]+)\.v(?P<n>\d+)\.json$")


def _find_overlay(
    capability_id: str, tenant_id: str, artifacts_dir: Path
) -> Path | None:
    """Return the highest-numbered overlay for ``tenant`` under a capability.

    Overlay filename convention: ``artifacts/<id>/overlays/<tenant>.vN.json``.
    """

    overlays = artifacts_dir / capability_id / "overlays"
    if not overlays.exists():
        return None
    candidates: list[tuple[int, Path]] = []
    for path in overlays.glob("*.json"):
        m = _OVERLAY_RE.match(path.name)
        if m and m.group("tenant") == tenant_id:
            candidates.append((int(m.group("n")), path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def resolve_capability(
    name: str,
    *,
    version: Optional[str] = None,
    tenant_id: Optional[str] = None,
    artifacts_dir: Path | str | None = None,
) -> Capability:
    """Load + optionally overlay a capability for invocation.

    A tenant is honored only if a matching overlay exists AND the tenant
    differs from the base's recorded tenant; asking for the base's own tenant
    is a no-op, not an error.
    """

    root = _artifacts_root(artifacts_dir)
    cap_dir = root / name
    if not cap_dir.exists():
        raise CapabilityNotPublished(f"capability {name!r} not found on disk")
    if _highest_approved(cap_dir) is None:
        raise CapabilityNotPublished(
            f"capability {name!r} has no approved versions (drafts hidden)"
        )
    # registry.load() resolves against the repo's artifacts dir. When the
    # caller overrides ``artifacts_dir`` (tests, alternate registries) we
    # duplicate the resolution logic locally so tests don't have to patch
    # module-level globals in registry.
    if artifacts_dir is None:
        cap = load(name, version)
    else:
        cap = _load_matching_version(cap_dir, version)
    if tenant_id and tenant_id != cap.target.tenant_id:
        overlay_path = _find_overlay(name, tenant_id, root)
        if overlay_path is None:
            raise OverlayNotFound(
                f"no overlay for tenant {tenant_id!r} of {name!r}"
            )
        overlay = TenantOverlay.model_validate(
            json.loads(overlay_path.read_text())
        )
        cap = compose(cap, overlay)
    return cap


def _load_matching_version(
    cap_dir: Path, version: Optional[str]
) -> Capability:
    """Resolve a version spec against a capability directory on disk.

    Mirrors registry.load semantics but scoped to an explicit ``cap_dir`` so
    tests can point at a temporary artifacts root without patching globals.
    """

    versions = _version_files(cap_dir)
    if not versions:
        raise CapabilityNotPublished(f"no version files under {cap_dir}")
    if version is None:
        for _v, path in reversed(versions):
            cap = _load_at(path)
            if cap.approval == "approved":
                return cap
        raise CapabilityNotPublished(f"no approved version under {cap_dir}")
    if "." in version:
        exact = {v: p for v, p in versions}.get(version)
        if exact is None:
            raise CapabilityNotPublished(
                f"version {version!r} not found under {cap_dir}"
            )
        return _load_at(exact)
    major = int(version)
    matching = [(v, p) for v, p in versions if _parse_version(v)[0] == major]
    if not matching:
        raise CapabilityNotPublished(
            f"no {version}.x version under {cap_dir}"
        )
    for _v, path in reversed(matching):
        cap = _load_at(path)
        if cap.approval == "approved":
            return cap
    raise CapabilityNotPublished(
        f"no approved {version}.x version under {cap_dir}"
    )


# ---- Invocation ---------------------------------------------------------


@dataclass
class InvokeOptions:
    """Runtime knobs the HTTP/CLI surfaces expose to callers."""

    version: Optional[str] = None
    tenant_id: Optional[str] = None
    headless: bool = True
    step_timeout_ms: int = 6000
    inject_mode: Optional[str] = None
    inject_after_step: int = 3
    escalation_broker: Optional[EscalationBroker] = None
    escalation_timeout_s: float = 300.0


def _split_options(payload: dict[str, Any]) -> tuple[InvokeOptions, dict[str, Any]]:
    """Peel invocation controls off the top of a params payload.

    Callers post one JSON object; version/tenant/inject_* live at the top
    level (declared in the input schema); everything else is a ParamSpec value.
    """

    opts = InvokeOptions(
        version=payload.get("version"),
        tenant_id=payload.get("tenant_id"),
        headless=bool(payload.get("headless", True)),
        step_timeout_ms=int(payload.get("step_timeout_ms", 6000)),
        inject_mode=payload.get("inject_mode"),
        inject_after_step=int(payload.get("inject_after_step", 3)),
    )
    control_keys = {
        "version", "tenant_id", "headless", "step_timeout_ms",
        "inject_mode", "inject_after_step",
    }
    params = {k: v for k, v in payload.items() if k not in control_keys}
    return opts, params


def _validate_params(cap: Capability, params: dict[str, Any]) -> dict[str, Any]:
    """Bind params through the same ParamSpec gate replay uses.

    Extra keys are dropped (see ``bind_params``); missing required keys raise
    ParamBindingError, which the HTTP/CLI layers translate into a 400.
    """

    return bind_params(cap.inputs, params)


async def invoke_with_surface(
    name: str,
    params: dict[str, Any],
    surface: ReplaySurface,
    evidence: EvidenceWriter,
    *,
    options: InvokeOptions | None = None,
    artifacts_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Invoke against a pre-built Surface. Used by tests and in-process demos.

    Returns the typed ReplayResult serialized through ``result_to_dict`` — the
    same shape the HTTP endpoint returns, so callers can share deserializers.
    """

    opts = options or InvokeOptions()
    cap = resolve_capability(
        name,
        version=opts.version,
        tenant_id=opts.tenant_id,
        artifacts_dir=artifacts_dir,
    )
    bound = _validate_params(cap, params)
    config = ReplayConfig(
        step_timeout_ms=opts.step_timeout_ms,
        inject_mode=opts.inject_mode,
        inject_after_step=opts.inject_after_step,
    )
    result: ReplayResult = await replay_with_surface(
        cap, surface, bound, evidence, config
    )
    return result_to_dict(result)


async def invoke(
    name: str,
    params: dict[str, Any],
    *,
    options: InvokeOptions | None = None,
    evidence_root: Path | str = DEFAULT_EVIDENCE_ROOT,
    artifacts_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Full invocation path: opens a browser via ``run_replay``.

    This is the production surface — the same path a Flask handler runs. The
    replay engine writes an evidence directory under ``evidence_root``; the
    returned dict includes the directory path so callers can attach it to
    their own audit trail.
    """

    opts = options or InvokeOptions()
    cap = resolve_capability(
        name,
        version=opts.version,
        tenant_id=opts.tenant_id,
        artifacts_dir=artifacts_dir,
    )
    bound = _validate_params(cap, params)
    config = ReplayConfig(
        step_timeout_ms=opts.step_timeout_ms,
        inject_mode=opts.inject_mode,
        inject_after_step=opts.inject_after_step,
        escalation_broker=opts.escalation_broker,
        escalation_timeout_s=opts.escalation_timeout_s,
        escalation_goal=cap.title,
    )
    result = await run_replay(
        cap, bound, evidence_root=Path(evidence_root),
        headless=opts.headless, config=config,
    )
    return result_to_dict(result)


# ---- Flask surface ------------------------------------------------------


def create_app(
    *,
    artifacts_dir: Path | str | None = None,
    evidence_root: Path | str = DEFAULT_EVIDENCE_ROOT,
    escalation_broker: EscalationBroker | None = None,
    escalation_timeout_s: float = 300.0,
):
    """Build the tiny agent-facing HTTP surface.

    ``GET /capabilities`` → the signatures + tool defs of every published
    capability (drafts invisible). ``POST /capabilities/<name>/invoke`` →
    validate args, run replay, return the typed result as JSON.

    Flask is imported inside the factory so importing ``cua.catalog`` in
    minimal environments (tests, offline agents) doesn't require Flask.

    ``escalation_broker`` is optional and caller-supplied (this module does
    not construct or own operator-console lifecycle, matching how it already
    takes ``evidence_root``/``artifacts_dir`` rather than owning storage);
    when a broker is passed, every invocation through this app can escalate
    to whichever operator console that broker is wired to.
    """

    from flask import Flask, jsonify, request

    app = Flask(__name__)

    @app.get("/capabilities")
    def _list():
        caps = list_published(artifacts_dir)
        return jsonify(
            {
                "capabilities": [
                    {
                        **capability_signature(c),
                        "tool": build_tool_definition(c),
                    }
                    for c in caps
                ]
            }
        )

    @app.post("/capabilities/<name>/invoke")
    def _invoke(name: str):
        payload = request.get_json(force=True, silent=True) or {}
        opts, params = _split_options(payload)
        # Broker is caller-supplied at app construction (can't arrive over
        # JSON); every invocation through this app shares it.
        opts.escalation_broker = escalation_broker
        opts.escalation_timeout_s = escalation_timeout_s
        try:
            result = asyncio.run(
                invoke(
                    name,
                    params,
                    options=opts,
                    evidence_root=evidence_root,
                    artifacts_dir=artifacts_dir,
                )
            )
        except CapabilityNotPublished as exc:
            return jsonify({"error": "not_found", "detail": str(exc)}), 404
        except OverlayNotFound as exc:
            return jsonify({"error": "no_overlay", "detail": str(exc)}), 404
        except ParamBindingError as exc:
            return jsonify({"error": "bad_params", "detail": str(exc)}), 400
        return jsonify(result)

    return app
