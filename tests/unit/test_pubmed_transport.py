"""NCBI transport failures are transient, and are not evidence of nonexistence.

Run 8b64a0e0 (M12): a truncated response body surfaces as
``httpx.RemoteProtocolError``, which ``_ncbi_get``'s status-code retry cannot see
because there is no status code. ``fetch_pubmed_records`` swallowed it and
``fetch_abstract`` then told the model *"No PubMed record found for 41130592"* —
a paper that exists, that the hub had cited by DOI four seconds earlier, and that
the same run had successfully fetched 69 seconds before. ``src/agent/tools.py``
returns that ``error`` string to the model verbatim, so the wording *is* the
claim, and the second half of the fix (say "the lookup failed", not "there is no
such paper") matters more than the retry: fixing only the retry makes the bug
rarer and less diagnosable.

Timing is deliberately not tested here — ``tests/unit/test_pubmed_pacing.py``
owns that — so this module zeroes the pacer and the backoff sleeps and asserts
only on retry *logic* and on what the model is told.
"""

import asyncio

import httpx
import pytest

from src.services import pubmed

_ONE_RECORD_XML = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>41130592</PMID><Article>
<ArticleTitle>A Paper That Exists</ArticleTitle>
<Abstract><AbstractText>Findings.</AbstractText></Abstract>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>
"""

_EMPTY_SET_XML = '<?xml version="1.0"?><PubmedArticleSet></PubmedArticleSet>'


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._next_slot = 0.0
    real_sleep = asyncio.sleep

    async def _instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)


def _client_factory(handler):
    """A ``_make_client`` replacement whose transport runs ``handler``."""
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_truncated_body_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )
        return httpx.Response(200, text=_ONE_RECORD_XML)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("41130592")
    assert calls["n"] == 2, "a truncated body must be retried, not surfaced as a miss"
    assert out["pmid"] == "41130592"
    assert out["title"] == "A Paper That Exists"


async def test_a_persistent_transport_failure_does_not_claim_the_record_is_absent(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.RemoteProtocolError("peer closed connection")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("41130592")
    assert calls["n"] == 3, "the retry budget is three attempts, as for a 429/5xx"
    assert "error" in out
    # This exact string is handed to the model (src/agent/tools.py). It must not be
    # an affirmative claim about the world.
    assert "No PubMed record found" not in out["error"]
    assert "41130592" in out["error"]
    assert "failed" in out["error"].lower()


async def test_a_genuinely_absent_record_still_reads_as_absent(monkeypatch):
    # The other half of the distinction: a completed lookup that matched nothing
    # must keep saying so, or the fix would have traded one wrong answer for another.
    def handler(request):
        return httpx.Response(200, text=_EMPTY_SET_XML)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("99999999")
    assert out["error"] == "No PubMed record found for 99999999"


async def test_a_read_timeout_is_retried_too(monkeypatch):
    # RemoteProtocolError is the one M12 caught in the act, but a read timeout, a
    # connect error and a read error are the same kind of event: the request did
    # not complete, which says nothing at all about the record.
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, text=_ONE_RECORD_XML)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("41130592")
    assert calls["n"] == 3
    assert out["title"] == "A Paper That Exists"


async def test_a_404_is_still_not_retried(monkeypatch):
    # The retry set stays narrow on purpose: a 404 is an answer, and retrying it
    # would just triple the traffic behind every bad identifier.
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("41130592")
    assert calls["n"] == 1
    assert "error" in out


async def test_idconv_issues_no_redirect(monkeypatch):
    # M14: IDCONV_BASE omitted the trailing slash, so every one of run 8b64a0e0's
    # 194 idconv calls paid a 301 and was re-issued — 388 requests for 194 lookups,
    # 32% of all NCBI traffic — and _pace() counted one, so the real rate against
    # NCBI was 2x what the pacer believed. Live-verified 2026-08-22:
    # .../v1/articles -> 301 to .../v1/articles/, and .../v1/articles/ -> 200.
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if not request.url.path.endswith("/"):
            return httpx.Response(301, headers={"Location": request.url.path + "/"})
        return httpx.Response(200, json={"records": [{"pmid": "1", "pmcid": "PMC1"}]})

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.convert_pmids_to_pmcids(["1"])
    assert out == {"1": "PMC1"}
    assert paths == ["/tools/idconv/api/v1/articles/"]


async def test_an_unresolvable_doi_is_not_reported_as_a_nonexistent_paper(monkeypatch):
    # convert_dois_to_pmids swallows its own failures too, so "could not resolve"
    # covers both "PubMed has no record" and "the lookup broke". The message has to
    # admit that rather than pick the more damaging reading for the model.
    def handler(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("10.1038/nature12373")
    assert "error" in out
    assert "10.1038/nature12373" in out["error"]
    assert "not evidence" in out["error"].lower()


# ---------------------------------------------------------------------------
# A COMPLETED request whose body will not parse is still not evidence of
# nonexistence — the same distinction as the transport failures above, one
# layer in.
# ---------------------------------------------------------------------------


async def test_malformed_xml_does_not_claim_the_record_is_absent(monkeypatch):
    """`_parse_pubmed_xml` swallowed `ET.ParseError` and returned `[]`.

    So `_fetch_pubmed_batch` returned `[]` WITHOUT raising, `fetch_abstract`'s
    `_LOOKUP_FAILED` guard never fired, and the model was told "No PubMed record
    found for X" about a request that completed with a body we could not read.
    Malformed XML is our end of the conversation failing, not PubMed's answer.
    """
    def handler(request):
        return httpx.Response(200, text="<PubmedArticleSet><PubmedArticle>")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("41130592")
    assert "error" in out
    assert "No PubMed record found" not in out["error"]
    assert "41130592" in out["error"]
    assert "failed" in out["error"].lower()


async def test_the_batch_path_still_survives_one_bad_response(monkeypatch):
    """With the default ``strict=False``, `fetch_pubmed_records` keeps a long
    ingest going: an ingest caller (industry evidence, the repair scripts) loses
    the bad batch's 100 PMIDs and no more.

    Two chunks (the batch size is 100): the first comes back unparseable, the
    second is fine. The good records must still arrive and nothing may
    propagate. The profile corpus does NOT take this path — it passes
    ``strict=True`` (next tests), because a silently thinned corpus is a wrong
    profile.
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="<not-xml")
        return httpx.Response(200, text=_ONE_RECORD_XML)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    records = await pubmed.fetch_pubmed_records([str(i) for i in range(150)])
    assert calls["n"] == 2
    assert [r["pmid"] for r in records] == ["41130592"]


