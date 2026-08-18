"""LLM tool-use discovery loop.

The model drives a real UI through a small set of a11y-first tools; every
tool call passes the PolicyGate before Playwright executes; each successful
action is recorded with a full locator ladder built from the resolved
element. On `finish`, one more model call proposes the caller-visible
contract (inputs / outputs / outcomes) which we assemble into a Capability
and validate through Pydantic (`model proposes, schema disposes`). The
artifact is born ``draft`` and written to the immutable registry layout
under ``artifacts/<id>/v1.0.json``; the full transcript + per-step
screenshots go under ``evidence/discovery_<id>/``.

The loop runs against either provider through :class:`cua.llm.ProviderClient`
(Anthropic Messages or OpenAI Chat Completions function-calling); the
concrete SDK is imported lazily by the adapter. Playwright is imported
here; ``cua/replay.py`` must never import Playwright, Anthropic, or OpenAI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.evidence import EvidenceWriter
from cua.llm import (
    ProviderClient,
    Turn,
    default_model_for,
    make_provider_client,
    select_provider,
)
from cua.locate import build_ladder
from cua.policy import Action, PolicyGate
from cua.schema import (
    Capability,
    Checkpoint,
    ElementPresentDetector,
    ExtractSpec,
    LocatorLadder,
    OutcomeSpec,
    OutputSpec,
    ParamSpec,
    Step,
    TargetSpec,
    TextMatchDetector,
    UrlPatternDetector,
    ValueRef,
)

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-5")
MAX_STEPS = 25
MAX_UNCHANGED_OBS = 3
DEFAULT_TIMEOUT_SECONDS = 300
PLAYWRIGHT_ACTION_TIMEOUT_MS = 8000

_ACTION_MAP = {
    "navigate": "navigate",
    "click": "click",
    "type_text": "type",
    "select_option": "select",
    "read_text": "read",
    "wait_for": "wait_for",
}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": (
            "Navigate the browser to a URL. Use only for URLs that appear in "
            "the accessibility tree (link hrefs) or the base URL provided in "
            "the goal. Do not invent URLs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["url", "why"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click a button, link, checkbox, or other interactive control. "
            "Provide the accessibility role and the visible / accessible "
            "name straight from the accessibility tree. Never write CSS."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["role", "name", "why"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text into a text field. `role` is the input's a11y role "
            "(usually 'textbox'); `name` is its accessible name (its label)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "text": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["role", "name", "text", "why"],
        },
    },
    {
        "name": "select_option",
        "description": (
            "Choose an option from a dropdown / <select>. Provide the "
            "field's a11y role ('combobox'), its accessible name (label), "
            "and the visible option label."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "option": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["role", "name", "option", "why"],
        },
    },
    {
        "name": "read_text",
        "description": (
            "Read the visible text content of an element (useful for "
            "balances, reference numbers, statuses). Records how to extract "
            "the value at replay; does not persist the value itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["role", "name", "why"],
        },
    },
    {
        "name": "wait_for",
        "description": (
            "Wait up to 10s for an element to appear. Use before reading a "
            "value that only appears after a navigation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["role", "name", "why"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Call once the goal is achieved. `summary` is a one-sentence "
            "description of the final observable outcome (what confirmation "
            "appeared, what value was read, etc)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


# ---- Trajectory records --------------------------------------------------


@dataclass
class RecordedStep:
    """One executed action captured for post-hoc capability compilation."""

    index: int
    action: str  # schema ActionKind
    tool_name: str
    ladder: LocatorLadder | None
    value: Any
    read_value: str | None = None  # only for read_text; never persisted raw
    role: str | None = None
    name: str | None = None
    why: str = ""
    url_before: str = ""
    url_after: str = ""


@dataclass
class DiscoveryOutcome:
    """Terminal state of the discovery loop."""

    status: str  # "finished" | "max_steps" | "stuck" | "timeout" | "error"
    summary: str = ""
    error: str | None = None


# ---- Accessibility helpers -----------------------------------------------


_MAX_TREE_NODES = 200
_INTERESTING_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "combobox",
        "checkbox",
        "radio",
        "heading",
        "row",
        "cell",
        "columnheader",
        "rowheader",
        "menuitem",
        "tab",
        "listbox",
        "option",
        "alert",
        "form",
        "dialog",
        "table",
    }
)


def flatten_a11y_tree(root: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten a Playwright a11y snapshot into a list of interesting nodes.

    Playwright's snapshot with ``interesting_only=True`` already prunes most
    layout junk; we additionally drop non-interactive text-only nodes and
    cap the total to keep the model prompt small and stable.
    """

    if not root:
        return []
    out: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], depth: int) -> None:
        role = node.get("role", "")
        name = (node.get("name") or "").strip()
        value = node.get("value")
        if role in _INTERESTING_ROLES or (role == "text" and name):
            out.append(
                {
                    "role": role,
                    "name": name[:120],
                    "value": (str(value)[:80] if value else None),
                    "depth": min(depth, 8),
                }
            )
        for child in node.get("children", []) or []:
            if len(out) >= _MAX_TREE_NODES:
                return
            walk(child, depth + 1)

    walk(root, 0)
    return out[:_MAX_TREE_NODES]


