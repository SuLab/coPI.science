import httpx
import pytest
import respx

from src.services.industry_sources import ctgov
from src.services.industry_sources.ctgov import evidence_from_study
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


@pytest.mark.contract
async def test_fetch_jhu_industry_trials_response_shape_flows_into_evidence_from_study():
    """Guards against fields= drifting back to the flat v2 legacy names,
    which evidence_from_study cannot parse (it reads nested protocolSection.*)."""
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
        studies = await ctgov.fetch_jhu_industry_trials("Someone MD")
    items = [item for study_ in studies for item in evidence_from_study(study_, 2018, {"lymphoma"})]
    assert len(items) == 1 and items[0].kind == "trial_industry_collab"
