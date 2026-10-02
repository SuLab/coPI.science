"""SecurityHeadersMiddleware on real responses (spec §6.3, M-03, A-06)."""

import re

import pytest

from src.models import USER_ROLE_ADMIN
from src.web.security_headers import ENFORCED_POLICY, REPORT_ONLY_POLICY
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _nonce(response) -> str:
    m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", response.headers["content-security-policy-report-only"])
    assert m, response.headers["content-security-policy-report-only"]
    return m.group(1)


def _assert_headers(response) -> None:
    assert response.headers["content-security-policy"] == ENFORCED_POLICY
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["content-security-policy-report-only"] == REPORT_ONLY_POLICY.format(
        nonce=_nonce(response)
    )
    assert len(response.headers.get_list("content-security-policy")) == 1


async def test_a_page_carries_every_security_header(client):
    r = await client.get("/login")
    assert r.status_code == 200
    _assert_headers(r)


async def test_every_inline_script_carries_this_responses_nonce(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await db_session.flush()
    for path, headers in (("/login", {}), ("/admin/users", auth_headers(admin.id))):
        r = await client.get(path, headers=headers)
        assert r.status_code == 200, path
        nonce = _nonce(r)
        inline = [t for t in re.findall(r"<script\b[^>]*>", r.text) if "src=" not in t]
        assert inline, f"{path} rendered no inline script; base.html has one"
        assert all(f'nonce="{nonce}"' in t for t in inline), (path, inline)


async def test_each_response_gets_a_fresh_nonce(client):
    first = await client.get("/login")
    second = await client.get("/login")
    assert _nonce(first) != _nonce(second)


@pytest.mark.parametrize("path", ["/static/js/ui.js", "/no-such-page", "/api/health"])
async def test_non_page_responses_carry_the_headers_too(client, path):
    r = await client.get(path)
    _assert_headers(r)


async def test_the_origin_guards_refusal_carries_the_headers(client_without_origin):
    r = await client_without_origin.post("/logout")
    assert r.status_code == 403
    assert r.text == "Cross-site request refused."
    _assert_headers(r)
