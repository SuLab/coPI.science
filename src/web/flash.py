"""One-shot page messages carried across a POST-redirect-GET in the signed session
(FN-09, A-14).

A message used to ride in the redirect's query string (``?error=...``,
``?slack_error=...``), where anyone could link a victim to a page saying whatever they
liked, and unquoted URL building mangled the text. ``flash`` stores it in the session,
which ``SessionMiddleware`` signs, and ``base.html`` shows and removes it on the next full
page render.

The context processor hands templates a callable rather than the popped list: Starlette
runs every context processor for every ``TemplateResponse``, partials included
(``agent/_thread_replies.html`` is fetched by script and extends nothing), and an eager pop
there would swallow a message meant for the next real page. Only ``base.html`` calls
``get_flashes()``.

Login clears the session (``src/routers/auth.py::_start_fresh_session``), so a message
queued before a login redirect is dropped; one queued after login survives one page view.
"""
from typing import Any

from starlette.requests import Request

#: Session key holding the pending queue: a list of ``{"text", "kind"}`` dicts.
FLASH_SESSION_KEY = "_flashes"
#: The kinds ``base.html`` styles; anything else is a programming error.
FLASH_KINDS = ("info", "success", "error")
#: The session is one signed cookie (browsers cap a cookie near 4 KB), so a message is
#: cut to this many characters and at most ``MAX_FLASHES`` are kept, newest last.
MAX_FLASH_CHARS = 300
MAX_FLASHES = 5


def flash(request: Request, text: str, kind: str = "info") -> None:
    """Queue ``text`` for the next full page this session renders."""
    if kind not in FLASH_KINDS:
        raise ValueError(f"unknown flash kind {kind!r}; expected one of {FLASH_KINDS}")
    queue = list(request.session.get(FLASH_SESSION_KEY) or [])
    queue.append({"text": str(text)[:MAX_FLASH_CHARS], "kind": kind})
    # Reassigned, not appended in place: Starlette's Session marks itself modified (and
    # re-sends the cookie) only on item assignment, in 1.4.1 and 1.7.0 alike.
    request.session[FLASH_SESSION_KEY] = queue[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[dict[str, str]]:
    """Remove and return the pending messages; ``[]`` when the request has no session."""
    if "session" not in request.scope:
        return []
    return list(request.session.pop(FLASH_SESSION_KEY, None) or [])


def flash_context(request: Request) -> dict[str, Any]:
    """Context processor: ``get_flashes()`` drains the queue when ``base.html`` calls it."""

    def get_flashes() -> list[dict[str, str]]:
        return pop_flashes(request)

    return {"get_flashes": get_flashes}
