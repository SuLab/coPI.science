"""Read the flash queue (src/web/flash.py) out of the session cookie a response set.

Built on ``tests/session_support.py``, which asks the app for the session cookie name
(``__Host-copi-session`` or ``copi-session``) and decodes it as ``SessionMiddleware`` signs it.
"""
from http.cookies import SimpleCookie

from src.web.flash import FLASH_SESSION_KEY
from tests.session_support import session_cookie_name, session_from_response


def session_after(response) -> dict:
    """The session the response's Set-Cookie carries; ``{}`` when it set none or cleared it."""
    return session_from_response(response) or {}


def session_flashes(response) -> list[dict[str, str]]:
    """The pending flash queue after ``response``."""
    return list(session_after(response).get(FLASH_SESSION_KEY) or [])


def session_cookie_header(response) -> dict[str, str]:
    """A ``Cookie`` header replaying the session ``response`` set, for the next request."""
    name = session_cookie_name()
    raw = None
    for k, v in response.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{name}="):
            raw = v
    assert raw is not None, "the response set no session cookie"
    jar = SimpleCookie()
    jar.load(raw)
    return {"Cookie": f"{name}={jar[name].value}"}
