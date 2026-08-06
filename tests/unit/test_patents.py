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
    result = await patents.search_prior_art("widget")
    assert result.hits[0]["patent_id"] == "US20260000001A1"
    assert result.hits[0]["title"] == "Novel Widget"
    assert result.hits[0]["applicant"] == "Acme Corp"
    assert result.hits[0]["inventor"] == "Jane Doe"
    assert result.hits[0]["date"] == "2026-01-01"


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
    result = await patents.search_prior_art("gene editing widget")
    assert "editing genes precisely" in result.hits[0]["abstract"]
    assert "gene-editing widget comprising a guide" in result.hits[0]["claim"]
    # only the FIRST claim is taken
    assert "claim 1" not in result.hits[0]["claim"].lower()


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
    result = await patents.search_prior_art("gene editing widget")
    assert len(result.hits) == 1
    assert result.hits[0]["title"] == "Gene Editing Widget"
    assert result.hits[0]["abstract"] == "" and result.hits[0]["claim"] == ""


@pytest.mark.asyncio
@respx.mock
async def test_pgpub_uri_never_leaks_past_the_fulltext_cap(monkeypatch):
    # _pgpub_uri is internal bookkeeping (which pre-grant XML to fetch for
    # enrichment) and must never reach a returned hit — including past
    # _FULLTEXT_MAX, where it is popped without ever being fetched. The last
    # hit's URI is deliberately left unmocked: if the cap ever regressed and
    # that hit got fetched too, respx would raise instead of silently passing.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    n = patents._FULLTEXT_MAX + 1
    bag = [
        {
            "applicationNumberText": f"18/{i:06d}",
            "pgpubDocumentMetaData": {"fileLocationURI": f"{_XML_URL}?i={i}"},
            "applicationMetaData": {
                "inventionTitle": "Novel Widget",
                "earliestPublicationNumber": f"US2026{i:07d}A1",
                "earliestPublicationDate": "2026-01-01",
                "firstApplicantName": "Acme Corp",
                "firstInventorName": "Jane Doe",
                "applicationStatusDescriptionText": "Patented Case",
            },
        }
        for i in range(n)
    ]
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"patentFileWrapperDataBag": bag})
    )
    for i in range(patents._FULLTEXT_MAX):
        respx.get(f"{_XML_URL}?i={i}").mock(return_value=httpx.Response(200, text=_PGPUB_XML))
    result = await patents.search_prior_art("widget")
    assert len(result.hits) == n
    assert all("_pgpub_uri" not in hit for hit in result.hits)


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
    assert (await patents.search_prior_art("nonexistent-xyzzy")).hits == []


@pytest.mark.asyncio
@respx.mock
async def test_404_no_matches_returns_empty_list(monkeypatch):
    # ODP answers 404 (not 200) when a valid search matches nothing. That is
    # "searched, found nothing" ([]), NOT "could not search" (None).
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(404, json={"code": "404", "message": "Not Found"})
    )
    assert (await patents.search_prior_art("nonexistent-xyzzy-concept")).hits == []


@pytest.mark.asyncio
@respx.mock
async def test_first_tier_ands_all_title_tokens(monkeypatch):
    # The first attempt must still AND every token (OR returns tens of thousands of
    # junk matches on common words). Backoff only widens AFTER that misses.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    captured = []

    def _capture(request):
        import json
        captured.append(json.loads(request.content)["q"])
        return httpx.Response(200, json={"patentFileWrapperDataBag": []})

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    await patents.search_prior_art("CRISPR base editing")
    assert captured[0] == "applicationMetaData.inventionTitle:(CRISPR AND base AND editing)"


@pytest.mark.asyncio
@respx.mock
async def test_backs_off_to_fewer_terms_when_full_phrase_misses(monkeypatch):
    # A 7-token free-text query ANDed on the title matches nothing. The search must
    # retry with the most specific terms rather than reporting a false clean.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    queries = []

    def _capture(request):
        import json
        q = json.loads(request.content)["q"]
        queries.append(q)
        # EXACT match, not substring: the full-phrase tier also contains both
        # tokens, so a substring test would match on the first attempt and the
        # backoff would never be exercised.
        if q == "applicationMetaData.inventionTitle:(TFEB AND BRAF)":
            return httpx.Response(200, json=_ODP_ONE_HIT)
        return httpx.Response(200, json={"patentFileWrapperDataBag": []})

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    result = await patents.search_prior_art(
        "TFEB inhibitor nuclear translocation melanoma BRAF resistance"
    )
    assert len(result.hits) == 1
    assert result.terms_used == ["TFEB", "BRAF"]
    assert result.total_terms == 7  # counts the raw query tokens, generics included
    assert result.broadened is True
    assert len(queries) == 3  # full phrase, top-3, top-2


@pytest.mark.asyncio
@respx.mock
async def test_no_backoff_needed_when_full_phrase_hits(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    route = respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_ONE_HIT)
    )
    result = await patents.search_prior_art("gene editing widget")
    assert result.broadened is False
    assert result.total_terms == 3
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_all_tiers_empty_reports_the_narrowest_breadth_tried(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"patentFileWrapperDataBag": []})
    )
    result = await patents.search_prior_art("alpha beta gamma delta epsilon")
    assert result.hits == []
    assert len(result.terms_used) == 2  # floored at two terms
    assert result.broadened is True


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_mid_backoff_returns_none_not_a_clean_result(monkeypatch):
    # A 429 on any tier must read as "could not search", never as novelty.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    calls = {"n": 0}

    def _capture(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"patentFileWrapperDataBag": []})
        return httpx.Response(429)

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    assert await patents.search_prior_art("alpha beta gamma delta") is None