def format_tree_for_model(nodes: list[dict[str, Any]]) -> str:
    """Human-readable rendering of the trimmed a11y tree for the model."""

    lines: list[str] = []
    for n in nodes:
        indent = "  " * n["depth"]
        name = f' "{n["name"]}"' if n["name"] else ""
        val = f' = "{n["value"]}"' if n["value"] else ""
        lines.append(f'{indent}[{n["role"]}]{name}{val}')
    return "\n".join(lines) if lines else "(empty)"


def observation_hash(url: str, tree_nodes: list[dict[str, Any]]) -> str:
    """Stable hash over url + trimmed tree for stuck detection."""

    payload = json.dumps(
        {"url": url, "tree": tree_nodes}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()




# ---- Element resolution + ladder construction ---------------------------


async def _find_label_text(page, element_id: str | None) -> str | None:
    """Return the text of a ``<label for="{element_id}">`` if present."""

    if not element_id:
        return None
    try:
        lbl = await page.query_selector(f'label[for="{element_id}"]')
        if not lbl:
            return None
        text = (await lbl.text_content()) or ""
        return text.strip() or None
    except Exception:
        return None


async def resolve_element_and_ladder(
    page, role: str, name: str
) -> tuple[Any, LocatorLadder]:
    """Resolve a role+name descriptor on the page and build a full ladder.

    Uses Playwright's ``get_by_role`` (accessible-name aware). Waits for the
    element to be visible so the resolution is deterministic. The returned
    ladder includes every rung we can derive from the resolved DOM element:
    role+name, associated label, visible text, id-based CSS, and bounding
    box center coordinates.
    """

    try:
        locator = page.get_by_role(role, name=name, exact=True).first
        await locator.wait_for(
            state="visible", timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS
        )
    except Exception:
        locator = page.get_by_role(role, name=name).first
        await locator.wait_for(
            state="visible", timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS
        )

    handle = await locator.element_handle()
    element_id = None
    tag = None
    text_content = None
    coords = None
    if handle is not None:
        try:
            element_id = await handle.get_attribute("id")
        except Exception:
            element_id = None
        try:
            tag = await handle.evaluate("(el) => el.tagName.toLowerCase()")
        except Exception:
            tag = None
        try:
            raw_text = await handle.text_content()
            text_content = (raw_text or "").strip() or None
            if text_content and len(text_content) > 80:
                text_content = text_content[:80]
        except Exception:
            text_content = None
        try:
            box = await handle.bounding_box()
            if box:
                coords = (
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2),
                )
        except Exception:
            coords = None

    label_text = await _find_label_text(page, element_id)
    css = f"#{element_id}" if element_id else (tag if tag else None)

    ladder = build_ladder(
        role=role,
        name=name,
        label_text=label_text,
        text=text_content,
        css=css,
        coordinates=coords,
    )
    return locator, ladder


# ---- Discovery driver ---------------------------------------------------


