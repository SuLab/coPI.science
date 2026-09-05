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


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """These tests pin parse/error-swallow behaviour, not the retry loop (issue #23 COR-29a) —
    zero the backoff so a mocked 5xx/timeout doesn't add ~3.5s of real sleep per test."""
    monkeypatch.setattr(orcid, "_RETRY_BACKOFF", 0)


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
    }


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


# ---- null-container tolerance (COR-15: ORCID sends explicit `null`, not a missing key) ----


def _record_all_null_containers():
    return {
        "person": {
            "name": {"given-names": None, "family-name": None},
            "emails": None,
            "researcher-urls": None,
        },
        "activities-summary": {
            "employments": {
                "affiliation-group": [
                    {
                        "summaries": [
                            {
                                "employment-summary": {
                                    "end-date": None,
                                    "display-index": None,
                                    "organization": None,
                                    "department-name": None,
                                }
                            }
                        ]
                    }
                ]
            }
        },
    }


@respx.mock
async def test_fetch_orcid_profile_tolerates_null_containers():
    respx.get(f"{BASE}/{OID}/record").mock(
        return_value=httpx.Response(200, json=_record_all_null_containers())
    )
    prof = await orcid.fetch_orcid_profile(OID)
    assert prof["name"] == OID  # given-names and family-name both null -> falls back to the id
    assert prof.get("institution") is None  # organization: null
    assert "lab_website" not in prof  # researcher-urls: null
    assert "email" not in prof  # emails: null


@respx.mock
async def test_fetch_orcid_grants_tolerates_a_null_title_container():
    data = {"group": [{"funding-summary": [{"title": None}]}]}
    respx.get(f"{BASE}/{OID}/fundings").mock(return_value=httpx.Response(200, json=data))
    assert await orcid.fetch_orcid_grants(OID) == []


@respx.mock
async def test_fetch_orcid_works_tolerates_null_containers_and_a_non_numeric_year():
    data = {
        "group": [
            {
                "work-summary": [
                    {
                        "title": None,
                        "type": "journal-article",
                        "publication-date": {"year": {"value": None}},
                        "external-ids": None,
                    }
                ]
            }
        ]
    }
    respx.get(f"{BASE}/{OID}/works").mock(return_value=httpx.Response(200, json=data))
    works = await orcid.fetch_orcid_works(OID)
    assert works == [
        {"title": "", "year": None, "pmid": None, "doi": None, "type": "journal-article"}
    ]


@respx.mock
async def test_fetch_orcid_works_tolerates_a_null_external_id_type():
    data = {
        "group": [
            {
                "work-summary": [
                    {
                        "title": {"title": {"value": "T"}},
                        "type": "journal-article",
                        "publication-date": None,
                        "external-ids": {
                            "external-id": [
                                {"external-id-type": None, "external-id-value": "31000000"},
                                {"external-id-type": "pmid", "external-id-value": "31000001"},
                            ]
                        },
                    }
                ]
            }
        ]
    }
    respx.get(f"{BASE}/{OID}/works").mock(return_value=httpx.Response(200, json=data))
    works = await orcid.fetch_orcid_works(OID)
    # the null-typed id is skipped, not fatal, and does not block the valid one after it
    assert works[0]["pmid"] == "31000001"


# ---- retry behaviour, not just the wiring (#23 COR-29a / R6) ----
#
# Until now the only thing pinning orcid.py's retry loop was the `_no_retry_backoff`
# fixture above — i.e. the *wiring*: if the module stopped calling get_with_retry, the
# monkeypatch target would still exist and every test would stay green. The retry
# BEHAVIOUR was covered generically in tests/unit/test_http_retry.py, against a hand-built
# client, so nothing observed that an ORCID fetch survives a transient failure.


@respx.mock
async def test_fetch_orcid_record_retries_a_transient_503_then_succeeds():
    route = respx.get(f"{BASE}/{OID}/record").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=_record())]
    )
    record = await orcid.fetch_orcid_record(OID)
    assert route.call_count == 2, "the 503 was not retried"
    assert record["person"]["name"]["family-name"]["value"] == "Carberry"


@respx.mock
async def test_fetch_orcid_record_gives_up_after_the_retry_budget():
    """Four attempts total (retries=3), then the caller sees the real HTTPStatusError.

    Pins the budget as well as the retry: an unbounded loop against a hard-down ORCID
    would hold a worker job open indefinitely, and `raise_for_status()` on the last
    attempt is what keeps every existing `except Exception` caller working.
    """
    route = respx.get(f"{BASE}/{OID}/record").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await orcid.fetch_orcid_record(OID)
    assert route.call_count == 4


@respx.mock
async def test_fetch_orcid_works_returns_the_works_after_a_transient_429():
    """The retry sits INSIDE fetch_orcid_works' swallow-all try, so a single 429 no
    longer costs a PI their whole publication list — it used to degrade to []."""
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
                                {"external-id-type": "pmid", "external-id-value": "31000000"},
                            ]
                        },
                    }
                ]
            }
        ]
    }
    route = respx.get(f"{BASE}/{OID}/works").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=data)]
    )
    works = await orcid.fetch_orcid_works(OID)
    assert route.call_count == 2
    assert [w["pmid"] for w in works] == ["31000000"]


@respx.mock
async def test_fetch_orcid_grants_retries_a_transport_error_then_succeeds():
    """A connect/read timeout is retried too, not only a retryable status."""
    data = {"group": [{"funding-summary": [{"title": {"title": {"value": "R01 Big Grant"}}}]}]}
    route = respx.get(f"{BASE}/{OID}/fundings").mock(
        side_effect=[httpx.ConnectTimeout("boom"), httpx.Response(200, json=data)]
    )
    assert await orcid.fetch_orcid_grants(OID) == ["R01 Big Grant"]
    assert route.call_count == 2
