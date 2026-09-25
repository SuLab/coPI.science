"""A failed NCBI request inside ``resolve_corpus`` fails the corpus.

``fetch_pubmed_records`` and ``convert_dois_to_pmids`` swallow their own
failures by default, which is right for a long ingest and wrong for a profile
corpus: ``resolve_corpus``'s ``_stage`` wrapper can only raise
``CorpusStageError`` for an exception it SEES, so a swallowed EFetch batch or
DOI lookup used to thin the corpus — and the tenure start and synthesis built
on it — silently. The corpus path now passes ``strict=True``.

Strict re-raises everything except a PERMANENT per-item failure (a 4xx other
than 429, an unreadable body): transient failures (transport, 429, 5xx), which
a job retry can recover, and anything else, which is most likely a bug of
ours. A per-item failure would repeat on every retry, so it costs that item
alone — re-raising it would leave the job, and every later regeneration of the
PI, ``dead`` — but the item is reported in ``CorpusResult.permanently_dropped``
so no tenure start is persisted from the incomplete corpus. The same 4xx on
``_SYSTEMIC_4XX_RUN`` requests in a row is NCBI refusing us, not bad items,
and raises.

``fetch_orcid_works`` swallows its failures by default too; the corpus passes
it ``strict=True`` as well (its own contract tests pin that mode).

The two pubmed functions run for real here, over an ``httpx.MockTransport``;
only the non-NCBI stages (ORCID works, OpenAlex, the PubMed searches) are
faked. A fake of the pubmed functions themselves could not show that the real
ones raise.
"""

import ast
import asyncio
import inspect

import httpx
import pytest

from src.services import corpus, pubmed
from src.services.corpus import CorpusStageError, resolve_corpus

_ORCID = "0000-0001-2345-6789"
_DOI = "10.1234/abc.def"


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


def _wire(monkeypatch, *, orcid_works):
    async def fake_orcid_works(orcid, *, strict=False):
        assert strict is True
        return list(orcid_works)

    async def fake_openalex(orcid):
        return []

    async def fake_search(term, retmax=200):
        return []

    monkeypatch.setattr(corpus, "fetch_orcid_works", fake_orcid_works)
    monkeypatch.setattr(corpus, "fetch_works_by_orcid", fake_openalex)
    monkeypatch.setattr(corpus, "search_pmids", fake_search)


def _route(request) -> str:
    path = request.url.path
    if "idconv" in path:
        return "idconv"
    if path.endswith("esearch.fcgi"):
        return "esearch"
    if path.endswith("efetch.fcgi"):
        return "efetch"
    raise AssertionError(f"unexpected NCBI request: {request.url}")


_EMPTY_IDCONV = {"records": []}
_EMPTY_ESEARCH = {"esearchresult": {"idlist": []}}


async def _resolve():
    return await resolve_corpus(_ORCID, "Rachel Green", "Johns Hopkins University")


_EMPTY_SET = '<?xml version="1.0"?><PubmedArticleSet></PubmedArticleSet>'


async def test_a_transiently_failed_efetch_batch_raises_corpus_stage_error(monkeypatch):
    _wire(monkeypatch, orcid_works=[{"pmid": str(i)} for i in range(1, 151)])
    calls = {"efetch": 0}

    def handler(request):
        assert _route(request) == "efetch"
        calls["efetch"] += 1
        if "," in request.url.params["id"] and calls["efetch"] <= 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text=_EMPTY_SET)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(CorpusStageError, match="efetch") as ei:
        await _resolve()
    assert calls["efetch"] == 3, "the 503 is retried to the budget, then raises"
    assert isinstance(ei.value.__cause__, httpx.HTTPStatusError)


async def test_a_permanently_failed_efetch_batch_does_not_fail_the_corpus(
    monkeypatch, caplog
):
    # An unparseable batch body would come back unparseable on every job
    # retry, so it must not raise: the batch is re-fetched PMID by PMID and
    # only the PMID that still fails is lost.
    _wire(monkeypatch, orcid_works=[{"pmid": str(i)} for i in range(1, 4)])
    singles = []

    def handler(request):
        assert _route(request) == "efetch"
        ids = request.url.params["id"]
        if "," in ids:
            return httpx.Response(200, text="<not-xml")
        singles.append(ids)
        if ids == "2":
            return httpx.Response(400, text="Bad Request")
        return httpx.Response(200, text=_EMPTY_SET)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    result = await _resolve()
    assert sorted(singles) == ["1", "2", "3"]
    assert "PMID 2 failed permanently" in caplog.text
    # Returned normally, but the corpus is known incomplete.
    assert result.permanently_dropped == ["2"]