@pytest.mark.asyncio
@respx.mock
async def test_lone_specific_term_among_generics_still_gets_a_narrower_tier(monkeypatch):
    # Regression: when only ONE token survives generics-filtering, the ranked pool
    # is narrower than both backoff widths (3 and _MIN_TERMS=2). Skipping the
    # narrower tier for that reason would reproduce the exact guaranteed-zero-hit
    # bug this task exists to fix, for the query that most needs the backoff — the
    # full 8-token AND is *always* a miss with 7 of the 8 tokens generic, and
    # "BRAF" alone is genuinely informative (10 real hits, live-verified).
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    queries = []

    def _capture(request):
        import json
        q = json.loads(request.content)["q"]
        queries.append(q)
        if q == "applicationMetaData.inventionTitle:(BRAF)":
            return httpx.Response(200, json=_ODP_ONE_HIT)
        return httpx.Response(200, json={"patentFileWrapperDataBag": []})

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    result = await patents.search_prior_art(
        "novel treatment method for BRAF disease using approach"
    )
    assert len(result.hits) == 1
    assert result.terms_used == ["BRAF"]
    assert result.broadened is True
    assert len(queries) > 1


def test_generic_terms_lose_to_specific_ones():
    ranked = patents._rank_terms(
        ["treatment", "C9orf72", "inhibitor", "MARK2", "disease"]
    )
    # Set comparison: which two survive is the invariant; their relative order is
    # an arbitrary tie-break not worth pinning.
    assert set(ranked[:2]) == {"C9orf72", "MARK2"}
    assert "treatment" not in ranked
    assert "inhibitor" not in ranked


def test_single_token_query_uses_one_tier():
    assert patents._tiers(["TFEB"]) == [["TFEB"]]


def test_first_tier_is_the_query_as_asked_in_original_order():
    # Only the backoff tiers reorder by salience. Tier 1 is the user's phrase.
    tiers = patents._tiers(["CRISPR", "base", "editing"])
    assert tiers[0] == ["CRISPR", "base", "editing"]


def test_all_generic_query_still_backs_off_normally():
    # _rank_terms falls back to the full token list when every token is generic
    # ("specific or tokens"), so an all-generic query must still get narrower
    # tiers — guarding the asymmetry where that fallback got backoff but a query
    # with exactly one specific term (see the lone-specific-term test above) did
    # not, before the _tiers fix.
    tiers = patents._tiers(["the", "of", "for", "with", "via"])
    assert len(tiers) == 3
    assert len(tiers[-1]) == 2


from src.agent.tools import TOOL_DEFINITIONS, _execute_search_prior_art  # noqa: E402
from src.services.patents import PriorArtResult  # noqa: E402

CAVEAT_MARK = "US filings only"


def test_search_prior_art_is_a_registered_tool():
    assert any(t["name"] == "search_prior_art" for t in TOOL_DEFINITIONS)


async def _fake(v):
    return v


@pytest.mark.asyncio
async def test_searched_empty_carries_caveat_and_says_no_matches(monkeypatch):
    from src.agent import tools as tools_mod
    monkeypatch.setattr(
        tools_mod, "search_prior_art",
        lambda q, limit=10: _fake(PriorArtResult([], ["crispr", "delivery"], 2)),
    )
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
    monkeypatch.setattr(
        tools_mod, "search_prior_art",
        lambda q, limit=10: _fake(PriorArtResult([hit], ["widget"], 1)),
    )
    out = await _execute_search_prior_art("widget")
    assert CAVEAT_MARK in out
    assert "US20260000001A1" in out
    assert "Novel Widget" in out
    assert "Acme Corp" in out
    assert "Jane Doe" in out
    assert "A widget that does things." in out
    assert "1. A widget comprising a thing." in out


@pytest.mark.asyncio
async def test_caveat_states_title_only_and_the_real_source(monkeypatch):
    from src.agent import tools as tools_mod
    monkeypatch.setattr(
        tools_mod, "search_prior_art",
        lambda q, limit=10: _fake(PriorArtResult([], ["widget"], 1)),
    )
    out = await _execute_search_prior_art("widget")
    assert "TITLE ONLY" in out
    assert "USPTO Open Data Portal" in out
    assert "PatentsView" not in out
    assert "freedom-to-operate" in out.lower()


@pytest.mark.asyncio
async def test_broadened_search_is_flagged_as_broader_than_asked(monkeypatch):
    from src.agent import tools as tools_mod
    result = PriorArtResult([], ["TFEB", "BRAF"], 7)
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake(result))
    out = await _execute_search_prior_art("TFEB inhibitor melanoma BRAF resistance x y")
    assert "BROADER" in out
    assert "2 most specific of your 7" in out


@pytest.mark.asyncio
async def test_unbroadened_search_reports_the_terms_plainly(monkeypatch):
    from src.agent import tools as tools_mod
    result = PriorArtResult([], ["gene", "editing"], 2)
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake(result))
    out = await _execute_search_prior_art("gene editing")
    assert "BROADER" not in out
    assert "gene AND editing" in out


def test_tool_description_demands_a_short_specific_query():
    spec = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_prior_art")
    text = spec["description"] + spec["input_schema"]["properties"]["query"]["description"]
    assert "2-4" in text
    assert "title" in text.lower()
