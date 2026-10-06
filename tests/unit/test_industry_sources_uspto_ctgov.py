import json

import httpx
import pytest
import respx

from src.services import odp
from src.services.industry_sources import ctgov, uspto_inventor
from src.services.industry_sources.ctgov import evidence_from_study
from src.services.industry_sources.registry import SourceUnavailable
from src.services.industry_sources.uspto_inventor import evidence_from_application


def app(filing="2022-12-01", applicants=("The Johns Hopkins University",), title="OXAZOLIDINONE FOR TREATMENT OF MYCOBACTERIUM TUBERCULOSIS", assignees=()):
    return {"applicationNumberText": "17123456",
            "applicationMetaData": {"filingDate": filing, "inventionTitle": title, "firstInventorName": "Gyanu Lamichhane",
                                    "applicantBag": [{"applicantNameText": a} for a in applicants]},
            "assignmentBag": [{"assigneeBag": [{"assigneeNameText": s}]} for s in assignees]}


def test_jhu_filed_in_tenure_with_keyword_overlap_is_patent_filed():
    items = evidence_from_application(app(), 2018, {"tuberculosis", "beta-lactam"})
    assert [i.kind for i in items] == ["patent_filed"] and items[0].pi_role == "inventor"


def test_no_jhu_applicant_yields_nothing():
    assert evidence_from_application(app(applicants=("University of St. Thomas",)), 2018, {"tuberculosis"}) == []


def test_pre_tenure_filing_yields_nothing():
    assert evidence_from_application(app(filing="2016-01-01"), 2018, {"tuberculosis"}) == []


def test_no_keyword_overlap_yields_nothing():
    assert evidence_from_application(app(title="WIDGET"), 2018, {"tuberculosis"}) == []


def test_company_assignee_adds_patent_assigned():
    items = evidence_from_application(app(assignees=("Paratek Pharmaceuticals, Inc.",)), 2018, {"tuberculosis"})
    assert {i.kind for i in items} == {"patent_filed", "patent_assigned"}


def test_academic_assignee_does_not_count_as_industry():
    items = evidence_from_application(app(assignees=("University of Elsewhere",)), 2018, {"tuberculosis"})
    assert [i.kind for i in items] == ["patent_filed"]


def test_short_acronym_keyword_matches_via_word_boundary_tokenizer():
    items = evidence_from_application(app(title="METHOD FOR TREATING TB"), 2018, {"TB"})
    assert [i.kind for i in items] == ["patent_filed"]


def test_hyphenated_keyword_matches_a_title_word_it_splits_into():
    items = evidence_from_application(app(title="LACTAM ANTIBIOTICS"), 2018, {"beta-lactam"})
    assert [i.kind for i in items] == ["patent_filed"]


def test_stopword_only_keyword_matches_nothing():
    assert evidence_from_application(app(title="METHOD FOR TREATING TUBERCULOSIS"), 2018, {"method"}) == []


def study(lead="Sidney Kimmel Comprehensive Cancer Center at Johns Hopkins", lead_class="OTHER", collab=("Novartis Pharmaceuticals",), start="2020-09", cond=("Lymphoma",)):
    return {"protocolSection": {"identificationModule": {"nctId": "NCT01665768", "briefTitle": "T"},
            "statusModule": {"startDateStruct": {"date": start}},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": lead, "class": lead_class}, "collaborators": [{"name": c, "class": "INDUSTRY"} for c in collab]},
            "conditionsModule": {"conditions": list(cond)}}}


def test_jhu_led_trial_with_industry_collaborator_counts():
    items = evidence_from_study(study(), 2018, {"lymphoma"})
    assert len(items) == 1 and items[0].kind == "trial_industry_collab" and items[0].company_name == "Novartis Pharmaceuticals"


def test_industry_led_trial_yields_nothing():
    assert evidence_from_study(study(lead="Novartis Pharmaceuticals", lead_class="INDUSTRY"), 2018, {"lymphoma"}) == []


def test_condition_mismatch_yields_nothing():
    assert evidence_from_study(study(cond=("Psoriasis",)), 2018, {"lymphoma"}) == []


def test_missing_collaborator_name_or_nct_is_skipped_not_a_crash():
    st = study()
    st["protocolSection"]["sponsorCollaboratorsModule"]["collaborators"] = [{"class": "INDUSTRY"}]
    assert evidence_from_study(st, 2018, {"lymphoma"}) == []
    st2 = study()
    del st2["protocolSection"]["identificationModule"]["nctId"]
    assert evidence_from_study(st2, 2018, {"lymphoma"}) == []