async def test_every_efetch_refused_with_the_same_4xx_fails_the_corpus(monkeypatch):
    # A 403 on every request is NCBI refusing US (a revoked key, a blocked
    # tool id), not 150 bad PMIDs: dropping them all would let the job succeed
    # on an empty corpus.
    _wire(monkeypatch, orcid_works=[{"pmid": str(i)} for i in range(1, 151)])
    singles = []

    def handler(request):
        assert _route(request) == "efetch"
        if "," not in request.url.params["id"]:
            singles.append(request.url.params["id"])
        return httpx.Response(403, text="Forbidden")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(CorpusStageError, match="efetch") as ei:
        await _resolve()
    assert isinstance(ei.value.__cause__, httpx.HTTPStatusError)
    assert ei.value.__cause__.response.status_code == 403
    assert singles == ["1", "2", "3"], "stops at the third identical single 4xx"


async def test_a_bug_in_the_parser_fails_the_corpus_not_one_pmid(monkeypatch):
    # An AttributeError is a bug of ours, not evidence about a record: strict
    # must surface it rather than drop every PMID it touches.
    _wire(monkeypatch, orcid_works=[{"pmid": "1"}, {"pmid": "2"}])

    def handler(request):
        return httpx.Response(200, text=_EMPTY_SET)

    def broken_parser(xml_text):
        raise AttributeError("'NoneType' object has no attribute 'text'")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    monkeypatch.setattr(pubmed, "_parse_pubmed_xml", broken_parser)
    with pytest.raises(CorpusStageError, match="efetch") as ei:
        await _resolve()
    assert isinstance(ei.value.__cause__, AttributeError)


async def test_three_consecutive_identical_doi_4xx_fail_the_corpus(monkeypatch):
    dois = [f"10.1234/doi.{i}" for i in range(5)]
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": d} for d in dois])
    calls = {"esearch": 0}

    def handler(request):
        route = _route(request)
        if route == "idconv":
            return httpx.Response(200, json=_EMPTY_IDCONV)
        assert route == "esearch"
        calls["esearch"] += 1
        return httpx.Response(403, text="Forbidden")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(CorpusStageError, match="doi_resolution") as ei:
        await _resolve()
    assert isinstance(ei.value.__cause__, httpx.HTTPStatusError)
    assert calls["esearch"] == 3


async def test_an_idconv_failure_raises_corpus_stage_error(monkeypatch):
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": _DOI}])

    def handler(request):
        assert _route(request) == "idconv"
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(CorpusStageError, match="doi_resolution") as ei:
        await _resolve()
    assert isinstance(ei.value.__cause__, httpx.ConnectError)


async def test_a_failed_doi_esearch_raises_corpus_stage_error(monkeypatch):
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": _DOI}])
    calls = {"esearch": 0}

    def handler(request):
        route = _route(request)
        if route == "idconv":
            return httpx.Response(200, json=_EMPTY_IDCONV)
        assert route == "esearch"
        calls["esearch"] += 1
        return httpx.Response(503, text="Service Unavailable")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(CorpusStageError, match="doi_resolution") as ei:
        await _resolve()
    assert calls["esearch"] == 3, "the 503 is retried to the budget before failing"
    assert isinstance(ei.value.__cause__, httpx.HTTPStatusError)


async def test_a_failed_doi_roundtrip_efetch_raises(monkeypatch):
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": _DOI}])
    calls = {"efetch": 0}

    def handler(request):
        route = _route(request)
        if route == "idconv":
            return httpx.Response(200, json=_EMPTY_IDCONV)
        if route == "esearch":
            return httpx.Response(200, json={"esearchresult": {"idlist": ["31000000"]}})
        calls["efetch"] += 1
        raise httpx.RemoteProtocolError("peer closed connection")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(CorpusStageError, match="doi_resolution") as ei:
        await _resolve()
    assert calls["efetch"] == 3
    assert isinstance(ei.value.__cause__, httpx.RemoteProtocolError)


