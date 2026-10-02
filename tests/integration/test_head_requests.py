"""M-02: HEAD answers every GET route with the GET's status and headers and no body."""

import pytest

from src.models import USER_ROLE_ADMIN
from src.web.head_requests import HeadAsGetMiddleware
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

#: Headers that legitimately differ between two requests: the clock, a refreshed
#: session cookie, and the per-request CSP nonce.
_VOLATILE = {"date", "set-cookie", "content-security-policy", "content-security-policy-report-only"}


def _stable(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() not in _VOLATILE}


@pytest.mark.parametrize(
    "path", ["/login", "/manager", "/static/js/assessment_chat.js", "/api/health"]
)
async def test_head_mirrors_get_without_a_body(client, path):
    get = await client.get(path)
    head = await client.head(path)
    assert head.status_code == get.status_code
    assert _stable(head.headers) == _stable(get.headers)
    assert head.content == b""


async def test_head_on_an_authenticated_page_and_its_unauthenticated_redirect(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)
    get = await client.get("/admin/jobs", headers=headers)
    head = await client.head("/admin/jobs", headers=headers)
    assert (head.status_code, head.content) == (get.status_code, b"")
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["content-length"] == get.headers["content-length"]

    anon_get = await client.get("/admin/jobs")
    anon_head = await client.head("/admin/jobs")
    assert anon_head.status_code == anon_get.status_code
    assert anon_head.headers.get("location") == anon_get.headers.get("location")
    assert anon_head.content == b""


async def test_head_on_a_post_only_path_is_405_and_on_an_unknown_path_404(client):
    assert (await client.head("/logout")).status_code == (await client.get("/logout")).status_code == 405
    missing = await client.head("/no-such-page")
    assert (missing.status_code, missing.content) == (404, b"")


def test_the_head_middleware_is_innermost():
    """`user_middleware` runs outermost-first; HEAD must turn into GET only after the
    CSRF guard and the session middleware have seen the real method."""
    from src.main import create_app

    order = [m.cls for m in create_app().user_middleware]
    assert order[-1] is HeadAsGetMiddleware
