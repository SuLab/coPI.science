import json
import time

import httpx
import pytest
import respx

from src.services import patents


@pytest.fixture(autouse=True)
def _isolated_and_unpaced(monkeypatch):
    """Each case gets its own empty memo and no real waiting.

    The memo is process-wide and keyed on the query's terms, and several cases here
    deliberately search "widget" against different mocks — so without the clear,
    one case's cached hits become the next one's answer and the HTTP mock never
    runs. Pacing and 429 backoff are real seconds; the two cases that assert on
    them raise the interval back up themselves.
    """
    patents.clear_prior_art_cache()
    monkeypatch.setattr(patents, "_PACE_INTERVAL", 0.0)
    monkeypatch.setattr(patents, "_ODP_BACKOFF", 0.0)
    patents._next_slot = 0.0


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


# A page of a much larger match set: ODP reports `count` at the top of every 200,
# and `sort: filingDate desc` + `limit: 10` means these ten are merely the ten
# most-recently-filed. 2,070 is the live count for a title search on "COVID".
_ODP_TRUNCATED = {
    "count": 2070,
    "patentFileWrapperDataBag": [
        {
            "applicationNumberText": f"18/{i:06d}",
            "applicationMetaData": {
                "inventionTitle": "Coronavirus Widget",
                "earliestPublicationNumber": f"US2026{i:07d}A1",
                "earliestPublicationDate": "2026-01-01",
                "filingDate": "2025-01-01",
                "firstApplicantName": "Acme Corp",
                "firstInventorName": "Jane Doe",
                "applicationStatusDescriptionText": "Docketed New Case - Ready for Examination",
            },
        }
        for i in range(10)
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_truncation_is_disclosed_when_odp_returns_a_page_of_a_huge_match_set(monkeypatch):
    # H2: the code requested 10 hits sorted by filing date and threw ODP's own
    # `count` away, so the hub could not tell "10 of 10" from "10 of 2,070".
    # 49 of 109 broadened searches in run 8b64a0e0 returned exactly 10. The
    # existing caveats disclaim SCOPE (a broadened query) and cannot disclaim
    # COMPLETENESS, because nothing ever told the model there was any.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(200, json=_ODP_TRUNCATED))
    result = await patents.search_prior_art("COVID")
    assert result.total_count == 2070
    assert result.truncated is True
    assert "10" in result.truncation_note and "2070" in result.truncation_note


@pytest.mark.asyncio
@respx.mock
async def test_a_complete_small_result_set_is_not_called_truncated(monkeypatch):
    # The mirror image: count == len(hits) means the hub is looking at everything
    # ODP matched, and claiming otherwise would be its own false disclosure.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(200, json=_ODP_ONE_HIT))
    result = await patents.search_prior_art("widget")
    assert result.total_count == 1
    assert result.truncated is False
    assert result.truncation_note == ""


@pytest.mark.asyncio
@respx.mock
async def test_a_response_without_a_count_reports_an_unknown_total(monkeypatch):
    # `count` is not a guaranteed field. Absent it, "how many matched" is unknown,
    # which must read as unknown rather than as "everything you asked for".
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"patentFileWrapperDataBag": _ODP_ONE_HIT["patentFileWrapperDataBag"]}
        )
    )
    result = await patents.search_prior_art("widget")
    assert result.total_count is None
    assert result.truncated is False
    assert result.truncation_note == ""


# An expired provisional: no publication number and no publication date, because a
# provisional application is never published. 17.4% of the 624 hit rows shown to
# the hub in run 8b64a0e0 looked like this.
_ODP_PROVISIONAL = {
    "count": 1,
    "patentFileWrapperDataBag": [
        {
            "applicationNumberText": "63/000000",
            "applicationMetaData": {
                "inventionTitle": "Widget Concept",
                "filingDate": "2024-05-01",
                "firstApplicantName": "Acme Corp",
                "firstInventorName": "Jane Doe",
                "applicationStatusDescriptionText": "Provisional Application Expired",
            },
        }
    ],
}


