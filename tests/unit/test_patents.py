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
async def test_missing_key_returns_empty_and_does_not_call(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "")
    hits = await patents.search_prior_art("widget")
    assert hits == []


@pytest.mark.asyncio
@respx.mock
async def test_http_error_returns_empty(monkeypatch):
    monkeypatch.setattr(patents, "_api_key", lambda: "k")
    respx.get(patents.SEARCH_URL).mock(return_value=httpx.Response(500))
    assert await patents.search_prior_art("widget") == []
