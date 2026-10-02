"""``POST /api/csp-report``: the sink for browsers' CSP violation reports (spec §6.3).

Browsers post here because ``report-uri`` names this path, both in the app's own
policy (src/web/security_headers.py; enforced since Phase 2) and in the shared nginx's
report-only policy on this vhost. No authentication, and exempt from ``OriginGuardMiddleware`` for this
exact path only: a violation report is not a form post from one of our pages and
may carry no Origin, or an opaque one. It stores nothing. Each report becomes one
JSON log line, bounded in size and count, so the endpoint cannot be turned into a
log flood or a log forgery.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.web.security_headers import CSP_REPORT_PATH

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 16 * 1024
MAX_REPORTS_PER_REQUEST = 20
MAX_FIELD_CHARS = 512
#: Process-wide cap on logged report lines (plan audit Q1-10): the route is anonymous
#: and Origin-exempt, so without it a flood could rotate the json-file logs and erase
#: recent warnings. Suppressed reports are counted and summarised once per window.
MAX_LINES_PER_WINDOW = 60
WINDOW_SECONDS = 60.0
_window = {"start": 0.0, "logged": 0, "suppressed": 0}
#: The window's clock; a module attribute so a test can move it without patching the
#: time module the event loop also uses.
_clock = time.monotonic


def _admit(n: int, now: float) -> int:
    """How many of ``n`` report lines may be logged now; counts the rest."""
    if now - _window["start"] >= WINDOW_SECONDS:
        if _window["suppressed"]:
            logger.info("csp_report suppressed=%d in the last window", _window["suppressed"])
        _window.update(start=now, logged=0, suppressed=0)
    room = max(0, MAX_LINES_PER_WINDOW - _window["logged"])
    take = min(n, room)
    _window["logged"] += take
    _window["suppressed"] += n - take
    return take


#: ``report-uri`` sends the first; the Reporting API (``report-to``) the second.
_CONTENT_TYPES = frozenset({"application/csp-report", "application/reports+json"})


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None once it is known to exceed MAX_BODY_BYTES (the rest is
    never read)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def _field(value: Any) -> str:
    return "" if value is None else str(value)[:MAX_FIELD_CHARS]


def _reports(content_type: str, payload: Any) -> list[dict[str, str]]:
    """(directive, blocked URI, document URI) for each CSP report in the payload."""
    if content_type == "application/csp-report":
        body = payload.get("csp-report") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            return []
        return [{
            "directive": _field(body.get("effective-directive") or body.get("violated-directive")),
            "blocked_uri": _field(body.get("blocked-uri")),
            "document_uri": _field(body.get("document-uri")),
        }]
    if not isinstance(payload, list):
        return []
    out = []
    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "csp-violation":
            continue
        body = item.get("body")
        if not isinstance(body, dict):
            continue
        out.append({
            "directive": _field(body.get("effectiveDirective")),
            "blocked_uri": _field(body.get("blockedURL")),
            "document_uri": _field(body.get("documentURL")),
        })
    return out


@router.post(CSP_REPORT_PATH)
async def csp_report(request: Request) -> Response:
    """Log each report (one JSON line apiece) and answer 204."""
    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type not in _CONTENT_TYPES:
        return Response(status_code=415)
    raw = await _read_capped(request)
    if raw is None:
        return Response(status_code=413)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers malformed JSON and undecodable bytes; RecursionError is
        # what CPython's json raises on nesting a 16 KB body can still hold.
        return Response(status_code=400)
    reports = _reports(content_type, payload)[:MAX_REPORTS_PER_REQUEST]
    for report in reports[: _admit(len(reports), _clock())]:
        logger.info("csp_report %s", json.dumps(report, sort_keys=True))
    return Response(status_code=204)