def _odp_pages(total: int):
    """An ODP stand-in answering each offset/limit from `total` numbered applications."""
    def answer(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["pagination"]
        start, limit = page["offset"], page["limit"]
        bag = [{"applicationNumberText": f"{n:08d}"} for n in range(start, min(start + limit, total))]
        if not bag and total == 0:
            return httpx.Response(404, json={"error": "no matches"})
        return httpx.Response(200, json={"count": total, "patentFileWrapperDataBag": bag})
    return answer


@pytest.fixture
def _odp_key(monkeypatch):
    monkeypatch.setattr(odp, "ODP_PACE_INTERVAL", 0.0)
    odp.ODP_PACER.reset()
    monkeypatch.setattr(uspto_inventor, "get_settings", lambda: type("S", (), {"uspto_api_key": "k"})())


@pytest.mark.asyncio
@respx.mock
async def test_uspto_pages_until_the_reported_count(_odp_key):
    route = respx.post(uspto_inventor.SEARCH_URL).mock(side_effect=_odp_pages(250))
    paged = await uspto_inventor.fetch_jhu_applications("Jane Wang")
    assert len(paged.items) == 250 and paged.truncated is False
    assert [json.loads(c.request.content)["pagination"]["offset"] for c in route.calls] == [0, 100, 200]


@pytest.mark.asyncio
@respx.mock
async def test_uspto_stops_at_the_page_cap_and_says_truncated(_odp_key):
    route = respx.post(uspto_inventor.SEARCH_URL).mock(side_effect=_odp_pages(1500))
    paged = await uspto_inventor.fetch_jhu_applications("Jane Wang")
    assert route.call_count == uspto_inventor.MAX_PAGES == 10
    assert len(paged.items) == 1000 and paged.truncated is True


@pytest.mark.asyncio
@respx.mock
async def test_exactly_the_cap_is_not_truncated(_odp_key):
    respx.post(uspto_inventor.SEARCH_URL).mock(side_effect=_odp_pages(1000))
    paged = await uspto_inventor.fetch_jhu_applications("Jane Wang")
    assert len(paged.items) == 1000 and paged.truncated is False


@pytest.mark.asyncio
@respx.mock
async def test_a_uspto_404_is_no_applications(_odp_key):
    """ODP answers 404 for a query with no matches; that is an empty result, not three
    failures and a dead job."""
    respx.post(uspto_inventor.SEARCH_URL).mock(side_effect=_odp_pages(0))
    paged = await uspto_inventor.fetch_jhu_applications("Jane Wang")
    assert paged.items == [] and paged.truncated is False


@pytest.mark.asyncio
async def test_uspto_without_a_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(uspto_inventor, "get_settings", lambda: type("S", (), {"uspto_api_key": ""})())
    with pytest.raises(SourceUnavailable) as raised:
        await uspto_inventor.fetch_jhu_applications("Jane Wang")
    assert raised.value.reason == "no_api_key"


@pytest.mark.asyncio
async def test_an_orcid_id_name_is_unavailable_for_both_sources(monkeypatch):
    monkeypatch.setattr(uspto_inventor, "get_settings", lambda: type("S", (), {"uspto_api_key": "k"})())
    for fetch in (uspto_inventor.fetch_jhu_applications, ctgov.fetch_jhu_industry_trials):
        with pytest.raises(SourceUnavailable) as raised:
            await fetch("0000-0002-1825-0097")
        assert raised.value.reason == "no_usable_name"


def _study(nct: str) -> dict:
    return study() | {"protocolSection": {**study()["protocolSection"],
                                          "identificationModule": {"nctId": nct, "briefTitle": "T"}}}


@pytest.mark.asyncio
@respx.mock
async def test_ctgov_follows_next_page_tokens():
    pages = {None: (["NCT1", "NCT2"], "t2"), "t2": (["NCT3"], None)}

    def answer(request: httpx.Request) -> httpx.Response:
        ids, nxt = pages[request.url.params.get("pageToken")]
        body = {"studies": [_study(i) for i in ids]} | ({"nextPageToken": nxt} if nxt else {})
        return httpx.Response(200, json=body)

    route = respx.get(ctgov.BASE).mock(side_effect=answer)
    paged = await ctgov.fetch_jhu_industry_trials("Jane Wang")
    assert [s["protocolSection"]["identificationModule"]["nctId"] for s in paged.items] == [
        "NCT1", "NCT2", "NCT3"]
    assert paged.truncated is False and route.call_count == 2
    assert route.calls[0].request.url.params["pageSize"] == "100"


@pytest.mark.asyncio
@respx.mock
async def test_ctgov_stops_at_the_page_cap_and_says_truncated():
    respx.get(ctgov.BASE).mock(return_value=httpx.Response(
        200, json={"studies": [_study("NCT9")], "nextPageToken": "again"}))
    paged = await ctgov.fetch_jhu_industry_trials("Jane Wang")
    assert len(paged.items) == ctgov.MAX_PAGES and paged.truncated is True


@pytest.mark.contract
async def test_fetch_jhu_industry_trials_response_shape_flows_into_evidence_from_study():
    """Guards against fields= drifting back to the flat v2 legacy names, which
    evidence_from_study cannot parse (it reads nested protocolSection.*)."""
    response_body = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {"nctId": "NCT03132584", "briefTitle": "Cyclophosphamide and Alemtuzumab In Lymphoma"},
                    "statusModule": {"startDateStruct": {"date": "2020-09-01"}},
                    "sponsorCollaboratorsModule": {
                        "leadSponsor": {"name": "Johns Hopkins University", "class": "OTHER"},
                        "collaborators": [{"name": "Genzyme, a Sanofi Company", "class": "INDUSTRY"}],
                    },
                    "conditionsModule": {"conditions": ["Non Hodgkin Lymphoma", "Diffuse Large B Cell Lymphoma"]},
                }
            }
        ]
    }
    with respx.mock() as router:
        router.get(ctgov.BASE).mock(return_value=httpx.Response(200, json=response_body))
        paged = await ctgov.fetch_jhu_industry_trials("Someone MD")
    items = [item for study_ in paged.items for item in evidence_from_study(study_, 2018, {"lymphoma"})]
    assert len(items) == 1 and items[0].kind == "trial_industry_collab"
