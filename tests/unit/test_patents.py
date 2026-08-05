import pytest
import respx
import httpx

from src.services import patents


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_normalised_hits(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(return_value=httpx.Response(
        200, json={"patents": [
            {"patent_id": "123", "patent_title": "Widget", "patent_date": "2020-01-01",
             "patent_abstract": "An abstract", "assignees": [{"assignee_organization": "Acme"}]},
        ]},
    ))
    hits = await patents.search_prior_art("widget")
    assert hits[0]["patent_id"] == "123"
    assert hits[0]["title"] == "Widget"
    assert hits[0]["assignees"] == ["Acme"]


@pytest.mark.asyncio
@respx.mock
async def test_missing_key_returns_empty_and_does_not_call(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "")
    # Register a route that WOULD succeed if called, so this test proves the
    # guard short-circuits before any HTTP call — not just that the result
    # happens to be []. If the `if not key: return []` guard were removed,
    # the request would hit this route and route.call_count would be 1,
    # failing the assertion below (empty "patents" alone would still let
    # hits == [] pass vacuously, which is why call_count is asserted too).
    route = respx.get(patents.SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"patents": []})
    )
    hits = await patents.search_prior_art("widget")
    assert hits == []
    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(return_value=httpx.Response(500))
    assert await patents.search_prior_art("widget") == []


@pytest.mark.asyncio
@respx.mock
async def test_rate_limited_returns_empty(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    assert await patents.search_prior_art("x") == []


@pytest.mark.asyncio
@respx.mock
async def test_bad_json_returns_empty(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(return_value=httpx.Response(200, text="not json"))
    assert await patents.search_prior_art("x") == []


from src.agent.tools import _execute_search_prior_art, TOOL_DEFINITIONS

CAVEAT_MARK = "US filings only"


def test_search_prior_art_is_a_registered_tool():
    assert any(t["name"] == "search_prior_art" for t in TOOL_DEFINITIONS)


@pytest.mark.asyncio
async def test_output_always_carries_us_only_caveat(monkeypatch):
    from src.agent import tools as tools_mod
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake([]))
    out = await _execute_search_prior_art("crispr delivery")
    assert CAVEAT_MARK in out
    assert "no us filings matched" in out.lower()


async def _fake(v):
    return v


@pytest.mark.asyncio
async def test_output_carries_caveat_on_has_hits_path(monkeypatch):
    from src.agent import tools as tools_mod

    hit = {
        "patent_id": "123",
        "title": "Widget",
        "date": "2020-01-01",
        "abstract": "An abstract",
        "assignees": ["Acme"],
    }
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _fake([hit]))
    out = await _execute_search_prior_art("widget")
    assert CAVEAT_MARK in out
    assert "US123" in out
    assert "Widget" in out
    assert "Acme" in out
