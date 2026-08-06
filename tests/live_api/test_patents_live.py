"""USPTO Open Data Portal prior-art search, live.

Gated on `PATENTSVIEW_API_KEY` in addition to the blanket `live_api` marker (see
`tests/conftest.py::pytest_collection_modifyitems`), because — unlike grants.gov,
ORCID and PubMed — `search_prior_art` returns `None` rather than raising when no key
is configured (src/services/patents.py `_api_key`). Without this extra skip, running
the whole `live_api` tier with `LIVE_API_TESTS=1` but no key would make these tests
pass for the wrong reason: "no key" and "the API is broken" both surface as `None`,
and the assertions below would never actually exercise the live endpoint.

`search_prior_art` returns `PriorArtResult | None` (see src/services/patents.py):
``None`` means the search could not run at all (no key, unreachable, rate-limited,
unparseable JSON) — never a clean negative, and a live 429 mid-run degrades to this
same `None`. Every test here treats `None` as inconclusive (skip), never as a
failure and never as evidence of no prior art.
"""

import os

import pytest

from src.services.patents import search_prior_art

pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(not os.getenv("PATENTSVIEW_API_KEY"), reason="no PatentsView key"),
]


@pytest.mark.asyncio
async def test_real_search_returns_hits_for_a_common_term(monkeypatch):
    monkeypatch.setenv("PATENTSVIEW_API_KEY", os.environ["PATENTSVIEW_API_KEY"])
    from src.config import get_settings
    get_settings.cache_clear()
    result = await search_prior_art("crispr")
    if result is None:
        pytest.skip("live search could not run (rate limited / unreachable) — inconclusive")
    assert isinstance(result.hits, list)
    if result.hits:
        assert "patent_id" in result.hits[0] and "title" in result.hits[0]


@pytest.mark.asyncio
async def test_real_search_broadens_an_overlong_query_to_a_hit(monkeypatch):
    """The exact production failure this whole change fixes: a verbose, sentence-like
    query is what PIs actually type, and ANDing every token on the title (the
    pre-backoff behaviour) is a guaranteed zero-hit. This drives that query at the
    real endpoint and confirms the backoff recovers a genuine hit rather than a false
    clean negative.

    Every token but "BRAF" here is in `_GENERIC` (src/services/patents.py), so the
    ranked pool narrows to that single term and both backoff tiers collapse to
    ``["BRAF"]`` — this is the same query as
    tests/unit/test_patents.py::test_lone_specific_term_among_generics_still_gets_a_narrower_tier,
    whose comment records it as live-verified (10 real hits) when that backoff was
    written, so a title search on "BRAF" alone is about as low-flake a live
    assertion as this endpoint allows.

    A 429 mid-backoff surfaces as `None` (search_prior_art's contract) — that is
    "could not search", not a clean result, so it is skipped rather than failed.
    """
    monkeypatch.setenv("PATENTSVIEW_API_KEY", os.environ["PATENTSVIEW_API_KEY"])
    from src.config import get_settings
    get_settings.cache_clear()
    result = await search_prior_art("novel treatment method for BRAF disease using approach")
    if result is None:
        pytest.skip("live search could not run (rate limited / unreachable) — inconclusive")
    assert result.terms_used == ["BRAF"]
    assert result.broadened is True
    assert len(result.hits) > 0
    assert "patent_id" in result.hits[0] and "title" in result.hits[0]
