"""A flash set by one request renders once, escaped, on the next full page (FN-09)."""
import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse

from src.web.flash import flash
from tests import factories
from tests.flash_support import session_cookie_header, session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_a_flash_renders_once_on_the_next_page(asgi_app, client, db_session):
    async def _flash_then_redirect(request: Request):
        flash(request, "Saved <b>ok</b>", "success")
        return RedirectResponse(url="/settings", status_code=302)

    asgi_app.add_api_route("/__test_flash", _flash_then_redirect, methods=["GET"])
    user = await factories.make_user(db_session)

    first = await client.get("/__test_flash", headers=auth_headers(user.id), follow_redirects=False)
    assert first.status_code == 302
    assert session_flashes(first) == [{"text": "Saved <b>ok</b>", "kind": "success"}]

    page = await client.get("/settings", headers=session_cookie_header(first))
    assert page.status_code == 200
    assert 'data-flash-kind="success"' in page.text
    assert "Saved &lt;b&gt;ok&lt;/b&gt;" in page.text
    assert session_flashes(page) == []

    again = await client.get("/settings", headers=session_cookie_header(page))
    assert "Saved &lt;b&gt;ok&lt;/b&gt;" not in again.text
