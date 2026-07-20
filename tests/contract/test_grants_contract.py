"""Contract tests for src/services/grants.py against grants.gov API v1 shapes.

Pins the nested {data: {hitCount, oppHits}} parse path, field mapping, the
detail endpoint's "no number => None" guard, and the raise-on-non-200 behavior
(search/list/detail have no try/except). respx intercepts the internal client.
"""

import httpx
import pytest
import respx

from src.services import grants

pytestmark = pytest.mark.contract

SEARCH_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"


def _search_payload(hits, hit_count=None):
    return {
        "errorcode": 0,
        "msg": "success",
        "data": {"hitCount": hit_count if hit_count is not None else len(hits), "oppHits": hits},
    }


@respx.mock
async def test_search_opportunities_maps_fields():
    hit = {
        "id": 12345,
        "number": "RFA-AI-27-019",
        "title": "Immunology R01",
        "agencyCode": "HHS-NIH11",
        "openDate": "2026-01-01",
        "closeDate": "2026-06-01",
        "description": "Study immunology.",
    }
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=_search_payload([hit])))
    results = await grants.search_opportunities("immunology")
    assert results == [
        {
            "id": 12345,
            "number": "RFA-AI-27-019",
            "title": "Immunology R01",
            "agency": "HHS-NIH11",
            "open_date": "2026-01-01",
            "close_date": "2026-06-01",
            "description": "Study immunology.",
        }
    ]


@respx.mock
async def test_search_opportunities_empty_hits():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=_search_payload([])))
    assert await grants.search_opportunities("nothing") == []


@respx.mock
async def test_search_opportunities_raises_on_non_200():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await grants.search_opportunities("x")


@respx.mock
async def test_list_posted_opportunities_single_page():
    hits = [
        {"id": 1, "number": "N1", "title": "T1", "agencyCode": "NSF",
         "openDate": "2026-01-01", "closeDate": "2026-02-01"},
        {"id": 2, "number": "N2", "title": "T2", "agencyCode": "HHS-NIH11",
         "openDate": "2026-01-05", "closeDate": "2026-02-05"},
    ]
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=_search_payload(hits, hit_count=2)))
    out = await grants.list_posted_opportunities()
    assert [o["number"] for o in out] == ["N1", "N2"]
    assert out[0] == {
        "id": 1, "number": "N1", "title": "T1", "agency": "NSF",
        "open_date": "2026-01-01", "close_date": "2026-02-01",
    }


@respx.mock
async def test_list_posted_opportunities_breaks_on_empty_hits():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=_search_payload([], hit_count=999)))
    # hitCount says more exist, but no hits returned -> loop breaks, no infinite paging
    assert await grants.list_posted_opportunities() == []


@respx.mock
async def test_fetch_opportunity_detail_maps_and_reads_synopsis():
    opp = {
        "id": 777,
        "number": "RFA-AI-27-019",
        "title": "Immunology R01",
        "agencyCode": "HHS-NIH11",
        "description": "desc",
        "openDate": "2026-01-01",
        "closeDate": "2026-06-01",
        "awardCeiling": "500000",
        "awardFloor": "100000",
        "categoryOfFundingActivity": "Health",
        "eligibleApplicants": "Universities",
        "additionalInformationUrl": "https://grants.gov/x",
        "synopsis": {"synopsisDesc": "Full synopsis text."},
    }
    respx.post(DETAIL_URL).mock(return_value=httpx.Response(200, json={"data": opp}))
    detail = await grants.fetch_opportunity_detail("777")
    assert detail["number"] == "RFA-AI-27-019"
    assert detail["synopsis"] == "Full synopsis text."
    assert detail["award_ceiling"] == "500000"
    assert detail["category"] == "Health"


@respx.mock
async def test_fetch_opportunity_detail_returns_none_without_number():
    # detail endpoint returned an error-shaped body (no "number") -> None
    respx.post(DETAIL_URL).mock(return_value=httpx.Response(200, json={"data": {"errorcode": 1, "msg": "not found"}}))
    assert await grants.fetch_opportunity_detail("nope") is None


@respx.mock
async def test_fetch_opportunity_detail_synopsis_non_dict_is_blank():
    opp = {"id": 9, "number": "N9", "synopsis": None}
    respx.post(DETAIL_URL).mock(return_value=httpx.Response(200, json={"data": opp}))
    detail = await grants.fetch_opportunity_detail("9")
    assert detail["synopsis"] == ""


@respx.mock
async def test_search_for_researchers_dedups_by_number_and_tags_keyword():
    hit = {"id": 1, "number": "N1", "title": "T1", "agencyCode": "NSF",
           "openDate": "", "closeDate": "", "description": ""}
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=_search_payload([hit])))
    out = await grants.search_for_researchers({"agent1": ["kw-a", "kw-b"]})
    # Both keywords were actually searched (not short-circuited) — without this, a bug
    # that searched only kw-a would still yield len==1/matched=="kw-a" and pass.
    assert route.call_count == 2
    # same opp number returned for both keywords -> deduped to one, tagged with first
    assert len(out["agent1"]) == 1
    assert out["agent1"][0]["matched_keyword"] == "kw-a"


@respx.mock
async def test_search_for_researchers_swallows_search_errors():
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(500))
    out = await grants.search_for_researchers({"agent1": ["kw-a"]})
    assert out == {"agent1": []}
    assert route.called  # fail if the mocked URL drifts — the swallowed error would otherwise hide it
