"""Programmatic end-to-end demo: the real policy gate on Post Transfer.

Unlike ``live_escalation_demo.py`` (which injects a fault to force an
escalation), this drives the genuine ``require_confirmation`` verdict that
``policy.yaml`` assigns to the "Post Transfer" click — the actual irreversible
commit step of ``mc_transfer`` v2.0. No fault injection; the gate fires
because that is what it is designed to do for this action.

    AUTOMATION -> PENDING_HANDOFF -> HUMAN -> RESUMING -> AUTOMATION

1. Launches ``cua replay --enable-escalation`` as a subprocess for
   ``mc_transfer`` v2.0. Steps 0-13 (sign on, search, select member, fill
   the transfer form, reach the review screen) run normally. At step 14
   (click "Post Transfer"), the policy gate returns ``require_confirmation``
   and the engine opens an intervention through the broker.
2. Polls ``GET /state`` on the operator console until pending, then
   ``POST /take_control`` + ``POST /hand_back`` — a scripted stand-in for a
   human approver, using the exact same operator API a real person would.
3. Waits for the replay subprocess to exit; captures the JSON result (typed
   outputs: confirmation_number, posted_at, amount_posted, both resulting
   balances) and the console lease-transition timeline into a manifest.

This is also the empirical check for whether the review page's second,
freshly-minted ``_token`` (different from the initial form page's) is
actually carried correctly: since every step is a real Playwright click
against the live DOM, each form submission carries whatever token is
currently on that specific page — no explicit token-read step exists or is
needed. A clean ``TRANSACTION COMPLETE`` result is definitive proof.

Usage:
    .venv/bin/python scripts/live_transfer_post_demo.py

Requires: Playwright chromium installed; free port 8765; a live network
path to https://web-sample.interface-hiring.com. This performs a REAL
transfer against the shared demo target when run.
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
        {"actor": ACTOR, "notes": "auditor script approved the Post Transfer commit"},
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
    evidence_root: Path, *, from_share: str, to_share: str, amount: str, memo: str
) -> subprocess.Popen[str]:
    cmd = [
        sys.executable, "-m", "cua", "replay",
        "--capability-id", "mc_transfer",
        "--version", "2.0",
        "--param", "member_id=100234",
        "--param", f"from_share={from_share}",
        "--param", f"to_share={to_share}",
        "--param", f"amount={amount}",
        "--param", f"memo={memo}",
        "--param", "operator_password=password",
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
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE)
    ap.add_argument("--from-share", default="100234-S0070")
    ap.add_argument("--to-share", default="100234-S0001-3")
    ap.add_argument("--amount", default="1.00")
    ap.add_argument("--memo", default="live post demo")
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.evidence_root / f"live_transfer_post_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cycle_log: list[dict] = []

    proc = _launch_replay(
        args.evidence_root,
        from_share=args.from_share,
        to_share=args.to_share,
        amount=args.amount,
        memo=args.memo,
    )
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
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str)
    )

    print(f"[live-transfer-post] evidence dir: {out_dir}")
    print(f"[live-transfer-post] lease transitions: {kinds}")
    print(f"[live-transfer-post] full cycle: {manifest['full_cycle_observed']}")
    print(f"[live-transfer-post] replay status: {manifest['replay_status']}")
    print(f"[live-transfer-post] replay outputs: {manifest['replay_outputs']}")
    print(f"[live-transfer-post] replay exit code: {replay_rc}")
    if not manifest["full_cycle_observed"] or replay_rc != 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