@pytest.mark.asyncio
@respx.mock
async def test_an_expired_provisional_is_labelled_not_prior_art(monkeypatch):
    # Of the 624 hit rows the hub read in run 8b64a0e0, only 12% were granted,
    # 17.4% were expired provisionals — never published, and therefore not prior
    # art at all — and 30% were unexamined 2026 filings, all rendered identically.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_PROVISIONAL)
    )
    hit = (await patents.search_prior_art("widget concept")).hits[0]
    assert "NOT prior art" in hit["prior_art_status"]
    # `status` is the only one of the two fields src/agent/tools.py renders, so the
    # label has to reach the model through it — without discarding ODP's own text.
    assert "NOT prior art" in hit["status"]
    assert "Provisional Application Expired" in hit["status"]


@pytest.mark.asyncio
@respx.mock
async def test_a_published_pending_application_is_still_labelled_prior_art(monkeypatch):
    # Deliberately NOT filtered out: a published pending application is prior art
    # under 35 USC 102(a)(2) even though it has never been examined.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(200, json=_ODP_TRUNCATED))
    hit = (await patents.search_prior_art("COVID")).hits[0]
    assert "102(a)(2)" in hit["prior_art_status"]


def test_the_prior_art_classes_are_distinguishable():
    assert "granted" in patents._prior_art_class("Patented Case", published=True)
    assert "abandoned" in patents._prior_art_class(
        "Abandoned -- Failure to Respond to an Office Action", published=True
    )
    # Filed but not yet published: not yet prior art, and not a provisional either.
    assert "not yet" in patents._prior_art_class(
        "Docketed New Case - Ready for Examination", published=False
    )
    # No status text at all: the publication itself is still enough to say this much.
    assert "102(a)(2)" in patents._prior_art_class("", published=True)


@pytest.mark.asyncio
@respx.mock
async def test_first_tier_ands_all_title_tokens(monkeypatch):
    # The first attempt must still AND every token (OR returns tens of thousands of
    # junk matches on common words). Backoff only widens AFTER that misses.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    captured = []

    def _capture(request):
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
    assert len(result.terms_used) == 1  # floored at one term (F2b)
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
async def test_a_429_is_retried_before_the_search_is_abandoned(monkeypatch):
    # M9: a 429 aborted the whole search with no retry, asymmetric with
    # pubmed._ncbi_get which retries 3x with backoff. 10 of 125 searches were lost
    # this way in run 8b64a0e0 and only 1 of the 10 was disclosed to the PI; one of
    # the 429s succeeded 41 ms later on the same key.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    monkeypatch.setattr(patents, "_ODP_BACKOFF", 0.0)
    calls = {"n": 0}

    def _capture(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=_ODP_ONE_HIT)

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    result = await patents.search_prior_art("widget")
    assert result is not None and len(result.hits) == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_an_identical_query_issues_no_second_http_request(monkeypatch):
    # 109 searches in run 8b64a0e0 resolved to only 91 distinct term-sets, so 18
    # of them (16.5%) re-ran the whole ladder AND re-paid the full-text enrichment
    # behind it — enrichment alone was 57% of the run's USPTO request budget, so
    # the redundant searches were ~24% of it.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    route = respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_ONE_HIT)
    )
    first = await patents.search_prior_art("widget")
    second = await patents.search_prior_art("widget")
    assert route.call_count == 1
    assert second.hits == first.hits
    # A cached result is handed to every later caller in the run, and `hits` is a
    # list of MUTABLE dicts — so it must be a copy, not the same object.
    assert second.hits[0] is not first.hits[0]


@pytest.mark.asyncio
@respx.mock
async def test_a_reordered_query_hits_the_same_cache_entry(monkeypatch):
    # ODP's AND is commutative (live-verified), so a permutation is the same search
    # and must not be paid for twice. Only the term ORDER shown back to the caller
    # differs, and that is the order the search actually ran in.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    route = respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_ODP_ONE_HIT)
    )
    await patents.search_prior_art("deoxyhypusine synthase")
    await patents.search_prior_art("synthase deoxyhypusine")
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_search_that_could_not_run_is_never_cached(monkeypatch):
    # Caching a None would poison the rest of the run: "could not search" is not a
    # result, and a rate limit is by definition transient.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    monkeypatch.setattr(patents, "_ODP_BACKOFF", 0.0)
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(429))
    assert await patents.search_prior_art("widget") is None
    respx.post(patents.SEARCH_URL).mock(return_value=httpx.Response(200, json=_ODP_ONE_HIT))
    again = await patents.search_prior_art("widget")
    assert again is not None and len(again.hits) == 1