@dataclass
class DiscoveryConfig:
    goal: str
    base_url: str
    tenant: str
    capability_id: str
    app_id: str
    target_version: str
    params: dict[str, str] = field(default_factory=dict)
    param_specs: dict[str, ParamSpec] = field(default_factory=dict)
    surface_kind: str = "legacy_web"
    max_steps: int = MAX_STEPS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    model: str = DEFAULT_MODEL
    provider: str = "anthropic"


class DiscoverySession:
    """Runs one tool-use loop against a live Playwright page."""

    def __init__(
        self,
        page,
        client: ProviderClient,
        config: DiscoveryConfig,
        evidence: EvidenceWriter,
    ) -> None:
        self.page = page
        self.client = client
        self.config = config
        self.evidence = evidence
        self.steps: list[RecordedStep] = []
        self.obs_hashes: list[str] = []
        self.transcript: list[dict[str, Any]] = []
        self.step_screenshots: list[Path] = []
        self.started = time.monotonic()

    # ---- observation -----------------------------------------------------

    async def observe(self) -> tuple[str, list[dict[str, Any]], bytes]:
        try:
            snapshot = await self.page.accessibility.snapshot(
                interesting_only=True
            )
        except Exception:
            snapshot = None
        nodes = flatten_a11y_tree(snapshot)
        try:
            screenshot = await self.page.screenshot()
        except Exception:
            screenshot = b""
        return self.page.url, nodes, screenshot

    def _policy_check(self, verb: str, url: str, value: Any) -> str:
        action = Action(
            verb=verb,
            url=url,
            value=str(value) if value is not None else None,
        )
        return PolicyGate.check(action)


    # ---- tool execution -------------------------------------------------

    async def execute_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        """Execute one model-issued tool call. Returns tool_result text."""

        current_url = self.page.url
        verb = tool_input.get("name", "") if tool_name == "click" else tool_name
        if tool_name == "click":
            verb = tool_input.get("name", "click")
        elif tool_name == "type_text":
            verb = "type"
        elif tool_name == "select_option":
            verb = "select"
        elif tool_name == "read_text":
            verb = "read"
        elif tool_name == "navigate":
            verb = "navigate"
        elif tool_name == "wait_for":
            verb = "wait_for"

        target_url = tool_input.get("url", current_url)
        value = tool_input.get("text") or tool_input.get("option")
        verdict = self._policy_check(verb, target_url, value)
        if verdict == "block":
            self.evidence.log(
                phase="discovery",
                action=tool_name,
                message="policy_blocked",
                metadata={"verb": verb, "url": target_url},
            )
            return f"POLICY_BLOCKED: verb={verb!r} on {target_url!r}"
        if verdict == "require_confirmation":
            self.evidence.log(
                phase="discovery",
                action=tool_name,
                message="policy_confirmation_auto_deny",
                metadata={"verb": verb, "url": target_url},
            )
            # In discovery we treat confirmation-required as a soft block —
            # the model should choose a non-destructive path or finish.
            return (
                f"POLICY_REQUIRES_CONFIRMATION: verb={verb!r}; try a "
                "non-destructive path or call finish."
            )

        try:
            return await self._dispatch_tool(tool_name, tool_input)
        except Exception as exc:
            self.evidence.log(
                phase="discovery",
                action=tool_name,
                error=str(exc)[:200],
                message="tool_execution_failed",
            )
            return f"ERROR: {type(exc).__name__}: {str(exc)[:200]}"

    async def _dispatch_tool(
        self, tool_name: str, tool_input: dict[str, Any]
    ) -> str:
        url_before = self.page.url
        why = tool_input.get("why", "")

        if tool_name == "navigate":
            url = tool_input["url"]
            await self.page.goto(url, wait_until="domcontentloaded")
            self._record_step(
                action="navigate",
                tool_name="navigate",
                ladder=None,
                value=url,
                role=None,
                name=None,
                why=why,
                url_before=url_before,
            )
            return f"Navigated to {self.page.url}"

        role = tool_input["role"]
        name = tool_input["name"]
        locator, ladder = await resolve_element_and_ladder(
            self.page, role, name
        )

        if tool_name == "click":
            await locator.click(timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
            try:
                await self.page.wait_for_load_state(
                    "domcontentloaded", timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS
                )
            except Exception:
                pass
            self._record_step(
                action="click", tool_name="click", ladder=ladder,
                value=None, role=role, name=name, why=why,
                url_before=url_before,
            )
            return f"Clicked {role} {name!r}; url now {self.page.url}"

        if tool_name == "type_text":
            text = tool_input["text"]
            await locator.fill(text, timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
            self._record_step(
                action="type", tool_name="type_text", ladder=ladder,
                value=text, role=role, name=name, why=why,
                url_before=url_before,
            )
            return f"Typed into {role} {name!r} (shape: len={len(text)})"

        if tool_name == "select_option":
            option = tool_input["option"]
            await locator.select_option(
                label=option, timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS
            )
            self._record_step(
                action="select", tool_name="select_option", ladder=ladder,
                value=option, role=role, name=name, why=why,
                url_before=url_before,
            )
            return f"Selected {option!r} in {role} {name!r}"

        if tool_name == "read_text":
            raw = await locator.text_content(
                timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS
            )
            text = (raw or "").strip()
            self._record_step(
                action="read", tool_name="read_text", ladder=ladder,
                value=None, role=role, name=name, why=why,
                url_before=url_before, read_value=text,
            )
            # Feed the model the observed text so it can decide what to do
            # next, but truncate defensively.
            return f"Read {role} {name!r}: {text[:300]!r}"

        if tool_name == "wait_for":
            self._record_step(
                action="wait_for", tool_name="wait_for", ladder=ladder,
                value=None, role=role, name=name, why=why,
                url_before=url_before,
            )
            return f"Waited for {role} {name!r}; visible"

        raise ValueError(f"Unknown tool: {tool_name}")

    def _record_step(
        self,
        *,
        action: str,
        tool_name: str,
        ladder: LocatorLadder | None,
        value: Any,
        role: str | None,
        name: str | None,
        why: str,
        url_before: str,
        read_value: str | None = None,
    ) -> None:
        step = RecordedStep(
            index=len(self.steps),
            action=action,
            tool_name=tool_name,
            ladder=ladder,
            value=value,
            read_value=read_value,
            role=role,
            name=name,
            why=why,
            url_before=url_before,
            url_after=self.page.url,
        )
        self.steps.append(step)
        # Redact params-declared values (e.g. secret passwords) before logging.
        redacted_value = value
        if isinstance(value, str):
            for pname, pval in self.config.params.items():
                if value == pval:
                    spec = self.config.param_specs.get(pname)
                    if spec is not None:
                        redacted_value = EvidenceWriter.redact_param_value(
                            spec, value
                        )
                    else:
                        redacted_value = f"<param:{pname}>"
                    break
        self.evidence.log(
            phase="discovery",
            action=action,
            step_index=step.index,
            message=f"{tool_name} {role or ''} {name or ''}".strip(),
            metadata={
                "why": why[:200],
                "value_shape": (
                    str(redacted_value) if redacted_value is not None else None
                ),
                "url_before": url_before,
                "url_after": self.page.url,
            },
        )


    # ---- main loop ------------------------------------------------------

    def _system_prompt(self) -> str:
        params_hint = ", ".join(
            f"{k}={'<redacted>' if self.config.param_specs.get(k) and self.config.param_specs[k].sensitivity in ('pii','secret') else v!r}"
            for k, v in self.config.params.items()
        )
        return (
            "You drive a legacy internal web app through a small set of "
            "accessibility-tree-first tools. You are DISCOVERING how to "
            "accomplish the goal; a separate deterministic replay engine "
            "will re-run whatever you succeed at.\n\n"
            "Rules:\n"
            "- Perceive from the [ROLE] \"name\" a11y tree in each "
            "observation. Do NOT invent element names.\n"
            "- Never write CSS selectors — pass a11y `role` and `name` and "
            "the surrounding code builds durable locators for you.\n"
            "- Prefer the most direct path. Do not explore unrelated pages.\n"
            "- Use the parameter values you have been given for anything the "
            "goal treats as an input; do not make up new members / amounts.\n"
            "- When the goal is achieved (a confirmation, a reference "
            "number, a read balance, etc.) call `finish` with a one-line "
            "summary of what confirms success.\n"
            f"- Available parameters (name=value): {params_hint or '(none)'}"
        )

    def _user_turn(
        self,
        url: str,
        tree_text: str,
        step_no: int,
        last_tool_result: str | None,
    ) -> str:
        remaining = max(0, self.config.max_steps - step_no)
        history = "\n".join(
            f"  {s.index}. {s.action} {s.role or ''} {s.name or ''} "
            f"— {s.why[:80]}"
            for s in self.steps[-6:]
        )
        parts = [
            f"GOAL: {self.config.goal}",
            f"CURRENT URL: {url}",
            f"STEP: {step_no + 1} / {self.config.max_steps}  (remaining: {remaining})",
            "",
            "ACCESSIBILITY TREE (interesting nodes only):",
            tree_text,
        ]
        if history:
            parts.extend(["", "RECENT STEPS:", history])
        if last_tool_result:
            parts.extend(["", "LAST TOOL RESULT:", last_tool_result[:800]])
        parts.extend([
            "",
            "Decide the next tool call. If the goal is complete, call `finish`.",
        ])
        return "\n".join(parts)

    async def run(self) -> DiscoveryOutcome:
        last_tool_result: str | None = None
        outcome = DiscoveryOutcome(status="max_steps")

        self.client.start(self._system_prompt(), TOOLS)

        for step_no in range(self.config.max_steps):
            if time.monotonic() - self.started > self.config.timeout_seconds:
                outcome = DiscoveryOutcome(status="timeout")
                break

            url, nodes, screenshot = await self.observe()
            self.step_screenshots.append(
                self.evidence.write_screenshot(
                    screenshot, name=f"step_{step_no:02d}.png"
                )
            )
            oh = observation_hash(url, nodes)
            self.obs_hashes.append(oh)
            if (
                len(self.obs_hashes) >= MAX_UNCHANGED_OBS
                and len(set(self.obs_hashes[-MAX_UNCHANGED_OBS:])) == 1
            ):
                self.evidence.log(
                    phase="discovery",
                    message="stuck: observation hash unchanged",
                    metadata={"repeats": MAX_UNCHANGED_OBS},
                )
                outcome = DiscoveryOutcome(status="stuck")
                break

            tree_text = format_tree_for_model(nodes)
            user_text = self._user_turn(
                url, tree_text, step_no, last_tool_result
            )
            self.transcript.append(
                {"role": "user", "step": step_no, "text": user_text}
            )

            turn: Turn = self.client.turn(user_text, max_tokens=1024)
            self.transcript.append(
                {
                    "role": "assistant",
                    "step": step_no,
                    "stop_reason": turn.stop_reason,
                    **turn.assistant_serialized,
                }
            )

            if not turn.tool_calls:
                # Model chose not to call a tool; nudge once, then bail.
                last_tool_result = (
                    "NOTE: You must call exactly one tool per turn. "
                    "Either call a tool that advances the goal, or call finish."
                )
                continue

            # Handle first tool call per turn (single-turn tool loop is enough
            # for this legacy synchronous app).
            tu = turn.tool_calls[0]
            if tu.name == "finish":
                outcome = DiscoveryOutcome(
                    status="finished",
                    summary=str(tu.input.get("summary", ""))[:400],
                )
                self.client.add_tool_result(tu.id, "finished")
                self.evidence.log(
                    phase="discovery",
                    message="finish",
                    metadata={"summary": outcome.summary},
                )
                break

            tool_result = await self.execute_tool(tu.name, dict(tu.input))
            last_tool_result = tool_result
            self.client.add_tool_result(tu.id, tool_result)

        return outcome



# ---- Capability compilation ---------------------------------------------


_COMPILE_SYSTEM = (
    "You are compiling a SUCCESSFUL discovery run into a reusable capability "
    "contract for a calling agent. You propose fields; a strict JSON schema "
    "validates them (`model proposes, schema disposes`). Return ONLY a JSON "
    "object matching the requested keys. No prose, no markdown fences."
)


def _detector_from_proposal(det: dict[str, Any]) -> Any:
    """Turn the compact detector shape the model returns into a schema Detector."""

    kind = det.get("kind")
    if kind == "text_match":
        return TextMatchDetector(
            contains=str(det["contains"])[:200],
            case_sensitive=bool(det.get("case_sensitive", False)),
        )
    if kind == "url_pattern":
        return UrlPatternDetector(pattern=str(det["pattern"])[:200])
    if kind == "element_present":
        raise ValueError(
            "element_present detectors are not accepted from the model in "
            "compilation; declare via text_match or url_pattern instead."
        )
    raise ValueError(f"unknown detector kind: {kind!r}")


def _apply_value_ref(
    literal: Any, params: dict[str, str]
) -> Any:
    """Replace a literal step value with a ValueRef if it matches a param."""

    if not isinstance(literal, str) or literal == "":
        return literal
    for pname, pval in params.items():
        if literal == pval:
            return ValueRef(param=pname)
    return literal


def _steps_from_recorded(
    recorded: list[RecordedStep], params: dict[str, str]
) -> list[Step]:
    steps: list[Step] = []
    for i, r in enumerate(recorded):
        target = r.ladder
        value = _apply_value_ref(r.value, params)
        # `read` and `wait_for` do not carry a value in the schema.
        if r.action in ("read", "wait_for", "click"):
            value = None
        if r.action == "navigate":
            # Navigate carries the URL as its value; ValueRef only if user
            # explicitly supplied it as a param (rare — usually literal).
            value = _apply_value_ref(r.value, params)
        checkpoint = None
        if r.url_after and r.url_after != r.url_before and r.action != "navigate":
            checkpoint = Checkpoint(
                description=f"URL advanced to {r.url_after}",
                detect=UrlPatternDetector(
                    pattern=re.escape(_url_path(r.url_after))
                ),
            )
        steps.append(
            Step(
                index=i,
                action=r.action,  # type: ignore[arg-type]
                target=target if r.action != "navigate" else None,
                value=value,
                checkpoint=checkpoint,
                on_error=[],
                notes=r.why[:200],
            )
        )
    return steps


def _url_path(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).path or "/"
    except Exception:
        return "/"


def _proposal_prompt(
    goal: str,
    params: dict[str, str],
    param_specs: dict[str, ParamSpec],
    recorded: list[RecordedStep],
) -> str:
    param_lines = []
    for name, val in params.items():
        spec = param_specs.get(name)
        sens = spec.sensitivity if spec else "internal"
        shown = (
            EvidenceWriter.redact_param_value(spec, val)
            if spec and spec.sensitivity in ("pii", "secret", "internal")
            else val
        )
        param_lines.append(f"  - {name} ({sens}): {shown}")
    step_lines = []
    for r in recorded:
        if r.action == "type" and isinstance(r.value, str):
            marker = next(
                (
                    f"<param:{p}>"
                    for p, v in params.items()
                    if v == r.value
                ),
                f"len={len(r.value)}",
            )
            step_lines.append(
                f"  {r.index}. type into [{r.role} \"{r.name}\"] "
                f"value={marker} — {r.why[:80]}"
            )
        elif r.action == "select" and isinstance(r.value, str):
            step_lines.append(
                f"  {r.index}. select {r.value!r} in [{r.role} \"{r.name}\"] "
                f"— {r.why[:80]}"
            )
        elif r.action == "read":
            step_lines.append(
                f"  {r.index}. read [{r.role} \"{r.name}\"] "
                f"= {(r.read_value or '')[:80]!r} — {r.why[:80]}"
            )
        else:
            step_lines.append(
                f"  {r.index}. {r.action} [{r.role or ''} \"{r.name or ''}\"] "
                f"— {r.why[:80]}"
            )
    return (
        f"GOAL: {goal}\n\n"
        f"PARAMETER VALUES USED (sensitivity-tagged; do not echo secrets):\n"
        f"{chr(10).join(param_lines) or '  (none)'}\n\n"
        f"RECORDED TRAJECTORY ({len(recorded)} steps):\n"
        f"{chr(10).join(step_lines)}\n\n"
        "Return a single JSON object with EXACTLY these keys:\n"
        "  title: str  (short human title)\n"
        "  description: str  (2-4 sentences, written for a CALLING agent)\n"
        "  inputs: [ {name, type, required, description, example, sensitivity} ]\n"
        "     — one entry per parameter above; type in "
        "{string,integer,currency,date,enum}; sensitivity in "
        "{public,internal,pii,secret}. `example` MUST be a placeholder, "
        "never the real value.\n"
        "  outputs: [ {name, type, description, from_read_step_index} ]\n"
        "     — declare 0..N outputs; each must reference the index of a "
        "`read` step in the trajectory that captured this value.\n"
        "  outcomes: [ {name, classification, terminal, message, detector} ]\n"
        "     — declare at least one business_outcome for success and one "
        "for the empirically-known failure mode (e.g. member_not_found). "
        "classification in {business_outcome, recoverable, hard_failure}; "
        "detector is {kind: 'text_match', contains: '...'} or "
        "{kind: 'url_pattern', pattern: '...'} (regex).\n"
    )


def compile_capability(
    client: ProviderClient,
    config: DiscoveryConfig,
    recorded: list[RecordedStep],
    outcome: DiscoveryOutcome,
    schema_version: str = "1.0",
) -> Capability:
    """Ask the model for the caller contract, then assemble+validate a Capability."""

    if not recorded:
        raise ValueError("Cannot compile a capability from an empty trajectory")

    proposal_text = _proposal_prompt(
        config.goal, config.params, config.param_specs, recorded
    )
    raw = client.compile_json(
        system=_COMPILE_SYSTEM, user_text=proposal_text, max_tokens=2048
    )
    raw = _strip_code_fence(raw)
    try:
        proposal = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Compilation proposal was not JSON: {exc}: {raw[:300]}")

    return assemble_capability(config, recorded, proposal, schema_version)


def _strip_code_fence(text: str) -> str:
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


def assemble_capability(
    config: DiscoveryConfig,
    recorded: list[RecordedStep],
    proposal: dict[str, Any],
    schema_version: str = "1.0",
) -> Capability:
    """Assemble a Capability from recorded steps + model's contract proposal.

    Split out from ``compile_capability`` so tests can exercise the
    schema-side logic without hitting the model.
    """

    # Inputs — only declare ones we have a value for, and prefer the
    # user-supplied ParamSpec sensitivity over the model's guess.
    inputs: list[ParamSpec] = []
    for i in proposal.get("inputs", []):
        pname = i["name"]
        if pname not in config.params:
            continue
        override = config.param_specs.get(pname)
        inputs.append(
            ParamSpec(
                name=pname,
                type=i.get("type", "string"),
                required=bool(i.get("required", True)),
                description=str(i.get("description", pname))[:200],
                example=i.get("example", "example"),
                sensitivity=(
                    override.sensitivity
                    if override is not None
                    else i.get("sensitivity", "internal")
                ),
            )
        )
    # Ensure every user-supplied param appears in inputs (fill defaults).
    declared = {p.name for p in inputs}
    for pname, spec in config.param_specs.items():
        if pname not in declared:
            inputs.append(spec)

    # Outputs — build ExtractSpec from the referenced read step's ladder.
    outputs: list[OutputSpec] = []
    for o in proposal.get("outputs", []):
        idx = o.get("from_read_step_index")
        if idx is None or not (0 <= idx < len(recorded)):
            continue
        rec = recorded[idx]
        if rec.action != "read" or rec.ladder is None:
            continue
        extract = ExtractSpec(
            locator=rec.ladder,
            regex=(o.get("regex") or None),
            transform=(o.get("transform") or None),
        )
        outputs.append(
            OutputSpec(
                name=o["name"],
                type=o.get("type", "string"),
                description=str(o.get("description", o["name"]))[:200],
                extract=extract,
            )
        )

    # Outcomes.
    outcomes: list[OutcomeSpec] = []
    for oc in proposal.get("outcomes", []):
        outcomes.append(
            OutcomeSpec(
                name=oc["name"],
                detect=_detector_from_proposal(oc["detector"]),
                classification=oc.get("classification", "business_outcome"),
                terminal=bool(oc.get("terminal", True)),
                message=str(oc.get("message", oc["name"]))[:200],
            )
        )

    steps = _steps_from_recorded(recorded, config.params)

    cap = Capability(
        schema_version=schema_version,  # type: ignore[arg-type]
        capability_id=config.capability_id,
        version_major=1,
        version_minor=0,
        title=str(proposal.get("title", config.capability_id))[:120],
        description=str(
            proposal.get("description", config.goal)
        )[:2000],
        target=TargetSpec(
            app_id=config.app_id,
            base_url=config.base_url,
            tenant_id=config.tenant,
            target_version=config.target_version,
            surface_kind=config.surface_kind,  # type: ignore[arg-type]
        ),
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        outcomes=outcomes,
        approval="draft",
        recorded_at=datetime.now(timezone.utc),
        recorded_by="llm_discovery",
    )
    return cap


# ---- Artifact / evidence writing ----------------------------------------


def write_artifact(cap: Capability, artifacts_root: Path) -> Path:
    """Write ``artifacts/<id>/v<major>.<minor>.json`` (never overwrite)."""

    cap_dir = artifacts_root / cap.capability_id
    cap_dir.mkdir(parents=True, exist_ok=True)
    path = cap_dir / f"v{cap.version_major}.{cap.version_minor}.json"
    if path.exists():
        raise FileExistsError(
            f"Refusing to overwrite immutable artifact: {path}"
        )
    path.write_text(cap.model_dump_json(indent=2))
    return path


def write_transcript(session: DiscoverySession, path: Path) -> Path:
    path.write_text(json.dumps(session.transcript, indent=2, default=str))
    return path


# ---- Top-level runner ---------------------------------------------------


async def run_discovery(
    config: DiscoveryConfig,
    *,
    artifacts_root: Path,
    evidence_root: Path,
    headless: bool = True,
    do_not_write: bool = False,
) -> dict[str, Any]:
    """Start a browser, run the loop, compile the capability, write outputs.

    Returns a dict with ``artifact_path``, ``evidence_dir``, ``outcome``.
    """

    # Import here so importing cua.discover doesn't require Playwright at
    # test time when we only exercise compilation.
    from playwright.async_api import async_playwright  # noqa: WPS433

    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = evidence_root / f"discovery_{config.capability_id}_{session_id}"
    evidence = EvidenceWriter(evidence_dir)
    evidence.log(
        phase="discovery",
        message="session_start",
        metadata={
            "goal": config.goal[:400],
            "base_url": config.base_url,
            "tenant": config.tenant,
            "capability_id": config.capability_id,
            "model": config.model,
            "provider": config.provider,
            "max_steps": config.max_steps,
            "param_names": sorted(config.params.keys()),
        },
    )

    client = make_provider_client(config.provider, config.model)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="UTC",
            )
            page = await context.new_page()
            session = DiscoverySession(page, client, config, evidence)
            outcome = await session.run()

            transcript_path = write_transcript(
                session, evidence_dir / "transcript.json"
            )

            artifact_path = None
            if outcome.status == "finished" and session.steps:
                try:
                    cap = compile_capability(client, config, session.steps, outcome)
                    if not do_not_write:
                        artifact_path = write_artifact(cap, artifacts_root)
                        evidence.log(
                            phase="discovery",
                            message="artifact_written",
                            metadata={
                                "path": str(artifact_path),
                                "steps": len(cap.steps),
                                "outcomes": [o.name for o in cap.outcomes],
                            },
                        )
                except Exception as exc:  # noqa: BLE001
                    evidence.log(
                        phase="discovery",
                        message="compile_failed",
                        error=f"{type(exc).__name__}: {exc}"[:400],
                    )
                    raise
            else:
                evidence.log(
                    phase="discovery",
                    message="skip_compile",
                    metadata={"status": outcome.status},
                )

            log_path = evidence.write_logs()
            return {
                "artifact_path": artifact_path,
                "evidence_dir": evidence_dir,
                "transcript_path": transcript_path,
                "log_path": log_path,
                "outcome": asdict(outcome),
                "step_count": len(session.steps),
            }
        finally:
            await browser.close()
