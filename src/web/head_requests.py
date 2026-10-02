"""HEAD on every GET route (M-02, docs/specs/2026-10-01-web-ui-remediation-design.md §7).

FastAPI's ``APIRoute`` registers exactly the methods it is given, so a
``@router.get`` route answers ``HEAD`` with 405; Starlette's own ``Route`` and
``StaticFiles`` handle ``HEAD`` but cover none of the API routers. This middleware
runs a ``HEAD`` as the ``GET`` it mirrors and drops the body, so status,
``Location`` and every header (``Content-Length`` included) are the ``GET``'s.

Registered FIRST in ``create_app()``, i.e. innermost: the CSRF guard and the
session middleware still see the real method (``HEAD`` is in ``SAFE_METHODS``);
only routing and handlers see ``GET``. A ``HEAD`` on a path with no ``GET`` route
gets the same 405 a ``GET`` would.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BODY_MESSAGES = ("http.response.body", "http.response.pathsend")
_EMPTY_FINAL_BODY: Message = {"type": "http.response.body", "body": b"", "more_body": False}


class HeadAsGetMiddleware:
    """Pure ASGI, so it works the same on every Starlette version."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        async def send_without_body(message: Message) -> None:
            if message["type"] in _BODY_MESSAGES:
                if not message.get("more_body", False):
                    await send(_EMPTY_FINAL_BODY)
                return
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, send_without_body)
