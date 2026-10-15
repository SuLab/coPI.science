"""httpx logs every request URL at INFO; ``src.services.pubmed`` installs a filter on the
``httpx`` logger so the NCBI ``api_key`` query value never reaches a log line."""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
import respx

import src.services.pubmed  # noqa: F401  (installs the filter)
from src.services import pubmed
from src.services.corpus import CorpusStageError
from src.services.job_queue import job_error_text


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


KEY = "SECRETKEY0123456789"
EFETCH = f"{pubmed.EUTILS_BASE}/efetch.fcgi"


@pytest.fixture
def real_client(monkeypatch, caplog):
    """The client pubmed._make_client() really builds, with respx intercepting the
    transport; the key comes from a stub settings object, and pacing and backoff are
    zeroed, as tests/unit/test_pubmed_transport.py does."""
    monkeypatch.setattr(pubmed, "get_settings", lambda: SimpleNamespace(
        ncbi_api_key=KEY, ncbi_contact_email="ops@example.org",
        ses_sender_email="ops@example.org",
    ))

    async def no_pace():
        return None

    real_sleep = asyncio.sleep

    async def instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed, "_pace", no_pace)
    monkeypatch.setattr(pubmed.asyncio, "sleep", instant)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as router:
        yield router


def _assert_keyless(exc: BaseException) -> None:
    assert exc.__context__ is None and exc.__suppress_context__ is True
    seen: BaseException | None = exc
    while seen is not None:
        assert KEY not in str(seen) and KEY not in repr(seen)
        if isinstance(seen, httpx.HTTPError):
            try:
                request = seen.request
            except RuntimeError:
                request = None
            if request is not None:
                assert KEY not in str(request.url) and KEY not in repr(request)
        if isinstance(seen, httpx.HTTPStatusError):
            assert KEY not in str(seen.response.request.url)
        seen = seen.__cause__ or seen.__context__


def _assert_logs_keyless(caplog) -> None:
    formatter = logging.Formatter("%(name)s %(message)s")
    for record in caplog.records:
        assert KEY not in formatter.format(record)


async def test_a_real_request_line_logs_the_key_redacted(real_client, caplog):
    real_client.get(EFETCH).mock(return_value=httpx.Response(
        200, text='<?xml version="1.0"?><PubmedArticleSet></PubmedArticleSet>'))
    params = {"db": "pubmed", "id": "1"}
    await pubmed._ncbi_get(EFETCH, params)
    assert any(r.name == "httpx" and "api_key=REDACTED" in r.getMessage() for r in caplog.records)
    _assert_logs_keyless(caplog)
    assert params == {"db": "pubmed", "id": "1"}


async def test_a_404_raises_a_keyless_status_error(real_client, caplog):
    real_client.get(EFETCH).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await pubmed._ncbi_get(EFETCH, {"db": "pubmed", "id": "1"})
    _assert_keyless(caught.value)
    assert caught.value.response.status_code == 404
    assert pubmed._per_item_4xx(caught.value) == 404
    _assert_logs_keyless(caplog)


async def test_a_retried_timeout_raises_keyless(real_client, caplog):
    route = real_client.get(EFETCH).mock(side_effect=httpx.ReadTimeout)
    with pytest.raises(httpx.ReadTimeout) as caught:
        await pubmed._ncbi_get(EFETCH, {"db": "pubmed", "id": "1"})
    _assert_keyless(caught.value)
    assert route.call_count == 3
    retries = [r for r in caplog.records
               if r.name == "src.services.pubmed" and r.levelno == logging.INFO
               and "retrying" in r.getMessage()]
    assert len(retries) == 2
    _assert_logs_keyless(caplog)


async def test_a_non_retryable_transport_error_raises_keyless(real_client):
    route = real_client.get(EFETCH).mock(side_effect=httpx.LocalProtocolError)
    with pytest.raises(httpx.LocalProtocolError) as caught:
        await pubmed._ncbi_get(EFETCH, {"db": "pubmed", "id": "1"})
    _assert_keyless(caught.value)
    assert route.call_count == 1


async def test_the_job_error_text_of_a_stage_failure_is_keyless(real_client):
    real_client.get(EFETCH).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await pubmed._ncbi_get(EFETCH, {"db": "pubmed", "id": "1"})
    text = job_error_text(CorpusStageError(f"corpus stage s1 failed: {caught.value}"))
    assert "api_key=REDACTED" in text and KEY not in text
