"""POST /api/csp-report (spec §6.3, M-03): the browsers' CSP violation reports."""

import json
import logging

import pytest

pytestmark = pytest.mark.integration

LOGGER = "src.routers.csp_report"

LEGACY = {
    "csp-report": {
        "document-uri": "https://blackbird.copi.science/admin/users",
        "violated-directive": "script-src-elem",
        "effective-directive": "script-src-elem",
        "blocked-uri": "inline",
        "original-policy": "default-src 'self'",
    }
}
REPORTING_API = [
    {
        "type": "csp-violation",
        "age": 0,
        "url": "https://blackbird.copi.science/admin/jobs",
        "body": {
            "documentURL": "https://blackbird.copi.science/admin/jobs",
            "blockedURL": "https://cdn.example/x.js",
            "effectiveDirective": "script-src-elem",
            "disposition": "report",
        },
    }
]


def _lines(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        if rec.name == LOGGER and rec.getMessage().startswith("csp_report {"):
            out.append(json.loads(rec.getMessage()[len("csp_report "):]))
    return out


async def test_a_legacy_report_is_logged_and_answered_204(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    r = await client_without_origin.post(
        "/api/csp-report",
        content=json.dumps(LEGACY),
        headers={"Content-Type": "application/csp-report"},
    )
    assert r.status_code == 204
    assert r.content == b""
    assert _lines(caplog) == [
        {
            "directive": "script-src-elem",
            "blocked_uri": "inline",
            "document_uri": "https://blackbird.copi.science/admin/users",
        }
    ]


async def test_a_reporting_api_batch_is_logged_one_line_per_report(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    other = {"type": "deprecation", "body": {"id": "x"}}
    r = await client_without_origin.post(
        "/api/csp-report",
        content=json.dumps(REPORTING_API + [other]),
        headers={"Content-Type": "application/reports+json; charset=utf-8"},
    )
    assert r.status_code == 204
    assert _lines(caplog) == [
        {
            "directive": "script-src-elem",
            "blocked_uri": "https://cdn.example/x.js",
            "document_uri": "https://blackbird.copi.science/admin/jobs",
        }
    ]


async def test_another_content_type_is_refused_unread(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    r = await client_without_origin.post(
        "/api/csp-report", content=json.dumps(LEGACY), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 415
    assert _lines(caplog) == []


async def test_a_declared_oversize_body_is_refused(client_without_origin):
    r = await client_without_origin.post(
        "/api/csp-report",
        content=b"{" + b" " * (16 * 1024) + b"}",
        headers={"Content-Type": "application/csp-report"},
    )
    assert r.status_code == 413


async def test_a_streamed_oversize_body_is_refused(client_without_origin):
    """No Content-Length: the cap is enforced while reading, not only from the header."""

    async def chunks():
        for _ in range(3):
            yield b" " * 8192

    r = await client_without_origin.post(
        "/api/csp-report", content=chunks(), headers={"Content-Type": "application/csp-report"}
    )
    assert r.status_code == 413


@pytest.mark.parametrize(
    "body",
    # 16000 unclosed brackets fit under MAX_BODY_BYTES yet nest deeper than CPython's json
    # recursion limit on 3.12 (the suite; ~10000) and 3.11 (the images; far lower).
    [b"not json", b"[" * 16000, b"\xff\xfe"],
    ids=["garbage", "deep-nesting", "bad-utf8"],
)
async def test_an_unparseable_body_is_a_400_not_a_500(client_without_origin, body):
    r = await client_without_origin.post(
        "/api/csp-report", content=body, headers={"Content-Type": "application/reports+json"}
    )
    assert r.status_code == 400


async def test_a_flood_of_reports_is_capped(client_without_origin, caplog, monkeypatch):
    """100 small reports (about 10 KB, under the body cap) log 20 lines: the rest of
    that request is dropped, and the process-wide window caps a sustained flood."""
    import src.routers.csp_report as csp_route

    monkeypatch.setattr(csp_route, "_window", {"start": 0.0, "logged": 0, "suppressed": 0})
    caplog.set_level(logging.INFO, logger=LOGGER)
    small = {"type": "csp-violation", "body": {"effectiveDirective": "img-src", "blockedURL": "x", "documentURL": "/"}}
    batch = [small] * 100
    assert len(json.dumps(batch)) < 16 * 1024
    r = await client_without_origin.post(
        "/api/csp-report",
        content=json.dumps(batch),
        headers={"Content-Type": "application/reports+json"},
    )
    assert r.status_code == 204
    assert len(_lines(caplog)) == 20
    for _ in range(4):  # 80 more admissible lines; the window admits 40 of them
        await client_without_origin.post(
            "/api/csp-report", content=json.dumps(batch),
            headers={"Content-Type": "application/reports+json"},
        )
    assert len(_lines(caplog)) == csp_route.MAX_LINES_PER_WINDOW
    later = csp_route._clock() + 3600.0
    monkeypatch.setattr(csp_route, "_clock", lambda: later)  # the next window
    await client_without_origin.post(
        "/api/csp-report", content=json.dumps([small]),
        headers={"Content-Type": "application/reports+json"},
    )
    assert any(rec.getMessage() == "csp_report suppressed=40 in the last window"
               for rec in caplog.records)


async def test_fields_cannot_forge_or_bloat_a_log_line(client_without_origin, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    hostile = {
        "csp-report": {
            "document-uri": "https://x/\nCRITICAL forged line",
            "effective-directive": "script-src",
            "blocked-uri": "https://x/" + "a" * 5000,
        }
    }
    r = await client_without_origin.post(
        "/api/csp-report", content=json.dumps(hostile), headers={"Content-Type": "application/csp-report"}
    )
    assert r.status_code == 204
    [rec] = [rec for rec in caplog.records if rec.name == LOGGER]
    assert "\n" not in rec.getMessage()
    [line] = _lines(caplog)
    assert line["document_uri"] == "https://x/\nCRITICAL forged line"
    assert len(line["blocked_uri"]) == 512
