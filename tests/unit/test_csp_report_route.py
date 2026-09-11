"""Unit coverage for `POST /api/csp-report`.

nginx's Report-Only CSP header (nginx/nginx.conf) carries `report-uri
/api/csp-report`, so browsers that violate the reported directives POST a
violation report here (see tests/unit/test_nginx_config.py for the nginx-side
half). Without this route, nothing collects those reports — the header would
have no `report-uri`/`report-to`, so violations would go nowhere. The route
must be public (no auth), accept both report content types
browsers actually send, cap the body so it can't be used to fill request logs
or memory, and never 500 on a malformed report.
"""

import base64
import json
import logging
import uuid

import httpx
from httpx import ASGITransport
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.main import create_app

CSP_REPORT_BODY = json.dumps(
    {
        "csp-report": {
            "document-uri": "https://copi.science/agent/wubot",
            "referrer": "",
            "violated-directive": "script-src-elem",
            "effective-directive": "script-src-elem",
            "original-policy": "default-src 'self'",
            "blocked-uri": "https://evil.example/x.js",
        }
    }
).encode()

REPORTS_JSON_BODY = json.dumps(
    [
        {
            "type": "csp-violation",
            "age": 10,
            "url": "https://copi.science/agent/wubot",
            "body": {
                "documentURL": "https://copi.science/agent/wubot",
                "effectiveDirective": "style-src-elem",
                "blockedURL": "inline",
            },
        }
    ]
).encode()


def _auth(user_id) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


async def _post(body: bytes, content_type: str, headers: dict | None = None):
    app = create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(
            "/api/csp-report",
            content=body,
            headers={"Content-Type": content_type, **(headers or {})},
        )


async def test_csp_report_endpoint_accepts_application_csp_report(caplog):
    with caplog.at_level(logging.WARNING):
        r = await _post(CSP_REPORT_BODY, "application/csp-report")
    assert r.status_code == 204
    assert r.content == b""
    [record] = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    msg = record.getMessage()
    assert "https://copi.science/agent/wubot" in msg
    assert "script-src-elem" in msg
    assert "https://evil.example/x.js" in msg


async def test_csp_report_endpoint_accepts_reports_plus_json(caplog):
    with caplog.at_level(logging.WARNING):
        r = await _post(REPORTS_JSON_BODY, "application/reports+json")
    assert r.status_code == 204
    [record] = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    msg = record.getMessage()
    assert "style-src-elem" in msg
    assert "inline" in msg


async def test_csp_report_endpoint_requires_no_authentication():
    # No session cookie at all -- a browser sending a violation report never
    # carries the app's session cookie for a cross-origin/CDN-triggered block.
    r = await _post(CSP_REPORT_BODY, "application/csp-report")
    assert r.status_code == 204


async def test_csp_report_endpoint_rejects_oversized_body_with_413():
    oversized = b'{"csp-report": {"document-uri": "' + b"x" * 8300 + b'"}}'
    assert len(oversized) > 8192
    r = await _post(oversized, "application/csp-report")
    assert r.status_code == 413


async def test_csp_report_endpoint_does_not_500_on_malformed_json():
    r = await _post(b"not json at all", "application/csp-report")
    assert r.status_code == 204


async def test_csp_report_endpoint_rejects_a_chunked_oversized_body_with_413(
    monkeypatch,
):
    """A chunked POST with no Content-Length skips the header-based size
    check entirely, and
    `await request.body()` buffers the WHOLE body before the len() check
    after it ever runs -- so a chunked body could push an unbounded amount
    into memory first. Read via the stream and abort once more than
    CSP_REPORT_MAX_BODY_BYTES have arrived, without ever buffering the
    whole thing.

    ``Request.body()`` is monkeypatched to fail the test if called at all --
    that pins the "never fully materialize the body" behavior, since a
    status-code-only assertion can't otherwise tell a streaming read apart
    from a buffered read followed by the same len() check.
    """
    from starlette.requests import Request

    def _must_not_be_called(self):
        raise AssertionError(
            "request.body() must not be called -- it buffers the whole body "
            "before any size check can reject it"
        )

    monkeypatch.setattr(Request, "body", _must_not_be_called)

    async def _chunked_body():
        # 100 KB, well over CSP_REPORT_MAX_BODY_BYTES (8 KB), in small chunks
        # so more than one iteration of the stream loop is exercised.
        for _ in range(100):
            yield b"a" * 1024

    app = create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post(
            "/api/csp-report",
            content=_chunked_body(),
            headers={"Content-Type": "application/csp-report"},
        )
    assert "content-length" not in {k.lower() for k in r.request.headers.keys()}, (
        "the request must actually be chunked (no Content-Length) for this "
        "test to exercise the stream-based size check"
    )
    assert r.status_code == 413


