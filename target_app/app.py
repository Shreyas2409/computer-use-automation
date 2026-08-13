"""Flask target app: a legacy-hostile credit-union servicing console.

Run: `python -m target_app.app`. Serves on port 8080.
Tenant selection via `TENANT` env var (defaults to `meridian`).
"""

from __future__ import annotations

import os
import secrets
import time

from flask import (
    Flask,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import data, tenants

INJECT_MODES = frozenset({"notfound", "slow", "500", "expired", "dialog", "denied"})
SLOW_INJECT_SECONDS = 5.0
DEMO_USER = "demo"
DEMO_PASSWORD = "demo123"


def create_app(tenant_name: str | None = None) -> Flask:
    tenant = tenants.resolve(tenant_name if tenant_name is not None else os.environ.get("TENANT"))
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "dev-only-secret-do-not-use-in-prod"
    app.config["TENANT"] = tenant
    prefix = tenant.route_prefix

    @app.context_processor
    def _inject_globals():
        return {
            "tenant": tenant,
            "inject_mode": getattr(g, "inject_mode", None),
            "prefix": prefix,
        }

    @app.before_request
    def _handle_injection():
        mode = request.args.get("inject")
        g.inject_mode = mode if mode in INJECT_MODES else None
        if request.path.startswith("/login") or request.path.startswith("/static"):
            return None
        if g.inject_mode == "expired":
            session.clear()
            return redirect(url_for("login") + "?reason=timeout")
        if g.inject_mode == "500":
            raise RuntimeError("Injected 500 error")
        if g.inject_mode == "denied":
            return render_template("denied.html"), 403
        if g.inject_mode == "slow":
            time.sleep(SLOW_INJECT_SECONDS)
        return None

    def _require_auth():
        if not session.get("user"):
            return redirect(url_for("login"))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        reason = request.args.get("reason")
        error = None
        if request.method == "POST":
            u = request.form.get("ctl00_MainContent_txtUsername", "")
            p = request.form.get("ctl00_MainContent_txtPassword", "")
            if u == DEMO_USER and p == DEMO_PASSWORD:
                session["user"] = u
                return redirect(prefix + "/members")
            error = "Invalid credentials. Please try again."
        return render_template("login.html", error=error, reason=reason)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route(prefix + "/members", methods=["GET", "POST"])
    def members():
        guard = _require_auth()
        if guard:
            return guard
        results: list[data.Member] | None = None
        query = ""
        no_match_message: str | None = None
        if request.method == "POST":
            query = request.form.get("ctl00_MainContent_txtMemberId", "").strip()
            if g.inject_mode == "notfound":
                no_match_message = "No member found matching that ID"
                results = []
            elif query:
                m = data.get_member(query)
                if m:
                    results = [m]
                else:
                    no_match_message = "No member found matching that ID"
                    results = []
        elif g.inject_mode == "notfound":
            no_match_message = "No member found matching that ID"
            results = []
        return render_template(
            "members.html",
            results=results,
            query=query,
            no_match_message=no_match_message,
        )

    @app.route(prefix + "/members/<member_id>")
    def member_detail(member_id: str):
        guard = _require_auth()
        if guard:
            return guard
        member = data.get_member(member_id)
        if not member:
            return render_template("not_found.html", member_id=member_id), 404
        if member.status == "Restricted":
            return render_template("restricted.html", member=member), 403
        return render_template("member_detail.html", member=member)

    @app.route(prefix + "/members/<member_id>/subaccount", methods=["GET", "POST"])
    def subaccount(member_id: str):
        guard = _require_auth()
        if guard:
            return guard
        member = data.get_member(member_id)
        if not member:
            return render_template("not_found.html", member_id=member_id), 404
        if member.status == "Restricted":
            return render_template("restricted.html", member=member), 403
        errors: dict[str, str] = {}
        form = {
            "account_type": "",
            "opening_deposit": "",
            "nickname": "",
            "funding_source": "",
        }
        if request.method == "POST":
            form["account_type"] = request.form.get("ctl00_MainContent_ddlAccountType", "")
            form["opening_deposit"] = request.form.get("ctl00_MainContent_txtOpeningDeposit", "").strip()
            form["nickname"] = request.form.get("ctl00_MainContent_txtNickname", "").strip()
            form["funding_source"] = request.form.get("ctl00_MainContent_ddlFundingSource", "")
            if form["account_type"] not in ("Checking", "Savings", "Money Market"):
                errors["account_type"] = "Select an account type"
            try:
                cents = int(round(float(form["opening_deposit"]) * 100))
                if cents < data.MIN_OPENING_DEPOSIT_CENTS:
                    errors["opening_deposit"] = "Opening deposit must be at least $25.00"
            except ValueError:
                errors["opening_deposit"] = "Enter a valid deposit amount"
            if not form["funding_source"]:
                errors["funding_source"] = "Choose a funding account"
            if not errors:
                ref = "REF-" + secrets.token_hex(4).upper()
                return render_template(
                    "confirmation.html", member=member, form=form, reference=ref
                )
        return render_template("subaccount.html", member=member, form=form, errors=errors)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8080, debug=False)
