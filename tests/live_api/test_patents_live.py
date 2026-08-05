"""PatentsView (USPTO) prior-art search, live.

Gated on `PATENTSVIEW_API_KEY` in addition to the blanket `live_api` marker (see
`tests/conftest.py::pytest_collection_modifyitems`), because — unlike grants.gov,
ORCID and PubMed — `search_prior_art` degrades to an empty list rather than an error
when no key is configured (src/services/patents.py `_api_key`). Without this extra
skip, running the whole `live_api` tier with `LIVE_API_TESTS=1` but no PatentsView key
would make this test pass for the wrong reason: "no key" and "the API is broken" would
both report as an empty list, and the assertions below would never actually exercise
PatentsView.
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
    hits = await search_prior_art("crispr")
    assert isinstance(hits, list)
    if hits:
        assert "patent_id" in hits[0] and "title" in hits[0]
