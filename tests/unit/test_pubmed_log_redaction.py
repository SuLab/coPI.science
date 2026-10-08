"""httpx logs every request URL at INFO; ``src.services.pubmed`` installs a filter on the
``httpx`` logger so the NCBI ``api_key`` query value never reaches a log line."""

import logging

import httpx

import src.services.pubmed  # noqa: F401  (installs the filter)


def _format(url):
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", 200, "OK"), None,
    )
    for f in logging.getLogger("httpx").filters:
        f.filter(record)
    return record.getMessage()


def test_the_api_key_is_redacted_from_an_httpx_request_line():
    url = httpx.URL("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&api_key=SECRET123&tool=x")
    line = _format(url)
    assert "SECRET123" not in line
    assert "api_key=REDACTED&tool=x" in line
    assert line.endswith('"HTTP/1.1 200 OK"')


def test_a_line_without_a_key_is_unchanged():
    url = httpx.URL("https://api.openalex.org/works?filter=pmid:1")
    assert _format(url) == 'HTTP Request: GET https://api.openalex.org/works?filter=pmid:1 "HTTP/1.1 200 OK"'


def test_an_ncbi_http_error_message_carries_no_api_key(monkeypatch):
    import asyncio

    import pytest

    from src.services import pubmed

    async def no_pace():
        return None

    class _Client:
        async def get(self, url, params=None):
            request = httpx.Request("GET", url, params=params)
            return httpx.Response(404, request=request)

    monkeypatch.setattr(pubmed, "_pace", no_pace)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        asyncio.run(pubmed._ncbi_attempts(
            _Client(), "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            {"db": "pubmed", "api_key": "SECRET123"},
        ))
    assert "SECRET123" not in str(caught.value)
    assert "api_key=REDACTED" in str(caught.value)
    assert caught.value.response.status_code == 404
    assert pubmed._per_item_4xx(caught.value) == 404


def test_the_pubmed_logger_redacts_an_exception_argument():
    exc = RuntimeError("Client error for url 'https://x/efetch.fcgi?api_key=SECRET123&db=pubmed'")
    record = logging.LogRecord("src.services.pubmed", logging.WARNING, __file__, 1,
                               "EFetch failed: %s", (exc,), None)
    for f in logging.getLogger("src.services.pubmed").filters:
        f.filter(record)
    assert "SECRET123" not in record.getMessage()
