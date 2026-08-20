"""Generic programmatic escalation demo: any capability, any policy-gated step.

Generalizes ``live_transfer_post_demo.py`` (which was mc_transfer-specific)
to take a capability id, version, and params directly, since several
recorded capabilities now have a real require_confirmation commit step
(Post Transfer, Open Share, Post Hold, ...).

    AUTOMATION -> PENDING_HANDOFF -> HUMAN -> RESUMING -> AUTOMATION

Launches ``cua replay --enable-escalation``, waits for the engine to open an
intervention on the policy-gated step, drives take_control + hand_back
through the operator console's own HTTP API (a scripted stand-in for a
human approver, not a bypass), and reports the final replay result.

Usage:
    .venv/bin/python scripts/live_escalate_replay.py \\
        --capability-id mc_open_share --version 1.0 \\
        --param member_id=100234 --param share_type=CERT \\
        --param initial_deposit=15.00 --param operator_password=password
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE = REPO_ROOT / "evidence"
OPERATOR_URL = "http://127.0.0.1:8765"
ACTOR = "alice-auditor"


def _http_get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_operator_up(deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            _http_get_json(f"{OPERATOR_URL}/state", timeout=1.5)
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    raise SystemExit(f"operator console did not answer on {OPERATOR_URL} in time")


def _wait_pending(deadline: float) -> dict:
    last_state: dict = {}
    while time.monotonic() < deadline:
        state = _http_get_json(f"{OPERATOR_URL}/state")
        last_state = state
        if state.get("pending") is not None:
            return state
        time.sleep(0.25)
    raise SystemExit(
        "engine never opened an intervention; "
        f"last lease state was {last_state.get('lease', {}).get('state')!r}"
    )


def _poll_until_automation(deadline: float) -> dict:
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            state = _http_get_json(f"{OPERATOR_URL}/state")
        except (urllib.error.URLError, ConnectionError, OSError):
            return last
        last = state
        if state.get("lease", {}).get("state") == "AUTOMATION":
            return state
        time.sleep(0.1)
    return last


def _drive_escalation(cycle_log: list[dict]) -> dict:
    state_before = _wait_pending(deadline=time.monotonic() + 90.0)
    cycle_log.append({"phase": "pending_seen", "state": state_before})

    tc = _http_post_json(f"{OPERATOR_URL}/take_control", {"actor": ACTOR})
    cycle_log.append({"phase": "take_control", "response": tc})

    hb = _http_post_json(
        f"{OPERATOR_URL}/hand_back",
        {"actor": ACTOR, "notes": "auditor script approved the policy-gated commit"},
    )
    cycle_log.append({"phase": "hand_back", "response": hb})

    state_after = _poll_until_automation(deadline=time.monotonic() + 20.0)
    cycle_log.append({"phase": "post_handback", "state": state_after})
    return state_after


def _parse_replay_json(stdout: str) -> dict | None:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(stdout[start:end + 1])
    except json.JSONDecodeError:
        return None


def _launch_replay(
    capability_id: str, version: str, params: list[str], evidence_root: Path
) -> subprocess.Popen[str]:
    cmd = [sys.executable, "-m", "cua", "replay", "--capability-id", capability_id]
    if version:
        cmd += ["--version", version]
    for p in params:
        cmd += ["--param", p]
    cmd += [
        "--enable-escalation",
        "--escalation-timeout-s", "90",
        "--evidence-root", str(evidence_root),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capability-id", required=True)
    ap.add_argument("--version", default="")
    ap.add_argument("--param", action="append", default=[], help="name=value, repeatable")
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE)
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.evidence_root / f"live_escalate_{args.capability_id}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cycle_log: list[dict] = []

    proc = _launch_replay(args.capability_id, args.version, args.param, args.evidence_root)
    try:
        _wait_operator_up(deadline=time.monotonic() + 30.0)
        cycle_log.append({"phase": "operator_up"})
        state_after = _drive_escalation(cycle_log)

        stdout, stderr = proc.communicate(timeout=120)
        replay_rc = proc.returncode
    except Exception:
        proc.kill()
        stdout, stderr = proc.communicate()
        (out_dir / "replay_stdout.log").write_text(stdout or "")
        (out_dir / "replay_stderr.log").write_text(stderr or "")
        (out_dir / "driver_cycle_log.json").write_text(
            json.dumps(cycle_log, indent=2, default=str)
        )
        raise

    (out_dir / "replay_stdout.log").write_text(stdout)
    (out_dir / "replay_stderr.log").write_text(stderr)
    (out_dir / "driver_cycle_log.json").write_text(
        json.dumps(cycle_log, indent=2, default=str)
    )
    (out_dir / "final_operator_state.json").write_text(
        json.dumps(state_after, indent=2, default=str)
    )

    replay_result = _parse_replay_json(stdout)
    if replay_result is not None:
        (out_dir / "replay_result.json").write_text(
            json.dumps(replay_result, indent=2, default=str)
        )

    lease = state_after.get("lease", {})
    transitions = lease.get("transitions", [])
    kinds = [t.get("to_state") for t in transitions]
    manifest = {
        "generated_at": stamp,
        "capability_id": args.capability_id,
        "replay_exit_code": replay_rc,
        "replay_status": replay_result.get("status") if replay_result else None,
        "replay_outputs": replay_result.get("outputs") if replay_result else None,
        "replay_evidence_dir": (
            replay_result.get("evidence_dir") if replay_result else None
        ),
        "lease_final_state": lease.get("state"),
        "lease_transition_kinds": kinds,
        "full_cycle_observed": kinds
        == ["PENDING_HANDOFF", "HUMAN", "RESUMING", "AUTOMATION"],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

    print(f"[live-escalate] evidence dir: {out_dir}")
    print(f"[live-escalate] lease transitions: {kinds}")
    print(f"[live-escalate] full cycle: {manifest['full_cycle_observed']}")
    print(f"[live-escalate] replay status: {manifest['replay_status']}")
    print(f"[live-escalate] replay outputs: {manifest['replay_outputs']}")
    print(f"[live-escalate] replay exit code: {replay_rc}")
    if not manifest["full_cycle_observed"] or replay_rc != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
