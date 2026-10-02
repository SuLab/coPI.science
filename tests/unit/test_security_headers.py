"""Policies and header values of src/web/security_headers.py (spec §6.3)."""

import re

from src.web.security_headers import (
    CSP_REPORT_PATH,
    ENFORCED_POLICY,
    REPORT_ONLY_POLICY,
    SCRIPT_POLICY_ENFORCED,
    new_nonce,
    security_header_items,
)


def test_the_enforced_policy_is_the_spec_text():
    assert ENFORCED_POLICY == "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


def test_the_script_policy_is_the_spec_text():
    assert CSP_REPORT_PATH == "/api/csp-report"
    assert REPORT_ONLY_POLICY.format(nonce="N") == (
        "default-src 'self'; script-src 'self' 'nonce-N'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; form-action 'self' https://slack.com https://*.slack.com; report-uri /api/csp-report"
    )


def test_phase_2_enforces_the_script_policy():
    """One combined enforced header, no report-only header (spec §7 "CSP")."""
    assert SCRIPT_POLICY_ENFORCED is True
    items = security_header_items("N")
    assert dict(items) == {
        "Content-Security-Policy": ENFORCED_POLICY + "; " + REPORT_ONLY_POLICY.format(nonce="N"),
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    assert len(items) == len(dict(items))
    assert "report-uri /api/csp-report" in dict(items)["Content-Security-Policy"]


def test_the_rollback_switch_restores_the_phase_1_split():
    items = dict(security_header_items("N", enforce_script_policy=False))
    assert items["Content-Security-Policy"] == ENFORCED_POLICY
    assert items["Content-Security-Policy-Report-Only"] == REPORT_ONLY_POLICY.format(nonce="N")


def test_nonces_are_fresh_and_need_no_escaping():
    nonces = {new_nonce() for _ in range(200)}
    assert len(nonces) == 200
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{22}", n) for n in nonces)
