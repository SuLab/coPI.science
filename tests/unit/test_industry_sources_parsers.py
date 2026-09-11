from src.services.industry_sources.companies import classify_company
from src.services.industry_sources.openalex_industry import evidence_from_work
from src.services.industry_sources.pubmed_coi import evidence_from_record

PI = "https://orcid.org/0000-0002-2214-0114"


def work(year, pi_inst_ids, companies, pi_raw=None, n_authors=3, pi_pos="last"):
    auths = [{"author": {"orcid": None}, "institutions": [{"id": f"https://openalex.org/{c}", "display_name": n, "type": "company"}],
              "author_position": "middle", "is_corresponding": False} for c, n in companies]
    auths.append({"author": {"orcid": PI}, "institutions": [{"id": f"https://openalex.org/{i}", "display_name": "JHU", "type": "education"} for i in pi_inst_ids],
                  "raw_affiliation_strings": pi_raw or [], "author_position": pi_pos, "is_corresponding": pi_pos == "last"})
    auths += [{"author": {"orcid": None}, "institutions": [], "author_position": "middle"}] * (n_authors - len(auths))
    return {"id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1"}, "publication_year": year, "authorships": auths, "funders": []}


def test_company_coauthor_in_tenure_with_jhu_pi_affiliation():
    items = evidence_from_work(work(2021, ["I145311948"], [("I4210091798", "Paratek Pharmaceuticals (United States)")]), PI, 2018, company_funder_ids=set())
    assert len(items) == 1 and items[0].kind == "coauthor_company" and items[0].in_tenure and items[0].pi_role == "corresponding"


def test_pre_tenure_year_yields_nothing():
    assert evidence_from_work(work(2015, ["I145311948"], [("I1", "Pfizer")]), PI, 2018, company_funder_ids=set()) == []


def test_in_window_year_but_pi_not_at_jhu_yields_nothing():
    assert evidence_from_work(work(2020, ["I999"], [("I1", "Pfizer")]), PI, 2018, company_funder_ids=set()) == []


def test_raw_affiliation_string_fallback_counts_as_jhu():
    items = evidence_from_work(work(2020, [], [("I1", "Pfizer")], pi_raw=["Dept of Medicine, Johns Hopkins University School of Medicine"]), PI, 2018, company_funder_ids=set())
    assert len(items) == 1


def test_consortium_paper_is_downweighted_in_evidence_payload():
    items = evidence_from_work(work(2020, ["I145311948"], [("I1", "Pfizer")], n_authors=60, pi_pos="middle"), PI, 2018, company_funder_ids=set())
    assert items[0].evidence["author_count"] == 60 and items[0].pi_role == "middle"


def test_vendor_classified_as_cro_vendor():
    assert classify_company("Applied BioPhysics (United States)", "company") == "cro_vendor"
    assert classify_company("Paratek Pharmaceuticals (United States)", "company") == "pharma_biotech"
    assert classify_company("Medtronic", "company") == "device_dx"
    assert classify_company("Something Ltd", "company") == "other"


def test_coi_positive_statement_extracts_company_and_relationship():
    rec = {"pmid": "38980071", "year": 2024, "coi_statement": "Daniel H. Deck and Alisa W. Serio are employees of Paratek Pharmaceuticals, Inc. All authors vouch for the integrity.",
           "affiliations": ["Paratek Pharmaceuticals Inc., King of Prussia, Pennsylvania, USA."]}
    items = evidence_from_record(rec, pi_year_ok=True)
    kinds = {i.kind for i in items}
    assert "coi_relationship" in kinds
    coi = next(i for i in items if i.kind == "coi_relationship")
    assert "Paratek" in coi.company_name and coi.evidence["relationship"] == "employee" and coi.evidence["span"].startswith("Daniel")


def test_coi_negative_statement_yields_nothing():
    assert evidence_from_record({"pmid": "1", "year": 2024, "coi_statement": "The authors declare no competing interests.", "affiliations": []}, pi_year_ok=True) == []


def test_coi_positive_relationship_survives_trailing_boilerplate_negation():
    rec = {"pmid": "2", "year": 2024,
           "coi_statement": "J.S. is an employee of Paratek Pharmaceuticals, Inc. and has no other competing interests."}
    items = evidence_from_record(rec, pi_year_ok=True)
    coi = next(i for i in items if i.kind == "coi_relationship")
    assert coi.evidence["relationship"] == "employee" and "Paratek" in coi.company_name


def test_empty_pi_orcid_never_matches_any_author():
    assert evidence_from_work(work(2021, ["I145311948"], [("I4210091798", "Paratek Pharmaceuticals (United States)")]), "", 2018, company_funder_ids=set()) == []
