"""Programmatic end-to-end live escalation demo (no human required).

Drives a full lease cycle against a real browser + real operator console:

    AUTOMATION -> PENDING_HANDOFF -> HUMAN -> RESUMING -> AUTOMATION

1. Launches ``cua replay --enable-escalation --inject-mode escalate`` as a
   subprocess. The engine runs the ``lookup_member_balance`` capability
   against ``http://localhost:8080`` in a headless browser, and the operator
   console starts on ``http://127.0.0.1:8765``.
2. Injects an in-process fault at ``--inject-after-step`` (default: step 3).
   The action handler raises, recovery rules do not match, so the engine
   opens an intervention through the broker and parks on the lease.
3. Polls ``GET /state`` on the operator console until ``pending`` is set,
   snapshots the lease state, then ``POST /take_control`` and
   ``POST /hand_back`` with the ``resume_token`` the engine handed us.
4. Waits for the replay subprocess to exit; captures the JSON result, a
   copy of ``/state`` after the cycle, and a manifest that stitches the
   evidence dir + console-timeline together.

Usage (auditor-reproducible one-liner):

    .venv/bin/python scripts/live_escalation_demo.py

Requires: target app on ``http://localhost:8080``; Playwright chromium
installed; free port 8765. Never prints or writes secret values.
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
    """Poll /state until the Flask console answers or the deadline lapses."""

    while time.monotonic() < deadline:
        try:
            _http_get_json(f"{OPERATOR_URL}/state", timeout=1.5)
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    raise SystemExit(
        f"operator console did not answer on {OPERATOR_URL} in time"
    )


def _wait_pending(deadline: float) -> dict:
    """Poll until the engine has opened an intervention (pending is set)."""

    last_state: dict = {}
    while time.monotonic() < deadline:
        state = _http_get_json(f"{OPERATOR_URL}/state")
        last_state = state
        pending = state.get("pending")
        if pending is not None:
            return state
        time.sleep(0.25)
    raise SystemExit(
        "engine never opened an intervention; "
        f"last lease state was {last_state.get('lease', {}).get('state')!r}"
    )


def _drive_escalation(cycle_log: list[dict]) -> dict:
    """take_control -> hand_back. Returns the final /state snapshot."""

    state_before = _wait_pending(deadline=time.monotonic() + 60.0)
    cycle_log.append({"phase": "pending_seen", "state": state_before})
    # NB: the operator console deliberately strips ``resume_token`` from
    # /state so it never lands in the polling UI. hand_back accepts a
    # missing resume_token (matching the console's own posts), so the
    # driver does not need it either.

    tc = _http_post_json(
        f"{OPERATOR_URL}/take_control", {"actor": ACTOR}
    )
    cycle_log.append({"phase": "take_control", "response": tc})

    hb = _http_post_json(
        f"{OPERATOR_URL}/hand_back",
        {
            "actor": ACTOR,
            "notes": "auditor script drove full lease cycle end-to-end",
        },
    )
    cycle_log.append({"phase": "hand_back", "response": hb})

    # The engine transitions RESUMING -> AUTOMATION via complete_resume()
    # after asyncio wakes from wait_for_handback(). Poll briefly so the
    # captured lease log includes the final AUTOMATION row instead of
    # racing the automation side.
    state_after = _poll_until_automation(deadline=time.monotonic() + 15.0)
    cycle_log.append({"phase": "post_handback", "state": state_after})
    return state_after


def _parse_replay_json(stdout: str) -> dict | None:
    """Extract the trailing JSON block from replay stdout.

    Flask's dev server prints ' * Serving ...' lines to stdout before the
    replay result gets json-dumped, so a naive ``json.loads`` fails. Slice
    from the first ``{`` to the last ``}`` inclusive and try to parse.
    """

    start = stdout.find("{")
    end = stdout.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(stdout[start:end + 1])
    except json.JSONDecodeError:
        return None


def _poll_until_automation(deadline: float) -> dict:
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            state = _http_get_json(f"{OPERATOR_URL}/state")
        except (urllib.error.URLError, ConnectionError, OSError):
            # Subprocess (and thus operator) may have exited already; return
            # the last successful snapshot we saw.
            return last
        last = state
        if state.get("lease", {}).get("state") == "AUTOMATION":
            return state
        time.sleep(0.1)
    return last


def _launch_replay(evidence_root: Path) -> subprocess.Popen[str]:
    """Spawn the replay CLI with escalation enabled and inject=escalate."""

    cmd = [
        sys.executable, "-m", "cua", "replay",
        "--capability-id", "lookup_member_balance",
        "--version", "2.2",
        "--param", "member_id=10001",
        "--param", "operator_password=demo123",
        "--enable-escalation",
        "--inject-mode", "escalate",
        "--inject-after-step", "3",
        "--escalation-timeout-s", "60",
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
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE)
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.evidence_root / f"live_escalation_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cycle_log: list[dict] = []

    proc = _launch_replay(args.evidence_root)
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
        "replay_exit_code": replay_rc,
        "replay_evidence_dir": (
            replay_result.get("evidence_dir") if replay_result else None
        ),
        "lease_final_state": lease.get("state"),
        "lease_transition_kinds": kinds,
        "full_cycle_observed": kinds
        == ["PENDING_HANDOFF", "HUMAN", "RESUMING", "AUTOMATION"],
        "interventions": (
            replay_result.get("interventions") if replay_result else None
        ),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str)
    )

    print(f"[live-escalation] evidence dir: {out_dir}")
    print(f"[live-escalation] lease transitions: {kinds}")
    print(f"[live-escalation] full cycle: {manifest['full_cycle_observed']}")
    print(f"[live-escalation] replay exit code: {replay_rc}")
    if not manifest["full_cycle_observed"] or replay_rc != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
