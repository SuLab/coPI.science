"""M-01: a browser gets an HTML error page; scripts keep FastAPI's JSON; redirects pass."""
from urllib.parse import urlsplit

import pytest

from src.config import get_settings
from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _origin() -> str:
    parts = urlsplit(get_settings().base_url)
    return f"{parts.scheme}://{parts.netloc}"


async def test_a_browser_403_renders_the_error_page(client, db_session):
    pi = await factories.make_user(db_session)
    r = await client.get("/admin/users", headers=auth_headers(pi.id))
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/html")
    assert "<h1" in r.text and "Access denied" in r.text
    assert "Admin access required" in r.text  # the exception's own detail
    assert 'href="/"' in r.text
    assert "Sign out" not in r.text and 'href="/login"' not in r.text


async def test_accept_json_keeps_the_json_body(client, db_session):
    pi = await factories.make_user(db_session)
    r = await client.get(
        "/admin/users", headers={**auth_headers(pi.id), "Accept": "application/json"}
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "Admin access required"}


async def test_an_unknown_page_is_an_html_404_without_the_generic_phrase(client):
    r = await client.get("/no-such-page-here")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "Page not found" in r.text
    assert "Not Found" not in r.text


async def test_paths_under_api_and_assessment_chat_stay_json(client):
    for path in ("/api/no-such-endpoint", "/assessment-chat/a/b/c"):
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.headers["content-type"].startswith("application/json"), path
        assert r.json() == {"detail": "Not Found"}, path


async def test_a_validation_error_is_an_html_422(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get("/workspace/activity/not-a-uuid", headers=auth_headers(admin.id))
    assert r.status_code == 422
    assert r.headers["content-type"].startswith("text/html")
    assert "Invalid request" in r.text


async def test_a_validation_error_with_accept_json_keeps_the_error_list(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(
        "/workspace/activity/not-a-uuid",
        headers={**auth_headers(admin.id), "Accept": "application/json"},
    )
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


async def test_the_login_redirect_passes_through(client):
    r = await client.get("/profile", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


async def test_a_405_keeps_its_allow_header(client):
    r = await client.put("/login")
    assert r.status_code == 405
    assert "allow" in r.headers
    assert "Method not allowed" in r.text


async def test_back_goes_to_a_same_origin_referer_only(client, db_session):
    pi = await factories.make_user(db_session)
    same = await client.get(
        "/admin/users",
        headers={**auth_headers(pi.id), "Referer": f"{_origin()}/settings?tab=email"},
    )
    assert 'href="/settings?tab=email"' in same.text
    foreign = await client.get(
        "/admin/users",
        headers={**auth_headers(pi.id), "Referer": "https://evil.example/x"},
    )
    assert "evil.example" not in foreign.text


async def test_an_unhandled_error_renders_the_page_with_security_headers(asgi_app):
    import httpx
    from httpx import ASGITransport

    async def boom():
        raise RuntimeError("boom")

    asgi_app.add_api_route("/__uiaudit_boom", boom)
    transport = ASGITransport(app=asgi_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        page = await c.get("/__uiaudit_boom", headers={"Accept": "text/html"})
        api = await c.get("/__uiaudit_boom", headers={"Accept": "application/json"})
    assert page.status_code == 500
    assert page.headers["content-type"].startswith("text/html")
    assert "<h1" in page.text and "boom" not in page.text
    assert page.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert api.status_code == 500 and api.json() == {"detail": "Internal Server Error"}
    assert api.headers["X-Content-Type-Options"] == "nosniff"
