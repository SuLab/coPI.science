"""Error responses for browser navigation (M-01).

FastAPI answers every ``HTTPException`` and ``RequestValidationError`` with JSON, so a 403,
404 or 422 reached by clicking a link showed raw ``{"detail": ...}``. One handler per
exception class now decides:

* a 3xx (``get_current_user`` raises ``HTTPException(302, headers={"Location": ...})``)
  and anything else below 400 goes to FastAPI's own handler, unchanged;
* JSON, exactly as before, for paths under ``JSON_PATH_PREFIXES`` (the assessment chat's
  ``fetch`` calls and the API) and for any request whose ``Accept`` names
  ``application/json``;
* otherwise ``templates/error.html`` with the same status code and headers (``Allow`` on a
  405 survives).

The page shows the exception's own detail when it is a sentence a handler wrote, and
nothing when it is Starlette's generic phrase ("Not Found") or a validation error list.

An unhandled exception gets the same split (``_server_error``), with the security headers
added by hand because ``ServerErrorMiddleware`` runs outside ``SecurityHeadersMiddleware``.
"""
from collections.abc import Mapping
from http import HTTPStatus
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from src.config import get_settings
from src.web.security_headers import new_nonce, security_header_items
from src.web.templating import make_templates

#: Path prefixes whose callers are scripts; they keep FastAPI's JSON error bodies.
JSON_PATH_PREFIXES: tuple[str, ...] = ("/assessment-chat/", "/api/")

#: The page heading per status; any other status uses its HTTP reason phrase.
ERROR_TITLES: dict[int, str] = {
    400: "Bad request",
    401: "Sign-in required",
    403: "Access denied",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Request too large",
    422: "Invalid request",
    429: "Too many requests",
    500: "Something went wrong",
}

_templates = make_templates()


def _phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def wants_json(request: Request) -> bool:
    """True for a script caller: a JSON path prefix, or ``Accept`` naming JSON."""
    if request.url.path.startswith(JSON_PATH_PREFIXES):
        return True
    return "application/json" in request.headers.get("accept", "").lower()


def _back_href(request: Request) -> str:
    """The same-origin ``Referer`` as a local path, else ``/``: a foreign referer is never
    echoed into a link."""
    referer = request.headers.get("referer")
    if not referer:
        return "/"
    parts = urlsplit(referer)
    base = urlsplit(get_settings().base_url)
    if (parts.scheme, parts.netloc.lower()) != (base.scheme, base.netloc.lower()):
        return "/"
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    return path + (f"?{parts.query}" if parts.query else "")


def error_page(
    request: Request,
    status_code: int,
    detail: str | None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """``templates/error.html`` for ``status_code``."""
    return _templates.TemplateResponse(
        request,
        "error.html",
        {
            "status_code": status_code,
            "title": ERROR_TITLES.get(status_code) or _phrase(status_code),
            "detail": detail,
            "back_href": _back_href(request),
        },
        status_code=status_code,
        headers=dict(headers) if headers else None,
    )


async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code < 400 or wants_json(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else None
    if detail == _phrase(exc.status_code):
        detail = None
    return error_page(request, exc.status_code, detail, getattr(exc, "headers", None))


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    if wants_json(request):
        return await request_validation_exception_handler(request, exc)
    return error_page(request, 422, None)


async def _server_error(request: Request, exc: Exception) -> Response:
    """Unhandled exceptions. ``ServerErrorMiddleware`` calls this OUTSIDE every user
    middleware, so the security headers are added here. The exception's text is never
    shown; the server logs it when the middleware re-raises."""
    nonce = getattr(request.state, "csp_nonce", None) or new_nonce()
    request.state.csp_nonce = nonce
    if wants_json(request):
        response: Response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    else:
        response = error_page(request, 500, None)
    for name, value in security_header_items(nonce):
        response.headers[name] = value
    return response


def install_error_handlers(app: FastAPI) -> None:
    """Replace FastAPI's JSON-only handlers with the browser-aware pair above, and give an
    unhandled exception the same HTML/JSON split."""
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _server_error)
