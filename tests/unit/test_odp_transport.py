"""One ODP transport; each caller keeps its own client options and request bodies
(patents: timeout 60 + redirects for the fileLocationURI 302; uspto_inventor:
timeout 30, no redirects, its own fields list)."""
import json

import httpx
import pytest
import respx

from src.services import odp, patents
from src.services.industry_sources import Paged, uspto_inventor

pytestmark = pytest.mark.asyncio


def test_one_url_one_pacer():
    assert patents.SEARCH_URL == uspto_inventor.SEARCH_URL == odp.ODP_SEARCH_URL
    assert odp.ODP_PACE_INTERVAL == 1.0


def test_headers():
    assert odp.odp_headers("k") == {"X-API-KEY": "k"}


@pytest.mark.parametrize("timeout,redirects", [(60, True), (30, False)])
def test_client_options(timeout, redirects):
    c = odp.odp_client(timeout=timeout, follow_redirects=redirects)
    assert c.timeout == httpx.Timeout(timeout) and c.follow_redirects is redirects


@respx.mock
async def test_uspto_inventor_request_is_unchanged(monkeypatch):
    monkeypatch.setattr(odp, "ODP_PACE_INTERVAL", 0.0)
    odp.ODP_PACER.reset()
    monkeypatch.setattr(uspto_inventor, "get_settings",
                        lambda: type("S", (), {"uspto_api_key": "key1"})())
    route = respx.post(odp.ODP_SEARCH_URL).mock(return_value=httpx.Response(200, json={"patentFileWrapperDataBag": []}))
    assert await uspto_inventor.fetch_jhu_applications("Jane Doe") == Paged([])
    req = route.calls[0].request
    assert req.headers["X-API-KEY"] == "key1"
    body = json.loads(req.content)
    assert body["fields"] == uspto_inventor._FIELDS
    assert body["pagination"] == {"offset": 0, "limit": 100}
    assert body["sort"] == [{"field": "applicationMetaData.filingDate", "order": "desc"}]


@respx.mock
async def test_uspto_inventor_404_is_empty(monkeypatch):
    monkeypatch.setattr(odp, "ODP_PACE_INTERVAL", 0.0)
    odp.ODP_PACER.reset()
    monkeypatch.setattr(uspto_inventor, "get_settings",
                        lambda: type("S", (), {"uspto_api_key": "key1"})())
    respx.post(odp.ODP_SEARCH_URL).mock(return_value=httpx.Response(404))
    assert await uspto_inventor.fetch_jhu_applications("Jane Doe") == Paged([])
