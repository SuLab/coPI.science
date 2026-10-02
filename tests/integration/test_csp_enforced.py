"""Spec §7 "CSP": the §6.3 script policy is enforced, `report-uri` kept, the
report-only header gone. `form-action` admits Slack, where both provisioning forms
redirect."""

import re

import pytest

from src.models import USER_ROLE_ADMIN
from src.web.security_headers import REPORT_ONLY_POLICY, SCRIPT_POLICY_ENFORCED
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_DIRECTIVES = (
    "default-src 'self'", "style-src 'self' 'unsafe-inline'", "img-src 'self' data:",
    "font-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'",
    "object-src 'none'", "report-uri /api/csp-report",
)


def _policy(response) -> str:
    assert "content-security-policy-report-only" not in response.headers
    return response.headers["content-security-policy"]


async def test_pages_carry_the_enforced_policy_with_a_fresh_nonce(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    first = _policy(await client.get("/admin/jobs", headers=auth_headers(admin.id)))
    second = _policy(await client.get("/login"))
    for policy in (first, second):
        for directive in _DIRECTIVES:
            assert directive in policy, directive
        assert re.search(r"script-src 'self' 'nonce-[A-Za-z0-9_-]+'", policy)
    nonce = re.compile(r"'nonce-([^']+)'")
    assert nonce.search(first).group(1) != nonce.search(second).group(1)


async def test_the_enforced_policy_lets_the_slack_provisioning_redirect_through(client):
    assert "form-action 'self' https://slack.com https://*.slack.com" in _policy(await client.get("/login"))


async def test_error_and_head_responses_are_covered_too(client):
    assert "script-src" in _policy(await client.get("/no-such-page", headers={"Accept": "text/html"}))
    assert "script-src" in _policy(await client.head("/login"))


def test_the_script_policy_is_enforced_and_takes_the_nonce():
    assert SCRIPT_POLICY_ENFORCED is True
    assert "'nonce-abc'" in REPORT_ONLY_POLICY.format(nonce="abc")
