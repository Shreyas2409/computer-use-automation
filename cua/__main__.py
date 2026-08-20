"""CLI entry: ``python -m cua {discover|replay|catalog|operator} ...``.

``catalog`` exposes the agent-facing surface: ``list`` prints Anthropic-shaped
tool definitions for every approved capability; ``invoke`` runs one against a
live browser through the shared replay engine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from cua.catalog import (
    InvokeOptions,
    build_tool_definition,
    capability_signature,
    invoke,
    list_published,
)
from cua.discover import DEFAULT_MODEL, MAX_STEPS, DiscoveryConfig, run_discovery
from cua.escalate import EscalationBroker
from cua.llm import default_model_for, select_provider
from cua.registry import compose, load
from cua.replay import ReplayConfig, run_replay
from cua.result import result_to_dict
from cua.schema import ParamSpec, Sensitivity, TenantOverlay
# Side-effect import: installs the Page.accessibility compat shim so the
# discovery loop's a11y capture works on Playwright 1.60+.
import cua.surface  # noqa: F401, E402


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACTS = REPO_ROOT / "artifacts"
DEFAULT_EVIDENCE = REPO_ROOT / "evidence"


def _parse_params(raw: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--param must be name=value, got {item!r}")
        name, _, value = item.partition("=")
        name = name.strip()
        if not name:
            raise SystemExit(f"--param name is empty in {item!r}")
        out[name] = value
    return out


def _parse_param_specs(paths: list[str]) -> dict[str, ParamSpec]:
    """Load one or more ParamSpec JSON files keyed by name.

    Each file is either a list of ParamSpec dicts or a single dict.
    """

    specs: dict[str, ParamSpec] = {}
    for p in paths or []:
        data = json.loads(Path(p).read_text())
        items = data if isinstance(data, list) else [data]
        for entry in items:
            spec = ParamSpec.model_validate(entry)
            specs[spec.name] = spec
    return specs


def _default_spec(name: str, sensitivity: Sensitivity = "internal") -> ParamSpec:
    return ParamSpec(
        name=name,
        type="string",
        required=True,
        description=f"Runtime-supplied value for {name}",
        example="example",
        sensitivity=sensitivity,
    )


def _add_discover_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "discover",
        help="Run the LLM tool-use discovery loop against a live app.",
    )
    p.add_argument("--goal", required=True, help="Plain-English goal for the model.")
    p.add_argument("--base-url", required=True, help="Root URL of the target app.")
    p.add_argument("--tenant", required=True, help="Tenant id (e.g. meridian).")
    p.add_argument("--app-id", required=True, help="App id recorded in target.")
    p.add_argument("--target-version", default="unknown")
    p.add_argument("--capability-id", required=True)
    p.add_argument(
        "--surface-kind",
        default="legacy_web",
        choices=["web", "legacy_web", "desktop"],
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Runtime input; repeat. Values may be redacted by --param-spec.",
    )
    p.add_argument(
        "--param-spec",
        action="append",
        default=[],
        metavar="PATH.json",
        help="ParamSpec JSON file(s) declaring sensitivity for --param values.",
    )
    p.add_argument(
        "--secret-param",
        action="append",
        default=[],
        metavar="NAME",
        help="Mark a --param as sensitivity=secret without a full ParamSpec file.",
    )
    p.add_argument(
        "--provider",
        default=None,
        choices=["anthropic", "openai"],
        help="LLM provider; auto-detected from present API key when omitted.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Model id override; defaults to the provider's default.",
    )
    p.add_argument("--max-steps", type=int, default=MAX_STEPS)
    p.add_argument("--timeout-seconds", type=int, default=300)
    p.add_argument("--headed", action="store_true", help="Run browser headed.")
    p.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS,
    )
    p.add_argument(
        "--evidence-root",
        type=Path,
        default=DEFAULT_EVIDENCE,
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Skip writing the compiled artifact (useful for dry-runs).",
    )


def _cmd_discover(args: argparse.Namespace) -> int:
    params = _parse_params(args.param)
    param_specs = _parse_param_specs(args.param_spec)
    for name in args.secret_param or []:
        param_specs[name] = _default_spec(name, sensitivity="secret")
    # Fill any --param that lacks a spec with an internal-sensitivity default,
    # so evidence redaction always has something to key on.
    for name in params:
        param_specs.setdefault(name, _default_spec(name))

    provider = select_provider(args.provider)
    # Model resolution: explicit --model > CUA_MODEL env > provider default.
    # ``DEFAULT_MODEL`` is Anthropic-flavoured; honour it only when the
    # provider is anthropic so an OpenAI run doesn't try a Claude id.
    if args.model:
        model = args.model
    elif provider == "anthropic":
        model = DEFAULT_MODEL
    else:
        model = default_model_for(provider)

    config = DiscoveryConfig(
        goal=args.goal,
        base_url=args.base_url,
        tenant=args.tenant,
        capability_id=args.capability_id,
        app_id=args.app_id,
        target_version=args.target_version,
        params=params,
        param_specs=param_specs,
        surface_kind=args.surface_kind,
        max_steps=args.max_steps,
        timeout_seconds=args.timeout_seconds,
        model=model,
        provider=provider,
    )
    result = asyncio.run(
        run_discovery(
            config,
            artifacts_root=args.artifacts_root,
            evidence_root=args.evidence_root,
            headless=not args.headed,
            do_not_write=args.no_write,
        )
    )
    # Print a compact JSON summary; paths are the actionable bits.
    summary = {
        "outcome": result["outcome"],
        "step_count": result["step_count"],
        "artifact_path": (
            str(result["artifact_path"]) if result["artifact_path"] else None
        ),
        "evidence_dir": str(result["evidence_dir"]),
        "transcript_path": str(result["transcript_path"]),
        "log_path": str(result["log_path"]),
    }
    print(json.dumps(summary, indent=2))
    return 0 if result["outcome"]["status"] == "finished" else 1


def _add_replay_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "replay",
        help="Deterministically replay a versioned capability artifact.",
    )
    p.add_argument("--capability-id", required=True)
    p.add_argument(
        "--version",
        default=None,
        help='Version spec: "2" (highest 2.x approved) or "2.1" (exact). '
        "Defaults to the highest approved version.",
    )
    p.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help="Optional TenantOverlay JSON to compose onto the base capability.",
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Runtime input; repeat.",
    )
    p.add_argument(
        "--inject-mode",
        default=None,
        choices=[
            # Original demo target's fault vocabulary.
            "notfound", "slow", "500", "expired", "dialog", "denied",
            "escalate",
            # Meridian Core's fault vocabulary (?inject=<mode> per the brief).
            # "notfound" is shared with the list above; the rest are new.
            "validation", "permission", "timeout", "maintenance", "server",
        ],
        help=(
            "Injection mode. Target-app modes append ?inject=MODE once; "
            "'escalate' is an in-process fault fired inside the action "
            "handler to exercise the full escalation cycle end-to-end."
        ),
    )
    p.add_argument(
        "--inject-after-step",
        type=int,
        default=3,
        help=(
            "For URL-based inject modes, apply once after this step index. "
            "For --inject-mode=escalate, raise the fault ON this step index "
            "(default: 3)."
        ),
    )
    p.add_argument(
        "--step-timeout-ms", type=int, default=6000,
    )
    p.add_argument("--headed", action="store_true", help="Run browser headed.")
    p.add_argument(
        "--evidence-root", type=Path, default=DEFAULT_EVIDENCE,
    )
    p.add_argument(
        "--enable-escalation",
        action="store_true",
        help="Serve the operator console and route escalations through it.",
    )
    p.add_argument(
        "--operator-host", default="127.0.0.1",
        help="Bind host for the operator console (default: 127.0.0.1).",
    )
    p.add_argument(
        "--operator-port", type=int, default=8765,
        help="Bind port for the operator console (default: 8765).",
    )
    p.add_argument(
        "--escalation-timeout-s", type=float, default=300.0,
        help="Seconds the engine will wait for an operator handback.",
    )


def _cmd_replay(args: argparse.Namespace) -> int:
    params = _parse_params(args.param)
    cap = load(args.capability_id, args.version)
    if args.overlay is not None:
        overlay = TenantOverlay.model_validate(
            json.loads(args.overlay.read_text())
        )
        cap = compose(cap, overlay)

    broker: EscalationBroker | None = None
    operator_thread = None
    if args.enable_escalation:
        broker, operator_thread = _start_operator(
            args.operator_host, args.operator_port
        )
        print(
            f"[operator] console at http://{args.operator_host}:"
            f"{args.operator_port}/  (broker attached)",
            file=sys.stderr,
        )

    config = ReplayConfig(
        step_timeout_ms=args.step_timeout_ms,
        inject_mode=args.inject_mode,
        inject_after_step=args.inject_after_step,
        escalation_broker=broker,
        escalation_timeout_s=args.escalation_timeout_s,
        escalation_goal=cap.title,
    )
    try:
        result = asyncio.run(
            run_replay(
                cap,
                params,
                evidence_root=args.evidence_root,
                headless=not args.headed,
                config=config,
            )
        )
    finally:
        # Flask dev server is a daemon thread; nothing to stop explicitly.
        _ = operator_thread
    print(json.dumps(result_to_dict(result), indent=2, default=str))
    return getattr(result, "exit_code", 0)


def _start_operator(host: str, port: int):
    """Boot the Flask operator console in a background daemon thread.

    Import Flask locally so ``cua`` still runs when the optional dependency
    isn't installed (e.g. minimal replay setups).
    """

    import threading

    from cua.operator import create_app

    broker = EscalationBroker()
    app = create_app(broker)

    def _serve() -> None:
        app.run(host=host, port=port, debug=False, use_reloader=False)

    thread = threading.Thread(
        target=_serve, name="operator-console", daemon=True
    )
    thread.start()
    return broker, thread


def _add_catalog_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "catalog",
        help="Agent-facing catalog: list published capabilities and invoke by name.",
    )
    csub = p.add_subparsers(dest="catalog_command", required=True)

    lp = csub.add_parser(
        "list",
        help="Print the tool definitions for every approved capability as JSON.",
    )
    lp.add_argument(
        "--artifacts-root", type=Path, default=DEFAULT_ARTIFACTS,
    )
    lp.add_argument(
        "--tool-only",
        action="store_true",
        help="Emit only the Anthropic-shaped tool defs (drop signature metadata).",
    )

    ip = csub.add_parser(
        "invoke",
        help="Invoke an approved capability by name against a live browser.",
    )
    ip.add_argument("--name", required=True, help="Capability id to invoke.")
    ip.add_argument(
        "--version",
        default=None,
        help='Version spec: "2" (highest 2.x approved) or "2.1" (exact). '
        "Defaults to the highest approved version.",
    )
    ip.add_argument(
        "--tenant",
        default=None,
        help="Tenant id; when set, composes the matching overlay before replay.",
    )
    ip.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Runtime input; repeat.",
    )
    ip.add_argument("--headed", action="store_true", help="Run browser headed.")
    ip.add_argument(
        "--step-timeout-ms", type=int, default=6000,
    )
    ip.add_argument(
        "--artifacts-root", type=Path, default=DEFAULT_ARTIFACTS,
    )
    ip.add_argument(
        "--evidence-root", type=Path, default=DEFAULT_EVIDENCE,
    )


def _cmd_catalog_list(args: argparse.Namespace) -> int:
    caps = list_published(args.artifacts_root)
    if args.tool_only:
        payload = [build_tool_definition(c) for c in caps]
    else:
        payload = [
            {**capability_signature(c), "tool": build_tool_definition(c)}
            for c in caps
        ]
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_catalog_invoke(args: argparse.Namespace) -> int:
    params = _parse_params(args.param)
    opts = InvokeOptions(
        version=args.version,
        tenant_id=args.tenant,
        headless=not args.headed,
        step_timeout_ms=args.step_timeout_ms,
    )
    result = asyncio.run(
        invoke(
            args.name,
            params,
            options=opts,
            evidence_root=args.evidence_root,
            artifacts_dir=args.artifacts_root,
        )
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "ok" else 1


def _cmd_catalog(args: argparse.Namespace) -> int:
    if args.catalog_command == "list":
        return _cmd_catalog_list(args)
    if args.catalog_command == "invoke":
        return _cmd_catalog_invoke(args)
    raise SystemExit(f"unknown catalog subcommand: {args.catalog_command}")


def _add_operator_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "operator",
        help="Run the standalone operator console (no engine attached).",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)


def _cmd_operator(args: argparse.Namespace) -> int:
    from cua.operator import create_app

    broker = EscalationBroker()
    app = create_app(broker)
    print(
        f"[operator] standalone console on http://{args.host}:{args.port}/ "
        "(no engine attached — /take_control will 409 until an intervention "
        "is opened programmatically).",
        file=sys.stderr,
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_discover_parser(sub)
    _add_replay_parser(sub)
    _add_catalog_parser(sub)
    _add_operator_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "discover":
        return _cmd_discover(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "catalog":
        return _cmd_catalog(args)
    if args.command == "operator":
        return _cmd_operator(args)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable, keeps type-checkers happy


if __name__ == "__main__":
    sys.exit(main())
