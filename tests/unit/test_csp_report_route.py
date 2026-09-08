"""Unit coverage for `POST /api/csp-report` (#27 I5, audit 2026-09-08 RC-5).

nginx's Report-Only CSP header (nginx/nginx.conf) now carries `report-uri
/api/csp-report`, so browsers that violate the reported directives POST a
violation report here. Before this route existed, nothing collected those
reports at all — the header had no `report-uri`/`report-to`, so violations
went nowhere (see tests/unit/test_nginx_config.py for the nginx-side half of
RC-5). The route must be public (no auth), accept both report content types
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


# --- Opus review of RC-5 (2026-09-08) --------------------------------------------
# 1. Content-type enforcement was only DOCUMENTED, never actually checked -- any
#    content-type reached the JSON parser. 2. Logged fields came straight from an
#    attacker-controlled JSON body with no length cap or newline stripping, so a
#    crafted document-uri could inject fake log lines (CRLF log injection).


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


async def test_csp_report_endpoint_truncates_an_oversized_logged_field(caplog):
    long_uri = "https://copi.science/" + "a" * 5000
    injected = json.dumps({"csp-report": {"document-uri": long_uri}}).encode()
    with caplog.at_level(logging.WARNING):
        r = await _post(injected, "application/csp-report")
    assert r.status_code == 204
    [record] = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    msg = record.getMessage()
    assert len(msg) < len(long_uri)
