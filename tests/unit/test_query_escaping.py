"""Per-destination query escaping (spec 2026-10-05 §4.1, P29): one hostile-name test per
builder, and the allowlist itself."""
import json

import httpx
import respx

from src.services import odp
from src.services.corpus import build_pubmed_query
from src.services.industry_sources import ctgov, openalex_industry, uspto_inventor
from src.services.query_escaping import openalex_filter_value, phrase_term, pubmed_author_term

HOSTILE = 'Jane" OR applicationMetaData.applicantBag.applicantNameText:"Pfizer\\ (x) [y] {z}\x00\n'


def test_phrase_term_keeps_names_and_drops_syntax():
    assert phrase_term("José García-Márquez O’Neil, Jr.") == "José García-Márquez O’Neil, Jr."
    assert phrase_term(HOSTILE) == "Jane OR applicationMetaData.applicantBag.applicantNameText Pfizer x y z"
    assert phrase_term("​ ") == ""


def test_pubmed_author_term_drops_operators_and_commas():
    assert pubmed_author_term("Smith AND Jones[Author] OR x, y NOT") == "Smith Jones Author x y"
    assert pubmed_author_term("Anne Ornstein") == "Anne Ornstein"   # operators only as whole words


def test_openalex_filter_value_keeps_ids_only():
    assert openalex_filter_value("0000-0002-1825-009X") == "0000-0002-1825-009X"
    assert openalex_filter_value("123|456,author.id:A5!") == "123456authoridA5"


def test_the_uspto_query_has_exactly_its_own_quotes():
    q = uspto_inventor.inventor_query(HOSTILE)
    assert q.count('"') == 4 and "\\" not in q and "\n" not in q
    assert q.startswith('applicationMetaData.firstInventorName:"Jane OR applicationMetaData')


def test_the_uspto_query_uses_the_parsed_name_and_refuses_an_orcid_id():
    assert uspto_inventor.inventor_query("Dr. Jane Smith, PhD").startswith(
        'applicationMetaData.firstInventorName:"Jane Smith" AND')
    assert uspto_inventor.inventor_query("0000-0002-1825-0097") is None


def test_the_ctgov_term_has_exactly_its_own_quotes():
    term = ctgov.official_term('Jane" AND AREA[LeadSponsorName]"Pfizer')
    assert term.count('"') == 2 and term.count("AREA[") == 2
    assert term.endswith('" AND AREA[CollaboratorClass]INDUSTRY')
    assert ctgov.official_term("0000-0002-1825-0097") is None


def test_the_pubmed_query_cannot_add_a_clause():
    q = build_pubmed_query('Jane Smith[Affiliation] OR "x', ['Johns "Hopkins"[ad] OR y'])
    assert q.count("[Author]") == 2 and q.count("[Affiliation]") == 1 and q.count('"') == 2
    assert build_pubmed_query("Jeffrey Rothstein", ["Johns Hopkins University"]) == (
        '((Rothstein Jeffrey[Author]) OR (Rothstein J[Author])) AND ("Johns Hopkins University"[Affiliation])')


@respx.mock
async def test_the_openalex_works_filter_cannot_add_a_filter():
    route = respx.get(f"{openalex_industry.OA}/works").mock(
        return_value=httpx.Response(200, json={"results": []}))
    await openalex_industry.fetch_works_for_pmids(["123", "4|5,author.id:A1", ""])
    sent = route.calls[0].request.url.params["filter"]
    assert sent == "pmid:123|45authoridA1"


@respx.mock
async def test_the_openalex_orcid_filter_cannot_add_a_filter():
    from src.services import openalex
    route = respx.get(openalex.OPENALEX_WORKS_URL).mock(
        return_value=httpx.Response(200, json={"results": [], "meta": {}}))
    await openalex.fetch_works_by_orcid("0000-0001-0000-0003,is_oa:true")
    assert route.calls[0].request.url.params["filter"] == (
        "author.orcid:https://orcid.org/0000-0001-0000-0003isoatrue")


@respx.mock
async def test_the_uspto_request_body_carries_the_escaped_query(monkeypatch):
    monkeypatch.setattr(odp, "ODP_PACE_INTERVAL", 0.0)
    odp.ODP_PACER.reset()
    monkeypatch.setattr(uspto_inventor, "get_settings", lambda: type("S", (), {"uspto_api_key": "k"})())
    route = respx.post(odp.ODP_SEARCH_URL).mock(return_value=httpx.Response(404))
    await uspto_inventor.fetch_jhu_applications(HOSTILE)
    assert json.loads(route.calls[0].request.content)["q"].count('"') == 4