class _ExplodingSessionFactory:
    """Raises if the badge middleware ever tries to open a DB session for this
    request -- proves /api/csp-report is excluded the same way /api/health is
    (src/main.py's AgentBadgeMiddleware path check), so an authenticated
    browser's violation report can't be slowed down or failed by badge-count
    queries."""

    def __call__(self):
        raise AssertionError(
            "AgentBadgeMiddleware opened a DB session for /api/csp-report -- "
            "it must be excluded like /api/health"
        )


async def test_csp_report_endpoint_is_excluded_from_badge_middleware(monkeypatch):
    monkeypatch.setattr("src.main.get_session_factory", _ExplodingSessionFactory())
    app = create_app()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post(
            "/api/csp-report",
            content=CSP_REPORT_BODY,
            headers={"Content-Type": "application/csp-report", **_auth(uuid.uuid4())},
        )
    assert r.status_code == 204


# --- Content-type enforcement and log-injection hardening -----------------------
# Content-type enforcement must be actually checked, not just documented -- any
# content-type must not reach the JSON parser unchecked. Logged fields come
# straight from an attacker-controlled JSON body, so they need a length cap
# and newline stripping to prevent a crafted document-uri injecting fake log
# lines (CRLF log injection).


async def test_csp_report_endpoint_accepts_application_json():
    r = await _post(CSP_REPORT_BODY, "application/json")
    assert r.status_code == 204


async def test_csp_report_endpoint_rejects_unsupported_content_type_with_415():
    r = await _post(CSP_REPORT_BODY, "text/plain")
    assert r.status_code == 415


async def test_csp_report_endpoint_rejects_unsupported_content_type_even_with_valid_body():
    # A wrong content-type is refused before the body is even inspected -- a
    # perfectly well-formed report must not slip through on the wrong header.
    r = await _post(REPORTS_JSON_BODY, "application/xml")
    assert r.status_code == 415


async def test_csp_report_endpoint_ignores_content_type_parameters():
    # `application/csp-report; charset=utf-8` is a real shape browsers send —
    # only the media type (before the first ';') should be checked.
    r = await _post(CSP_REPORT_BODY, "application/csp-report; charset=utf-8")
    assert r.status_code == 204


async def test_csp_report_endpoint_sanitizes_an_embedded_newline_in_logged_fields(caplog):
    injected = json.dumps(
        {
            "csp-report": {
                "document-uri": "https://copi.science/x\n2026-09-08 CRITICAL fake log line",
                "violated-directive": "script-src-elem",
                "blocked-uri": "https://evil.example/y\rinjected",
            }
        }
    ).encode()
    with caplog.at_level(logging.WARNING):
        r = await _post(injected, "application/csp-report")
    assert r.status_code == 204
    [record] = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    msg = record.getMessage()
    assert "\n" not in msg
    assert "\r" not in msg
    # The record itself is one line in any line-oriented log sink, i.e. `str(record)`
    # / the formatted message has no embedded newline that could masquerade as a
    # second, forged log line.
    assert len(msg.splitlines()) == 1


async def test_csp_report_endpoint_strips_non_printable_chars_from_logged_fields(caplog):
    # \n/\r are not the only way to forge a fake log line or corrupt a
    # terminal/log viewer -- an ANSI escape sequence and
    # Unicode's U+2028 LINE SEPARATOR are both non-printable and neither was
    # stripped by the old CR/LF-only replace().
    injected = json.dumps(
        {
            "csp-report": {
                "document-uri": "https://copi.science/x\x1b[31mFAKE\x1b[0m",
                "violated-directive": "script-src-elem",
                "blocked-uri": "https://evil.example/y\u2028injected",
            }
        }
    ).encode()
    with caplog.at_level(logging.WARNING):
        r = await _post(injected, "application/csp-report")
    assert r.status_code == 204
    [record] = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    msg = record.getMessage()
    assert "\x1b" not in msg
    assert "\u2028" not in msg
    assert len(msg.splitlines()) == 1


async def test_csp_report_endpoint_truncates_an_oversized_logged_field(caplog):
    long_uri = "https://copi.science/" + "a" * 5000
    injected = json.dumps({"csp-report": {"document-uri": long_uri}}).encode()
    with caplog.at_level(logging.WARNING):
        r = await _post(injected, "application/csp-report")
    assert r.status_code == 204
    [record] = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    msg = record.getMessage()
    assert len(msg) < len(long_uri)