async def test_a_permanently_failed_doi_esearch_is_no_match_not_a_failure(
    monkeypatch, caplog
):
    # A 400 is not retried by _ncbi_get and would repeat on every job retry:
    # it reads as "no PMID for this DOI", with a WARNING naming the DOI.
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": _DOI}])
    calls = {"esearch": 0}

    def handler(request):
        route = _route(request)
        if route == "idconv":
            return httpx.Response(200, json=_EMPTY_IDCONV)
        assert route == "esearch"
        calls["esearch"] += 1
        return httpx.Response(400, text="Bad Request")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    result = await _resolve()
    assert result.kept == []
    assert calls["esearch"] == 1
    assert f"ESearch DOI lookup for {_DOI} failed permanently" in caplog.text
    assert result.permanently_dropped == [_DOI]


async def test_a_permanently_failed_idconv_batch_falls_through_to_esearch(
    monkeypatch, caplog
):
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": _DOI}])
    seen = []

    def handler(request):
        route = _route(request)
        seen.append(route)
        if route == "idconv":
            return httpx.Response(400, text="Bad Request")
        assert route == "esearch"
        return httpx.Response(200, json=_EMPTY_ESEARCH)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    result = await _resolve()
    assert result.kept == []
    assert seen == ["idconv", "esearch"]
    assert "ID converter batch failed permanently" in caplog.text
    assert _DOI in caplog.text


async def test_a_doi_answered_as_absent_is_not_a_failure(monkeypatch):
    # Positive control: every request COMPLETES and NCBI simply maps nothing —
    # an idconv error record, then an ESearch with no hit. That is an answer,
    # so strictness must not turn it into a failed job.
    _wire(monkeypatch, orcid_works=[{"pmid": None, "doi": _DOI}])
    seen = []

    def handler(request):
        route = _route(request)
        seen.append(route)
        if route == "idconv":
            return httpx.Response(
                200, json={"records": [{"doi": _DOI, "status": "error"}]}
            )
        assert route == "esearch"
        return httpx.Response(200, json=_EMPTY_ESEARCH)

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    result = await _resolve()
    assert result.kept == []
    assert seen == ["idconv", "esearch"], "both lookups must actually have run"
    assert result.permanently_dropped == [], "an answer is not a drop"


_STRICT_REQUIRED = {
    "convert_dois_to_pmids",
    "fetch_orcid_works",
    "fetch_pubmed_records",
}


def _callee(node: ast.Call) -> str | None:
    return node.func.id if isinstance(node.func, ast.Name) else None


def _passes_strict_true(node: ast.Call) -> bool:
    return any(
        kw.arg == "strict"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in node.keywords
    )


def test_every_corpus_stage_call_is_strict():
    """A new ``_stage`` over any swallowing function without ``strict=True``
    would reintroduce the silent thinning, and no fake-driven test would see
    it: the fakes answer whatever they are asked."""
    tree = ast.parse(inspect.getsource(resolve_corpus))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    wrapped = [
        inner
        for outer in calls
        if _callee(outer) == "_stage" and len(outer.args) >= 2
        for inner in [outer.args[1]]
        if isinstance(inner, ast.Call) and _callee(inner) in _STRICT_REQUIRED
    ]
    assert {_callee(c) for c in wrapped} == _STRICT_REQUIRED, (
        "resolve_corpus no longer stages all three swallowing lookups — this "
        "guard has gone vacuous; re-point it"
    )
    assert len(wrapped) == len(_STRICT_REQUIRED), (
        "each swallowing lookup is expected at exactly one _stage call site"
    )
    for call in wrapped:
        assert _passes_strict_true(call), (
            f"_stage({_callee(call)}(...)) is missing strict=True"
        )
    # And neither function is called OUTSIDE a _stage, where a raise would
    # escape as something other than CorpusStageError.
    bare = [c for c in calls if _callee(c) in _STRICT_REQUIRED]
    assert len(bare) == len(wrapped)
