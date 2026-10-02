"""Wikidata founder lookup by ORCID (spec §7.5 Step 1b, §8). The `wikidata_*.json`
fixtures are the live query service's answers to the exact query text in
`wikidata._QUERY`, recorded on 2026-10-02."""
from pathlib import Path

import httpx
import pytest

from src.services.company_sources import SourceUnavailable, wikidata

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "company_discovery"
SPARQL_JSON = {"content-type": "application/sparql-results+json;charset=utf-8"}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    slept: list[float] = []

    async def _record_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(wikidata, "_interval", lambda: 0.0)
    wikidata._PACER.reset()
    monkeypatch.setattr(wikidata, "_sleep", _record_sleep)
    return slept


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def test_a_p112_match_returns_every_founded_item(respx_mock):
    route = respx_mock.get(wikidata.SPARQL_URL).mock(return_value=httpx.Response(
        200, content=_fixture("wikidata_p112_0000-0002-7086-765X.json"), headers=SPARQL_JSON))
    result = await wikidata.founded_by_orcid("0000-0002-7086-765X", contact="ops@example.org")
    assert result.pi_item == "Q311003"
    assert [(c.company_name, c.item, c.url) for c in result.companies] == [
        ("Celera Corporation", "Q643341", "https://www.wikidata.org/wiki/Q643341"),
        ("J. Craig Venter Institute", "Q1439786", "https://www.wikidata.org/wiki/Q1439786"),
        ("Synthetic Genomics", "Q4050516", "https://www.wikidata.org/wiki/Q4050516"),
    ]
    request = route.calls.last.request
    assert request.headers["User-Agent"] == (
        f"copi-science-company-discovery/1.0 (ops@example.org) httpx/{httpx.__version__}"
    )
    assert request.headers["Accept"] == "application/sparql-results+json"
    assert request.url.params["format"] == "json"
    query = request.url.params["query"]
    assert '?pi wdt:P496 "0000-0002-7086-765X" .' in query
    assert "OPTIONAL { ?company wdt:P112 ?pi . }" in query


async def test_no_p112_match_returns_the_pi_item_and_no_company(respx_mock):
    respx_mock.get(wikidata.SPARQL_URL).mock(return_value=httpx.Response(
        200, content=_fixture("wikidata_no_p112_0000-0003-1195-438X.json"), headers=SPARQL_JSON))
    result = await wikidata.founded_by_orcid("0000-0003-1195-438X", contact="ops@example.org")
    assert result.pi_item == "Q7926425" and result.companies == []


async def test_a_429_is_retried_once_after_retry_after(respx_mock, _no_waiting):
    route = respx_mock.get(wikidata.SPARQL_URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, content=_fixture("wikidata_p112_0000-0002-7086-765X.json"), headers=SPARQL_JSON),
    ])
    result = await wikidata.founded_by_orcid("0000-0002-7086-765X", contact="ops@example.org")
    assert len(result.companies) == 3 and route.call_count == 2
    assert _no_waiting == [2.0]


async def test_a_second_429_makes_the_source_unavailable(respx_mock, _no_waiting):
    respx_mock.get(wikidata.SPARQL_URL).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "120"}),
        httpx.Response(429, headers={"Retry-After": "120"}),
    ])
    with pytest.raises(SourceUnavailable, match="HTTP 429"):
        await wikidata.founded_by_orcid("0000-0002-7086-765X", contact="ops@example.org")
    assert _no_waiting == [wikidata.RETRY_AFTER_CAP_SECONDS]


@pytest.mark.parametrize("failure", [httpx.Response(500), httpx.ConnectError("reset"),
                                     httpx.Response(200, text="<html>not json</html>")])
async def test_transport_status_and_body_failures_are_unavailable(respx_mock, failure):
    if isinstance(failure, Exception):
        respx_mock.get(wikidata.SPARQL_URL).mock(side_effect=failure)
    else:
        respx_mock.get(wikidata.SPARQL_URL).mock(return_value=failure)
    with pytest.raises(SourceUnavailable):
        await wikidata.founded_by_orcid("0000-0002-7086-765X", contact="ops@example.org")


async def test_no_contact_or_bad_orcid_sends_nothing(respx_mock):
    route = respx_mock.get(wikidata.SPARQL_URL)
    with pytest.raises(SourceUnavailable, match="SEC_USER_AGENT unset"):
        await wikidata.founded_by_orcid("0000-0002-7086-765X", contact=None)
    with pytest.raises(SourceUnavailable, match="no valid ORCID"):
        await wikidata.founded_by_orcid('0000" } #', contact="ops@example.org")
    assert route.call_count == 0


def test_rows_without_a_usable_label_add_nothing():
    data = {"results": {"bindings": [
        {"pi": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1"},
         "company": {"type": "uri", "value": "http://www.wikidata.org/entity/Q2"},
         "companyLabel": {"type": "literal", "value": "Q2"}},
        {"pi": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1"},
         "company": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3"}},
    ]}}
    result = wikidata.parse_bindings(data)
    assert result.pi_item == "Q1" and result.companies == []
    with pytest.raises(SourceUnavailable):
        wikidata.parse_bindings({"head": {}})


@pytest.mark.parametrize(("value", "contact"), [
    ("Blackbird Labs admin@example.org", "admin@example.org"),
    ("https://example.org/contact", "https://example.org/contact"),
    ("", None),
    ("   ", None),
])
def test_contact_from_sec_user_agent(value, contact):
    assert wikidata.contact_from(value) == contact


@pytest.mark.parametrize(("header", "seconds"), [
    ("3", 3.0), ("600", wikidata.RETRY_AFTER_CAP_SECONDS), (None, wikidata.RETRY_AFTER_DEFAULT_SECONDS),
    ("soon", wikidata.RETRY_AFTER_DEFAULT_SECONDS), ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
])
def test_retry_after_parsing(header, seconds):
    assert wikidata._retry_after_seconds(header) == seconds
