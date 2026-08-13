"""Phase 1 acceptance script.

Drives the Flask test client through auth, happy path, three natural conditions,
and all six injection modes for both tenants. Exits 0 iff every check passes.

Run: `python -m target_app.acceptance`
"""

from __future__ import annotations

import sys
from typing import Callable

from . import app as app_module
from . import tenants


class Checker:
    def __init__(self, label: str) -> None:
        self.label = label
        self.failed = 0
        self.passed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        if condition:
            self.passed += 1
        else:
            self.failed += 1
        suffix = f" -- {detail}" if detail else ""
        print(f"  [{status}] {self.label} :: {name}{suffix}")


def _login(client) -> None:
    client.post(
        "/login",
        data={
            "ctl00_MainContent_txtUsername": "demo",
            "ctl00_MainContent_txtPassword": "demo123",
        },
        follow_redirects=False,
    )


def run_tenant(tenant_id: str, patch_sleep: Callable[[float], None]) -> Checker:
    tenant = tenants.resolve(tenant_id)
    prefix = tenant.route_prefix
    app = app_module.create_app(tenant_id)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    c = Checker(tenant_id)

    with app.test_client() as client:
        # Auth: bad creds rejected, good creds redirect to prefix/members
        r = client.post(
            "/login",
            data={
                "ctl00_MainContent_txtUsername": "demo",
                "ctl00_MainContent_txtPassword": "wrong",
            },
        )
        c.check("bad creds show error", b"Invalid credentials" in r.data)

        r = client.post(
            "/login",
            data={
                "ctl00_MainContent_txtUsername": "demo",
                "ctl00_MainContent_txtPassword": "demo123",
            },
        )
        c.check(
            "good creds redirect to members",
            r.status_code == 302 and r.headers["Location"].endswith(prefix + "/members"),
        )

        # Members page renders with tenant-specific label
        r = client.get(prefix + "/members")
        c.check(
            "members page has tenant search button",
            tenant.search_button_label.encode() in r.data,
        )
        c.check(
            "members page has tenant heading",
            tenant.members_heading.encode() in r.data,
        )

        # Happy path: search for 10001, view detail, open sub-account
        r = client.post(
            prefix + "/members",
            data={"ctl00_MainContent_txtMemberId": "10001"},
        )
        c.check("search 10001 shows result row", b"Alex Rivera" in r.data)

        r = client.get(prefix + "/members/10001")
        c.check("detail page shows accounts", b"****4417" in r.data)
        c.check(
            "detail page uses ASP.NET-style grid id",
            b"ctl00_MainContent_grdAccounts" in r.data,
        )

        r = client.get(prefix + "/members/10001/subaccount")
        c.check("subaccount form renders", b"Opening deposit" in r.data)
        c.check(
            "subaccount form uses label-for bindings",
            b'for="ctl00_MainContent_txtOpeningDeposit"' in r.data,
        )

        r = client.post(
            prefix + "/members/10001/subaccount",
            data={
                "ctl00_MainContent_ddlAccountType": "Savings",
                "ctl00_MainContent_txtOpeningDeposit": "100.00",
                "ctl00_MainContent_txtNickname": "Vacation",
                "ctl00_MainContent_ddlFundingSource": "****4417",
            },
        )
        c.check("happy-path confirmation renders", b"Sub-Account Opened" in r.data)
        c.check("confirmation has reference number", b"REF-" in r.data)

        # Natural condition 1: member 99999 doesn't exist (search + direct)
        r = client.post(
            prefix + "/members",
            data={"ctl00_MainContent_txtMemberId": "99999"},
        )
        c.check(
            "natural: search 99999 shows no-match",
            b"No member found matching that ID" in r.data,
        )
        r = client.get(prefix + "/members/99999")
        c.check(
            "natural: direct GET 99999 is 404 with not-found template",
            r.status_code == 404 and b"No member exists with ID 99999" in r.data,
        )

        # Natural condition 2: restricted member (10002)
        r = client.get(prefix + "/members/10002")
        c.check(
            "natural: restricted member returns 403 with restricted template",
            r.status_code == 403 and b"Restricted status" in r.data,
        )

        # Natural condition 3: minimum opening deposit validation
        r = client.post(
            prefix + "/members/10001/subaccount",
            data={
                "ctl00_MainContent_ddlAccountType": "Checking",
                "ctl00_MainContent_txtOpeningDeposit": "10.00",
                "ctl00_MainContent_txtNickname": "",
                "ctl00_MainContent_ddlFundingSource": "****4417",
            },
        )
        c.check(
            "natural: below-minimum deposit shows validation error",
            b"at least $25.00" in r.data,
        )

        # Injection modes
        r = client.post(
            prefix + "/members?inject=notfound",
            data={"ctl00_MainContent_txtMemberId": "10001"},
        )
        c.check(
            "inject notfound: search shows no-match",
            b"No member found matching that ID" in r.data,
        )

        # inject=slow: patch sleep for speed, but verify it was called
        called = {"n": 0}

        def _fake_sleep(secs: float) -> None:
            called["n"] += 1

        patch_sleep(_fake_sleep)
        r = client.get(prefix + "/members?inject=slow")
        patch_sleep(None)
        c.check(
            "inject slow: sleeps then renders 200",
            r.status_code == 200 and called["n"] >= 1,
        )

        r = client.get(prefix + "/members?inject=500")
        c.check("inject 500: server error", r.status_code == 500)

        r = client.get(prefix + "/members?inject=expired")
        c.check(
            "inject expired: redirects to /login?reason=timeout",
            r.status_code == 302 and "reason=timeout" in r.headers["Location"],
        )

        # re-login (expired cleared the session)
        _login(client)

        r = client.get(prefix + "/members?inject=dialog")
        c.check(
            "inject dialog: renders page with modal panel",
            r.status_code == 200 and b"ctl00_MainContent_pnlInterstitial" in r.data,
        )

        r = client.get(prefix + "/members?inject=denied")
        c.check(
            "inject denied: 403 with denied template",
            r.status_code == 403
            and b"You do not have permission to view this record" in r.data,
        )

    return c


def main() -> int:
    import target_app.app as target_module

    original_sleep = target_module.time.sleep

    def patch_sleep(fn):
        target_module.time.sleep = fn if fn is not None else original_sleep

    total_failed = 0
    total_passed = 0
    print("Phase 1 acceptance -- both tenants")
    for tenant_id in ("meridian", "summit"):
        print(f"\nTenant: {tenant_id}")
        c = run_tenant(tenant_id, patch_sleep)
        total_passed += c.passed
        total_failed += c.failed

    print(f"\nResult: {total_passed} passed, {total_failed} failed")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