@pytest.mark.asyncio
@respx.mock
async def test_every_odp_request_in_the_ladder_is_paced(monkeypatch):
    # Every one of the 10 429s in run 8b64a0e0 landed on the 3rd POST of its own
    # tier ladder, issued back-to-back with no spacing at all — the burst was ours,
    # which is why pacing matters more here than retrying (mirroring
    # pubmed._ncbi_get, where `await _pace()` before every attempt is the
    # load-bearing half).
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    monkeypatch.setattr(patents, "_PACE_INTERVAL", 0.05)
    patents._next_slot = 0.0
    respx.post(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"count": 0, "patentFileWrapperDataBag": []})
    )
    t0 = time.monotonic()
    await patents.search_prior_art("alpha beta gamma delta epsilon")  # four tiers
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05 * 3, f"four POSTs finished in {elapsed:.3f}s — the ladder is not paced"


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


def test_a_hyphenated_symbol_survives_as_one_token():
    # H3, root cause half 1: the sanitiser split on every hyphen, so the numeric
    # suffix of every SYMBOL-N name became a token of its own — and (see the next
    # test) outranked the symbol. The narrowest backoff tier of this query used to
    # be ['1']: a USPTO title search for the numeral one.
    tokens = patents._tokenise("LOX-1 myeloid-derived suppressor cells")
    assert tokens[0] == "LOX-1"
    assert patents._tiers(tokens)[-1] == ["LOX-1"]
    # A hyphen before a LETTER stays a token boundary: only SYMBOL-N is protected,
    # so every non-hyphenated query is tokenised exactly as it was before.
    assert "myeloid-derived" not in tokens
    assert patents._tokenise("CRISPR base editing") == ["CRISPR", "base", "editing"]


def test_a_leading_hyphen_can_never_reach_uspto():
    # "-" is the simplified query syntax's NOT operator when it leads a term. A
    # token can only START on an alphanumeric, so no amount of caller punctuation
    # can turn a clause into its negation.
    assert patents._tokenise("-BRAF --1 kinase") == ["BRAF", "1", "kinase"]


def test_an_all_digit_token_scores_below_a_gene_symbol():
    # H3, root cause half 2: _salience awarded +3 for containing a digit AND +2 for
    # `not token.islower()` — and a pure-digit token satisfies BOTH, since
    # "1".islower() is False. That is 5 before the length bonus, against GDF=3 and
    # LOX=3, so the numeral was promoted to rank 1 of every SYMBOL-N query.
    assert patents._salience("441524") < patents._salience("LOX")
    assert patents._salience("1") < patents._salience("GDF")
    assert patents._rank_terms(["GDF", "15"])[0] == "GDF"
    # The digit bonus itself must survive: it is what promotes the symbols the
    # _salience docstring names.
    assert patents._salience("C9orf72") > patents._salience("kinase")
    assert patents._salience("HER3") > patents._salience("kinase")


@pytest.mark.asyncio
@respx.mock
async def test_a_hyphenated_symbol_is_sent_as_a_quoted_phrase(monkeypatch):
    # Live-verified 2026-08-22 against api.uspto.gov: an UNQUOTED hyphen inside a
    # term is read as OR, not as part of the word.
    #   inventionTitle:(LOX-1)   -> count 34,680  (= LOX 111 + "1" 34,595 - 26 both)
    #   inventionTitle:("LOX-1") -> count 26      (= LOX AND 1, exactly)
    # So keeping the hyphen without quoting it would be strictly worse than the
    # bug it fixes: the most specific token in the query would become the least
    # specific clause in it, and sort-by-filing-date + limit 10 would hand the hub
    # ten unrelated recent filings as prior art.
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    captured = []

    def _capture(request):
        captured.append(json.loads(request.content)["q"])
        return httpx.Response(200, json=_ODP_ONE_HIT)

    respx.post(patents.SEARCH_URL).mock(side_effect=_capture)
    await patents.search_prior_art("LOX-1 antibody")
    assert captured[0] == 'applicationMetaData.inventionTitle:("LOX-1" AND antibody)'


