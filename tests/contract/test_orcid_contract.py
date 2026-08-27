"""Contract tests for src/services/orcid.py against ORCID pub API v3.0 shapes.

Pins how the parsers read a real-shaped record and how each function behaves on
non-200 / timeout / malformed JSON. respx intercepts the httpx.AsyncClient the
service builds internally.
"""

import json

import httpx
import pytest
import respx

from src.services import orcid

pytestmark = pytest.mark.contract

BASE = "https://pub.orcid.org/v3.0"
OID = "0000-0002-1825-0097"


def _record():
    return {
        "person": {
            "name": {
                "given-names": {"value": "Josiah"},
                "family-name": {"value": "Carberry"},
            },
            "emails": {
                "email": [
                    {"email": "secondary@brown.edu", "primary": False},
                    {"email": "josiah@brown.edu", "primary": True},
                ]
            },
            "researcher-urls": {
                "researcher-url": [{"url": {"value": "https://lab.example.edu"}}]
            },
        },
        "activities-summary": {
            "employments": {
                "affiliation-group": [
                    {
                        "summaries": [
                            {
                                "employment-summary": {
                                    "end-date": None,
                                    "display-index": "0",
                                    "organization": {"name": "Brown University"},
                                    "department-name": "Psychoceramics",
                                }
                            }
                        ]
                    }
                ]
            }
        },
    }


@respx.mock
async def test_fetch_orcid_record_returns_json():
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(200, json=_record()))
    rec = await orcid.fetch_orcid_record(OID)
    assert rec["person"]["name"]["given-names"]["value"] == "Josiah"


@respx.mock
async def test_fetch_orcid_profile_parses_all_fields():
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(200, json=_record()))
    prof = await orcid.fetch_orcid_profile(OID)
    assert prof == {
        "orcid": OID,
        "name": "Josiah Carberry",
        "email": "josiah@brown.edu",  # primary wins over the first-seen secondary
        "institution": "Brown University",
        "department": "Psychoceramics",
        "lab_website": "https://lab.example.edu",
        "employments": [
            {"organization": "Brown University", "start_year": None, "current": True},
        ],
    }


@respx.mock
async def test_fetch_orcid_profile_extracts_every_employment_with_start_years():
    """The tenure derivation (src/services/jhu_rules.py) needs org name,
    start year and whether the employment is current — for ALL employments,
    not just the primary current one the institution field uses."""
    record = {
        "person": {"name": {"given-names": {"value": "A"}, "family-name": {"value": "B"}}},
        "activities-summary": {
            "employments": {
                "affiliation-group": [
                    {"summaries": [{"employment-summary": {
                        "end-date": None,
                        "display-index": "0",
                        "organization": {"name": "Johns Hopkins University"},
                        "start-date": {"year": {"value": "2011"}},
                    }}]},
                    {"summaries": [{"employment-summary": {
                        "end-date": {"year": {"value": "2009"}},
                        "display-index": "1",
                        "organization": {"name": "Stanford University"},
                        "start-date": {"year": {"value": "2004"}},
                    }}]},
                    {"summaries": [{"employment-summary": {
                        "end-date": None,
                        "display-index": "2",
                        "organization": {"name": "HHMI"},
                        "start-date": None,
                    }}]},
                ]
            }
        },
    }
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(200, json=record))
    prof = await orcid.fetch_orcid_profile(OID)
    assert prof["employments"] == [
        {"organization": "Johns Hopkins University", "start_year": 2011, "current": True},
        {"organization": "Stanford University", "start_year": 2004, "current": False},
        {"organization": "HHMI", "start_year": None, "current": True},
    ]


@respx.mock
async def test_fetch_orcid_profile_falls_back_to_orcid_when_no_name():
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(200, json={"person": {}}))
    prof = await orcid.fetch_orcid_profile(OID)
    assert prof["name"] == OID
    assert "institution" not in prof


@respx.mock
async def test_fetch_orcid_grants_parses_titles():
    data = {
        "group": [
            {"funding-summary": [{"title": {"title": {"value": "R01 Big Grant"}}}]},
            {"funding-summary": [{"title": {"title": {"value": "NSF Small Grant"}}}]},
        ]
    }
    respx.get(f"{BASE}/{OID}/fundings").mock(return_value=httpx.Response(200, json=data))
    assert await orcid.fetch_orcid_grants(OID) == ["R01 Big Grant", "NSF Small Grant"]


@respx.mock
async def test_fetch_orcid_works_parses_ids_and_year():
    data = {
        "group": [
            {
                "work-summary": [
                    {
                        "title": {"title": {"value": "A Paper"}},
                        "type": "journal-article",
                        "publication-date": {"year": {"value": "2019"}},
                        "external-ids": {
                            "external-id": [
                                {"external-id-type": "PMID", "external-id-value": "31000000"},
                                {"external-id-type": "doi", "external-id-value": "10.1/abc"},
                            ]
                        },
                    }
                ]
            }
        ]
    }
    respx.get(f"{BASE}/{OID}/works").mock(return_value=httpx.Response(200, json=data))
    works = await orcid.fetch_orcid_works(OID)
    assert works == [
        {"title": "A Paper", "year": 2019, "pmid": "31000000", "doi": "10.1/abc", "type": "journal-article"}
    ]


# ---- error paths (pinning current behavior, not fixing) ----


@respx.mock
async def test_fetch_orcid_record_raises_on_non_200():
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await orcid.fetch_orcid_record(OID)


@respx.mock
async def test_fetch_orcid_profile_propagates_http_error():
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await orcid.fetch_orcid_profile(OID)


@respx.mock
async def test_fetch_orcid_record_raises_on_malformed_json():
    respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(json.JSONDecodeError):  # malformed body -> stdlib json error propagates
        await orcid.fetch_orcid_record(OID)


@respx.mock
async def test_fetch_orcid_grants_swallows_non_200_returns_empty():
    route = respx.get(f"{BASE}/{OID}/fundings").mock(return_value=httpx.Response(503))
    assert await orcid.fetch_orcid_grants(OID) == []
    assert route.called  # fail if the mocked URL drifts — the swallowed error would otherwise hide it


@respx.mock
async def test_fetch_orcid_grants_swallows_timeout_returns_empty():
    route = respx.get(f"{BASE}/{OID}/fundings").mock(side_effect=httpx.TimeoutException("t"))
    assert await orcid.fetch_orcid_grants(OID) == []
    assert route.called


@respx.mock
async def test_fetch_orcid_works_swallows_non_200_returns_empty():
    route = respx.get(f"{BASE}/{OID}/works").mock(return_value=httpx.Response(500))
    assert await orcid.fetch_orcid_works(OID) == []
    assert route.called


@respx.mock
async def test_fetch_orcid_works_swallows_timeout_returns_empty():
    route = respx.get(f"{BASE}/{OID}/works").mock(side_effect=httpx.TimeoutException("t"))
    assert await orcid.fetch_orcid_works(OID) == []
    assert route.called