def _record_xml(pmid: str) -> str:
    return _ONE_RECORD_XML.replace("41130592", pmid)


async def test_strict_batch_path_raises_on_a_transient_failure(monkeypatch):
    """``strict=True`` is the corpus path: a batch that fails TRANSIENTLY (here
    a 503, retried to ``_ncbi_get``'s budget) must raise rather than return the
    surviving batch — the job retry is the recovery."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text=_ONE_RECORD_XML)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await pubmed.fetch_pubmed_records(
            [str(i) for i in range(150)], strict=True
        )
    assert calls["n"] == 3, "strict stops at the first failed batch"


async def test_strict_batch_path_falls_back_per_pmid_on_a_permanent_failure(
    monkeypatch, caplog
):
    """A PERMANENT batch failure (an unparseable body) would repeat on every
    job retry, so strict mode re-fetches that batch's PMIDs one at a time: the
    good records survive, and the one PMID that still fails permanently is
    dropped with a WARNING naming it."""
    singles = []

    def handler(request):
        ids = request.url.params["id"]
        if "," in ids:
            return httpx.Response(200, text="<not-xml")
        singles.append(ids)
        if ids == "22":
            return httpx.Response(200, text="<not-xml")
        return httpx.Response(200, text=_record_xml(ids))

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    records = await pubmed.fetch_pubmed_records(["11", "22", "33"], strict=True)
    assert singles == ["11", "22", "33"]
    assert [r["pmid"] for r in records] == ["11", "33"]
    assert "PMID 22 failed permanently" in caplog.text
    assert "PMID 11 " not in caplog.text and "PMID 33 " not in caplog.text


async def test_strict_per_pmid_fallback_still_raises_on_a_transient_failure(
    monkeypatch,
):
    def handler(request):
        ids = request.url.params["id"]
        if "," in ids:
            return httpx.Response(404, text="Not Found")
        if ids == "22":
            raise httpx.ConnectError("no route to host")
        return httpx.Response(200, text=_record_xml(ids))

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(httpx.ConnectError):
        await pubmed.fetch_pubmed_records(["11", "22", "33"], strict=True)


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (httpx.ConnectError("x"), True),
        (httpx.ReadTimeout("x"), True),
        (httpx.RemoteProtocolError("x"), True),
        (httpx.UnsupportedProtocol("x"), True),  # any TransportError
        (httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://e.x"),
            response=httpx.Response(429)), True),
        (httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://e.x"),
            response=httpx.Response(504)), True),
        (httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://e.x"),
            response=httpx.Response(400)), False),
        (httpx.HTTPStatusError(
            "x", request=httpx.Request("GET", "https://e.x"),
            response=httpx.Response(404)), False),
        (pubmed.PubMedParseError("x"), False),
        (ValueError("x"), False),
    ],
)
def test_is_transient_splits_retryable_from_per_item_failures(exc, transient):
    assert pubmed._is_transient(exc) is transient


def test_a_parse_failure_is_its_own_exception_not_an_empty_result():
    """Raising rather than returning `[]` is the whole fix, so it is pinned
    directly: `[]` and "unreadable" are different answers and only the caller
    knows which one it can act on."""
    with pytest.raises(pubmed.PubMedParseError):
        pubmed._parse_pubmed_xml("<not-xml")
    assert issubclass(pubmed.PubMedParseError, ValueError)
    assert pubmed._parse_pubmed_xml(_EMPTY_SET_XML) == []


# ---------------------------------------------------------------------------
# _RETRYABLE_TRANSPORT: base classes, not a list of leaf names
# ---------------------------------------------------------------------------


def test_every_timeout_class_is_retried():
    """The tuple's own comment claims "every member here means 'the request did
    not complete'". Enumerating leaves cannot keep that promise: it named
    ReadTimeout but not ConnectTimeout, WriteTimeout or PoolTimeout, and
    ReadError but not WriteError — so `_ncbi_get` surfaced four transport
    failures as hard errors that are as transient as the one it retried, and
    `fetch_abstract` turned them into an affirmative claim about the world.

    This walks the hierarchy rather than listing names, so a class httpx adds
    later is covered on the day it appears.
    """
    for base in (httpx.TimeoutException, httpx.NetworkError):
        assert issubclass(base, pubmed._RETRYABLE_TRANSPORT), base
        subclasses = base.__subclasses__()
        assert subclasses, f"{base.__name__} should have leaves to check"
        for cls in subclasses:
            assert issubclass(cls, pubmed._RETRYABLE_TRANSPORT), cls
    # The httpx names in 0.28.1 that this must catch, spelled out so a
    # rename shows up as a failure here rather than as silence.
    for cls in (
        httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
        httpx.PoolTimeout, httpx.ConnectError, httpx.ReadError,
        httpx.WriteError, httpx.CloseError, httpx.RemoteProtocolError,
    ):
        assert issubclass(cls, pubmed._RETRYABLE_TRANSPORT), cls


def test_a_malformed_request_of_ours_is_still_not_retried():
    """The other half: retrying a request that was never well-formed just
    triples the traffic behind a bug of ours. `LocalProtocolError` is the
    pointed one — its sibling `RemoteProtocolError` IS retried, and a
    `ProtocolError` base would have swept both in."""
    for cls in (
        httpx.ProxyError, httpx.UnsupportedProtocol, httpx.LocalProtocolError,
    ):
        assert not issubclass(cls, pubmed._RETRYABLE_TRANSPORT), cls


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("no connection available"),
        httpx.WriteError("broken pipe"),
    ],
)
async def test_the_newly_covered_transport_failures_are_actually_retried(
    monkeypatch, exc
):
    """The property above, driven through `_ncbi_get` — a class that is a
    subclass but that the retry loop somehow did not catch would pass the
    hierarchy walk and fail here."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise exc
        return httpx.Response(200, text=_ONE_RECORD_XML)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.fetch_abstract("41130592")
    assert calls["n"] == 3
    assert out["title"] == "A Paper That Exists"