def test_a_permutation_tier_is_not_issued_as_a_second_request():
    # M13: _tiers deduped candidates with a LIST comparison, but ODP's AND is
    # commutative — live-verified, `deoxyhypusine AND synthase` and
    # `synthase AND deoxyhypusine` both return 27. Salience ranking reorders a
    # 2-token query into the same SET as tier 1, which the list comparison read as
    # a new tier: 17 provably-wasted POSTs in run 8b64a0e0, each of which also
    # brought the ladder one step closer to the 429 it eventually took.
    assert patents._tiers(["synthase", "deoxyhypusine"]) == [
        ["synthase", "deoxyhypusine"],
        ["deoxyhypusine"],
    ]


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
    # not, before the _tiers fix. Widths now end at one term (F2b), so a
    # 5-token all-generic query yields four tiers: full, top-3, top-2, top-1.
    tiers = patents._tiers(["the", "of", "for", "with", "via"])
    assert len(tiers) == 4
    assert len(tiers[-1]) == 1


def test_two_token_query_now_backs_off_instead_of_being_inert():
    # Regression (F2): the pre-fix guard (`width >= len(tokens): continue`)
    # skipped BOTH backoff tiers for a 2-token query, so the widely-recommended
    # "2-4 specific terms" query never backed off at all. `_rank_terms` had
    # already isolated "TFEB" as the only non-generic term, but the old code
    # never tried it alone.
    assert patents._tiers(["TFEB", "inhibitor"]) == [["TFEB", "inhibitor"], ["TFEB"]]


def test_two_specific_term_query_still_backs_off_to_the_single_term_floor():
    # Both tokens survive generics-filtering, so width 3 and width _MIN_TERMS
    # (2) collapse into tier 1 (the exact query) with nothing left to add —
    # but the final width-1 floor (F2b, human-approved) still applies
    # unconditionally, since even a fully-specific AND'd pair can zero out
    # where either term alone returns real hits (TFEB: 10, BRAF: 10, measured
    # live). No redundant *duplicate* tiers are produced either way.
    assert patents._tiers(["TFEB", "BRAF"]) == [["TFEB", "BRAF"], ["TFEB"]]


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


@pytest.mark.asyncio
async def test_singular_broadened_term_reads_naturally(monkeypatch):
    # When backoff collapses to exactly one surviving term, "the 1 most specific of
    # your N terms" reads awkwardly. Plural wording (asserted above) must not change.
    from src.agent import tools as tools_mod
    result = PriorArtResult([], ["BRAF"], 8)
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake(result))
    out = await _execute_search_prior_art(
        "novel treatment method for BRAF disease using approach"
    )
    assert "BROADER" in out
    assert "1 most specific" not in out
    assert "single most specific of your 8" in out


@pytest.mark.asyncio
async def test_empty_query_is_not_reported_as_a_negative_result(monkeypatch):
    # search_prior_art("") / an all-punctuation query short-circuits to
    # PriorArtResult(hits=[], terms_used=[], total_terms=0) WITHOUT ever calling
    # USPTO (src/services/patents.py). That must never render as "No US filings
    # matched" — indistinguishable from a real negative — nor reuse the UNAVAILABLE
    # wording verbatim, since this is a bad query, not a tool outage.
    from src.agent import tools as tools_mod
    monkeypatch.setattr(
        tools_mod, "search_prior_art",
        lambda q, limit=10: _fake(PriorArtResult([], [], 0)),
    )
    out = await _execute_search_prior_art("!!!")
    assert "no us filings matched" not in out.lower()
    assert "no search was performed" in out.lower()
    assert "novelty" in out.lower() or "freedom-to-operate" in out.lower()


def test_tool_description_demands_a_short_specific_query():
    spec = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_prior_art")
    text = spec["description"] + spec["input_schema"]["properties"]["query"]["description"]
    assert "2-4" in text
    assert "title" in text.lower()
