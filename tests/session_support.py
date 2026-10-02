"""Forge and read the signed session cookie the way ``SessionMiddleware`` does.

``create_app()`` (src/main.py) stores the session as
``TimestampSigner(secret_key).sign(base64(json(session)))`` under
``session_cookie_name(settings)``. The suite reads ``.env``; the host's sets
``ALLOW_HTTP_SESSIONS=false`` and the setting defaults to false, so tests normally run
under ``__Host-copi-session``. Every helper asks the app for the name instead of
spelling it. A forged session with no ``epoch`` key reads as epoch 0, which every
account whose ``users.session_epoch`` was never bumped accepts.
"""

from __future__ import annotations

import base64
import json
import time
from http.cookies import SimpleCookie

from itsdangerous import TimestampSigner

from src.config import get_settings
from src.dependencies import IMPERSONATE_EXPIRES_KEY, IMPERSONATE_KEY, IMPERSONATION_MAX_AGE
from src.main import session_cookie_name as _cookie_name_for

#: SessionMiddleware's max_age in create_app() (30 days).
SESSION_MAX_AGE = 30 * 24 * 3600


def session_cookie_name() -> str:
    """The session cookie name create_app() uses under the current settings."""
    return _cookie_name_for(get_settings())


def sign_session(payload: dict) -> str:
    """The cookie value SessionMiddleware would issue for ``payload``."""
    signer = TimestampSigner(get_settings().secret_key)
    return signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()


def raw_session_headers(payload: dict) -> dict[str, str]:
    """Request headers carrying a session holding exactly ``payload``."""
    return {"Cookie": f"{session_cookie_name()}={sign_session(payload)}"}


def session_headers(user_id, *, impersonate=None, **extra) -> dict[str, str]:
    """Request headers carrying a signed-in session for ``user_id`` plus ``extra`` keys.

    ``impersonate`` signs an impersonation of that user into the session, with the
    expiry POST /admin/impersonate would give it; it is honoured only for an admin.
    """
    payload = {"user_id": str(user_id), **extra}
    if impersonate is not None:
        payload[IMPERSONATE_KEY] = str(impersonate)
        payload[IMPERSONATE_EXPIRES_KEY] = int(time.time()) + IMPERSONATION_MAX_AGE
    return raw_session_headers(payload)


def session_from_response(response) -> dict | None:
    """Decode the session a response set.

    None: the response set no session cookie (the request's session carried forward
    unchanged). ``{}``: the session was cleared — Starlette re-sets the cookie to the
    literal string "null".
    """
    name = session_cookie_name()
    raw = None
    for k, v in response.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{name}="):
            raw = v
    if raw is None:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    value = jar[name].value
    if not value or value == "null":
        return {}
    signer = TimestampSigner(get_settings().secret_key)
    return json.loads(base64.b64decode(signer.unsign(value, max_age=SESSION_MAX_AGE)))
