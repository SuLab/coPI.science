"""The session cookie's name follows the setting (spec 2026-10-01 §6.7, A-05):
`__Host-copi-session` when it is Secure, `copi-session` over plain HTTP."""
import httpx
import pytest
from httpx import ASGITransport

import src.main as main_module
from src.config import get_settings

pytestmark = pytest.mark.integration


def test_the_name_follows_allow_http_sessions():
    real = get_settings()
    secure = real.model_copy(update={"allow_http_sessions": False})
    plain = real.model_copy(update={"allow_http_sessions": True})
    assert main_module.session_cookie_name(secure) == "__Host-copi-session"
    assert main_module.session_cookie_name(plain) == "copi-session"


async def _session_set_cookies(monkeypatch, *, allow_http: bool) -> list[str]:
    real = get_settings()
    monkeypatch.setattr(
        main_module, "get_settings",
        lambda: real.model_copy(update={"allow_http_sessions": allow_http}),
    )
    app = main_module.create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        # A safe `next` makes /login write the session, so a Set-Cookie is issued.
        r = await c.get("/login", params={"next": "/profile"})
    return [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]


async def test_a_secure_session_cookie_meets_the_host_prefix_rules(monkeypatch):
    cookies = await _session_set_cookies(monkeypatch, allow_http=False)
    assert len(cookies) == 1, cookies
    cookie = cookies[0]
    assert cookie.startswith("__Host-copi-session="), cookie
    assert "; path=/;" in cookie
    assert "secure" in cookie.lower()
    assert "domain=" not in cookie.lower()


async def test_a_plain_http_session_cookie_keeps_the_old_name(monkeypatch):
    cookies = await _session_set_cookies(monkeypatch, allow_http=True)
    assert len(cookies) == 1, cookies
    assert cookies[0].startswith("copi-session=")
    assert "secure" not in cookies[0].lower()
