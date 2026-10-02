"""Forge the signed session cookie ``SessionMiddleware`` would issue.

Identical construction to ``tests/session_support.py`` —
``itsdangerous.TimestampSigner(secret_key)`` over ``base64(json(session))``,
under ``src.main.SESSION_COOKIE_HTTP`` (``copi-session``): every instance these
flows and the ui_audit harness drive runs ``ALLOW_HTTP_SESSIONS=true``. Kept in
its own module so both the pytest flows and the host-side ``auth_helper`` can use it.

This is how the browser flows authenticate. It is a deliberate bypass of ORCID
login, which is broken in this deployment (root cause in ``README.md``), not a
convenience: the flows under test are the *admin* and *agent* surfaces, and
holding them hostage to a third-party OAuth outage would test ORCID rather than
us. It requires the signing key, so it is no weaker than the deployment already
is.
"""

import base64
import json

from itsdangerous import TimestampSigner

from src.main import SESSION_COOKIE_HTTP

COOKIE_NAME = SESSION_COOKIE_HTTP


def forge_session_cookie(user_id: str, **extra) -> str:
    """Return the cookie *value* for a session holding ``user_id``."""
    from src.config import get_settings

    signer = TimestampSigner(get_settings().secret_key)
    payload = {"user_id": str(user_id), **extra}
    data = base64.b64encode(json.dumps(payload).encode())
    return signer.sign(data).decode()


def auth_headers(user_id: str, **extra) -> dict[str, str]:
    """Request headers carrying a forged session for ``user_id``."""
    return {"Cookie": f"{COOKIE_NAME}={forge_session_cookie(user_id, **extra)}"}
