"""Forge and read the signed session cookie the way ``SessionMiddleware`` does.

``create_app()`` (src/main.py) stores the session as
``TimestampSigner(secret_key).sign(base64(json(session)))`` under the ``copi-session``
cookie, exactly as Starlette's ``SessionMiddleware`` does. Impersonation is not part of
the session: it is the separate ``copi-impersonate`` cookie, which ``session_headers``
appends to the same ``Cookie`` header. A forged session with no ``epoch`` key reads as
epoch 0, which every account whose ``users.session_epoch`` was never bumped accepts.
"""

from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie

from itsdangerous import TimestampSigner

from src.config import get_settings

SESSION_COOKIE = "copi-session"

#: SessionMiddleware's max_age in create_app() (30 days).
SESSION_MAX_AGE = 30 * 24 * 3600


def sign_session(payload: dict) -> str:
    """The cookie value SessionMiddleware would issue for ``payload``."""
    signer = TimestampSigner(get_settings().secret_key)
    return signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()


def raw_session_headers(payload: dict) -> dict[str, str]:
    """Request headers carrying a session holding exactly ``payload``."""
    return {"Cookie": f"{SESSION_COOKIE}={sign_session(payload)}"}


def session_headers(user_id, *, impersonate=None, **extra) -> dict[str, str]:
    """Request headers carrying a signed-in session for ``user_id`` plus ``extra`` keys.

    ``impersonate`` adds the ``copi-impersonate`` cookie naming that user to the same
    ``Cookie`` header; the app honours it only for an admin.
    """
    headers = raw_session_headers({"user_id": str(user_id), **extra})
    if impersonate is not None:
        headers["Cookie"] += f"; copi-impersonate={impersonate}"
    return headers


def session_from_response(response) -> dict | None:
    """Decode the session a response set.

    None: the response set no session cookie (the request's session carried forward
    unchanged). ``{}``: the session was cleared — Starlette re-sets the cookie to the
    literal string "null".
    """
    raw = None
    for k, v in response.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{SESSION_COOKIE}="):
            raw = v
    if raw is None:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    value = jar[SESSION_COOKIE].value
    if not value or value == "null":
        return {}
    signer = TimestampSigner(get_settings().secret_key)
    return json.loads(base64.b64decode(signer.unsign(value, max_age=SESSION_MAX_AGE)))
