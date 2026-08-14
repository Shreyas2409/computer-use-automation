"""Policy tests — brief test #4: gate blocks off-allowlist, destructive actions, sensitive data redaction."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cua.policy import PolicyGate, Action, redact_sensitive_value
from cua.evidence import EvidenceWriter
from cua.schema import ParamSpec


def test_policy_gate_blocks_off_allowlist_domain():
    """Brief test #4a: PolicyGate blocks an off-allowlist domain."""
    action = Action(
        verb="click",
        url="https://evil.com/login",
        domain="evil.com",
    )
    verdict = PolicyGate.check(action)
    assert verdict == "block", "Off-allowlist domain should be blocked"


def test_policy_gate_allows_allowlist_domain():
    """PolicyGate allows domain on allowlist."""
    action = Action(
        verb="click",
        url="http://localhost:8080/members",
    )
    verdict = PolicyGate.check(action)
    assert verdict == "allow", "Localhost should be allowed"


def test_policy_gate_blocks_destructive_action():
    """Brief test #4b: PolicyGate blocks a destructive verb (delete)."""
    action = Action(
        verb="delete",
        url="http://localhost:8080/members",
    )
    verdict = PolicyGate.check(action)
    assert verdict == "block", "Destructive verb 'delete' should be blocked"


def test_policy_gate_blocks_transfer_funds():
    """PolicyGate blocks 'transfer' (destructive)."""
    action = Action(
        verb="transfer",
        url="http://localhost:8080/members/123",
    )
    verdict = PolicyGate.check(action)
    assert verdict == "block", "Destructive verb 'transfer' should be blocked"


def test_policy_gate_blocks_close_account():
    """PolicyGate blocks 'close_account'."""
    action = Action(
        verb="close_account",
        url="http://localhost:8080/members",
    )
    verdict = PolicyGate.check(action)
    assert verdict == "block", "Destructive verb 'close_account' should be blocked"


def test_policy_gate_requires_confirmation_on_submit():
    """PolicyGate requires confirmation on 'Submit' pattern."""
    action = Action(
        verb="click",  # clicking a Submit button
        url="http://localhost:8080/servicings/confirm",
    )
    action.verb = "Submit"
    verdict = PolicyGate.check(action)
    assert verdict == "require_confirmation", "'Submit' should require confirmation"


def test_policy_gate_requires_confirmation_on_confirm():
    """PolicyGate requires confirmation on 'Confirm' pattern."""
    action = Action(
        verb="Confirm",
        url="http://localhost:8080/servicings",
    )
    verdict = PolicyGate.check(action)
    assert verdict == "require_confirmation", "'Confirm' should require confirmation"


def test_redaction_blocks_ssn_like_pattern():
    """Brief test #4c: redact_sensitive_value blocks SSN-like patterns."""
    ssn = "123-45-6789"
    redacted = redact_sensitive_value(ssn)
    assert "<redacted:ssn:9>" in redacted or "<redacted" in redacted
    assert ssn not in redacted, "SSN should not appear in redacted output"


def test_redaction_blocks_email_pattern():
    """Redaction blocks email addresses."""
    email = "user@example.com"
    redacted = redact_sensitive_value(email)
    assert "<redacted" in redacted
    assert email not in redacted, "Email should not appear in redacted output"


def test_redaction_preserves_non_sensitive():
    """Redaction preserves non-sensitive data."""
    value = "safe_string_12345"
    redacted = redact_sensitive_value(value)
    # Should match original if no redaction pattern matched
    assert redacted == value or "<redacted" not in redacted


def test_evidence_writer_logs_with_redaction():
    """Brief test #4d: evidence logs are redacted and never show sensitive data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = EvidenceWriter(tmpdir)

        # Log with sensitive metadata (e.g., SSN that matches redaction pattern)
        writer.log(
            phase="discovery",
            action="type",
            step_index=2,
            metadata={"ssn": "123-45-6789", "email": "test@example.com"}
        )

        # Write and read logs
        log_file = writer.write_logs()
        assert log_file.exists()

        content = log_file.read_text()
        # Verify sensitive patterns are redacted
        assert "123-45-6789" not in content, "SSN should not appear in evidence"
        assert "test@example.com" not in content, "Email should not appear in evidence"
        assert "<redacted" in content, "Redaction marker should be present"


def test_evidence_writer_redact_param_pii():
    """Test redaction of PII-sensitivity parameters."""
    param = ParamSpec(
        name="ssn",
        type="string",
        required=True,
        description="Social security",
        example="123-45-6789",
        sensitivity="pii"
    )
    
    redacted = EvidenceWriter.redact_param_value(param, "123-45-6789")
    assert "123-45-6789" not in redacted
    assert "<redacted:pii:" in redacted


def test_evidence_writer_redact_param_secret():
    """Test redaction of secret-sensitivity parameters."""
    param = ParamSpec(
        name="password",
        type="string",
        required=True,
        description="Login password",
        example="demo123",
        sensitivity="secret"
    )
    
    redacted = EvidenceWriter.redact_param_value(param, "mypassword")
    assert "mypassword" not in redacted
    assert "<redacted:secret:" in redacted


def test_evidence_writer_redact_param_internal():
    """Test redaction of internal-sensitivity parameters."""
    param = ParamSpec(
        name="token",
        type="string",
        required=True,
        description="Internal token",
        example="abc123",
        sensitivity="internal"
    )
    
    redacted = EvidenceWriter.redact_param_value(param, "token_value")
    assert "token_value" not in redacted
    assert "<redacted:internal>" in redacted


def test_evidence_writer_preserves_public():
    """Test that public-sensitivity parameters are not redacted."""
    param = ParamSpec(
        name="member_id",
        type="string",
        required=True,
        description="Member ID",
        example="10001",
        sensitivity="public"
    )
    
    redacted = EvidenceWriter.redact_param_value(param, "10001")
    assert "10001" in redacted, "Public data should not be redacted"
