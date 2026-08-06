import httpx
import pytest
import respx

from src.services import patents

# A realistic USPTO ODP Patent File Wrapper search response (trimmed).
_ODP_ONE_HIT = {
    "count": 1,
    "patentFileWrapperDataBag": [
        {
            "applicationNumberText": "18/000000",
            "applicationMetaData": {
                "inventionTitle": "Novel Widget",
                "earliestPublicationNumber": "US20260000001A1",
                "earliestPublicationDate": "2026-01-01",
                "filingDate": "2024-01-01",
                "firstApplicantName": "Acme Corp",
                "firstInventorName": "Jane Doe",
                "applicationStatusDescriptionText": "Patented Case",
            },
        }
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_normalised_hits(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(200, json=_ODP_ONE_HIT))
    hits = await patents.search_prior_art("widget")
    assert hits[0]["patent_id"] == "US20260000001A1"
    assert hits[0]["title"] == "Novel Widget"
    assert hits[0]["applicant"] == "Acme Corp"
    assert hits[0]["inventor"] == "Jane Doe"
    assert hits[0]["date"] == "2026-01-01"


_XML_URL = "https://api.uspto.gov/files/APPXML/1.xml"
_ODP_HIT_WITH_PGPUB = {
    "count": 1,
    "patentFileWrapperDataBag": [
        {
            "applicationNumberText": "18/000001",
            "pgpubDocumentMetaData": {"fileLocationURI": _XML_URL},
            "applicationMetaData": {
                "inventionTitle": "Gene Editing Widget",
                "earliestPublicationNumber": "US20260000002A1",
                "earliestPublicationDate": "2026-02-02",
                "firstApplicantName": "Johns Hopkins University",
                "firstInventorName": "Jane Roe",
                "applicationStatusDescriptionText": "Docketed New Case",
            },
        }
    ],
}
_PGPUB_XML = (
    "<us-patent-application><abstract><p>A widget for editing genes precisely.</p>"
    "</abstract><claims><claim>1. A gene-editing widget comprising a guide.</claim>"
    "<claim>2. The widget of claim 1.</claim></claims></us-patent-application>"
)


@pytest.mark.asyncio
@respx.mock
async def test_enriches_abstract_and_first_claim_from_pgpub_xml(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_HIT_WITH_PGPUB)
    )
    respx.get(_XML_URL).mock(return_value=httpx.Response(200, text=_PGPUB_XML))
    hits = await patents.search_prior_art("gene editing widget")
    assert "editing genes precisely" in hits[0]["abstract"]
    assert "gene-editing widget comprising a guide" in hits[0]["claim"]
    # only the FIRST claim is taken
    assert "claim 1" not in hits[0]["claim"].lower()


@pytest.mark.asyncio
@respx.mock
async def test_fulltext_fetch_failure_leaves_hit_title_level(monkeypatch):
    # A hit with a pgpub URI whose XML fetch fails must still return the hit,
    # just without abstract/claim — enrichment is best-effort, never fatal.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_HIT_WITH_PGPUB)
    )
    respx.get(_XML_URL).mock(return_value=httpx.Response(500))
    hits = await patents.search_prior_art("gene editing widget")
    assert len(hits) == 1
    assert hits[0]["title"] == "Gene Editing Widget"
    assert hits[0]["abstract"] == "" and hits[0]["claim"] == ""


@pytest.mark.asyncio
@respx.mock
async def test_missing_key_returns_none_and_does_not_call(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "")
    # Route WOULD succeed if called — proves the no-key guard short-circuits before
    # any HTTP call. No key = cannot search = None (NOT []).
    route = respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"patentFileWrapperDataBag": []})
    )
    hits = await patents.search_prior_art("widget")
    assert hits is None
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_http_error_returns_none(monkeypatch):
    # 500 / unreachable / DNS failure = could-not-search = None (not []).
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(500))
    assert await patents.search_prior_art("widget") is None


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_returns_none(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    assert await patents.search_prior_art("x") is None


@pytest.mark.asyncio
@respx.mock
async def test_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(200, text="not json"))
    assert await patents.search_prior_art("x") is None


@pytest.mark.asyncio
@respx.mock
async def test_searched_but_empty_returns_empty_list(monkeypatch):
    # A real 200 with zero matches is [] (searched, found nothing) — NOT None.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"count": 0, "patentFileWrapperDataBag": []})
    )
    assert await patents.search_prior_art("nonexistent-xyzzy") == []


@pytest.mark.asyncio
@respx.mock
async def test_404_no_matches_returns_empty_list(monkeypatch):
    # ODP answers 404 (not 200) when a valid search matches nothing. That is
    # "searched, found nothing" ([]), NOT "could not search" (None).
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(404, json={"code": "404", "message": "Not Found"})
    )
    assert await patents.search_prior_art("nonexistent-xyzzy-concept") == []


@pytest.mark.asyncio
@respx.mock
async def test_query_ands_title_tokens(monkeypatch):
    # Multi-word queries must AND the title tokens (OR returns tens of thousands of
    # junk matches on common words). Assert the outgoing body ANDs the tokens.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    captured = {}

    def _capture(request):
        import json
        captured["q"] = json.loads(request.content)["q"]
        return httpx.Response(200, json={"patentFileWrapperDataBag": []})

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    await patents.search_prior_art("CRISPR base editing")
    assert captured["q"] == "applicationMetaData.inventionTitle:(CRISPR AND base AND editing)"


from src.agent.tools import TOOL_DEFINITIONS, _execute_search_prior_art  # noqa: E402

CAVEAT_MARK = "US filings only"


def test_search_prior_art_is_a_registered_tool():
    assert any(t["name"] == "search_prior_art" for t in TOOL_DEFINITIONS)


async def _fake(v):
    return v


@pytest.mark.asyncio
async def test_searched_empty_carries_caveat_and_says_no_matches(monkeypatch):
    from src.agent import tools as tools_mod
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake([]))
    out = await _execute_search_prior_art("crispr delivery")
    assert CAVEAT_MARK in out
    assert "no us filings matched" in out.lower()


@pytest.mark.asyncio
async def test_unavailable_when_search_could_not_run(monkeypatch):
    # search_prior_art returns None (no key / endpoint unreachable). The tool must
    # emit an explicit UNAVAILABLE notice, NOT "no US filings matched".
    from src.agent import tools as tools_mod
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake(None))
    out = await _execute_search_prior_art("crispr delivery")
    assert "unavailable" in out.lower()
    assert "novelty" in out.lower()
    assert "no us filings matched" not in out.lower()


@pytest.mark.asyncio
async def test_output_carries_caveat_and_fields_on_has_hits_path(monkeypatch):
    from src.agent import tools as tools_mod
    hit = {
        "patent_id": "US20260000001A1",
        "title": "Novel Widget",
        "date": "2026-01-01",
        "applicant": "Acme Corp",
        "inventor": "Jane Doe",
        "status": "Patented Case",
        "abstract": "A widget that does things.",
        "claim": "1. A widget comprising a thing.",
    }
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake([hit]))
    out = await _execute_search_prior_art("widget")
    assert CAVEAT_MARK in out
    assert "US20260000001A1" in out
    assert "Novel Widget" in out
    assert "Acme Corp" in out
    assert "Jane Doe" in out
    assert "A widget that does things." in out
    assert "1. A widget comprising a thing." in out
