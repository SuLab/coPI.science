import pytest

from src.services.company_sources import pi_name
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


LAM = {"last": "Lamichhane", "fore": "Gyanu", "initials": "G", "collective": None}
DECK = {"last": "Deck", "fore": "Daniel H", "initials": "DH", "collective": None}
SERIO = {"last": "Serio", "fore": "Alisa W", "initials": "AW", "collective": None}
SMITH = {"last": "Smith", "fore": "John", "initials": "J", "collective": None}
GYANU = pi_name("Gyanu Lamichhane")


def coi(statement, authors=(LAM, SMITH), pmid="38980071", year=2024):
    return {"pmid": pmid, "year": year, "authors": list(authors), "coi_statement": statement}


def _coi_items(statement, authors=(LAM, SMITH), pi=GYANU):
    return evidence_from_record(coi(statement, authors), pi_year_ok=True, pi=pi)


def test_paratek_employees_are_not_the_pi():
    """Inverted (P4): Deck and Serio are named for Paratek; Lamichhane, the PI, is not, and
    "All authors vouch …" sits in the next sentence."""
    assert _coi_items(
        "Daniel H. Deck and Alisa W. Serio are employees of Paratek Pharmaceuticals, Inc. "
        "All authors vouch for the integrity.", authors=(DECK, SERIO, LAM)) == []


def test_all_authors_credits_the_pi():
    (item,) = _coi_items("All authors are employees of Acme Therapeutics.")
    assert item.company_name == "Acme Therapeutics" and item.evidence["relationship"] == "employee"
    assert item.evidence["attributed"] is True and item.evidence["pi_mention"] == "All authors"


def test_the_pi_initials_credit_the_pi():
    (item,) = _coi_items("G.L. is a consultant for Acme Therapeutics.")
    assert item.evidence["relationship"] == "consultant" and item.evidence["pi_mention"] == "G.L."
    assert item.external_id == "38980071:acme therapeutics" and item.year == 2024


def test_the_nearest_run_decides_each_company():
    items = _coi_items("G.L. is a consultant for Acme Therapeutics and J.S. is an employee of "
                       "Paratek Pharmaceuticals, Inc.")
    assert [(i.company_name, i.evidence["relationship"]) for i in items] == [("Acme Therapeutics", "consultant")]


def test_the_relationship_nearest_the_company_wins():
    items = _coi_items("G.L. is a co-founder of Acme Bio and a consultant for Beta Therapeutics.")
    assert [(i.company_name, i.evidence["relationship"]) for i in items] == [
        ("Acme Bio", "founder"), ("Beta Therapeutics", "consultant")]


def test_a_founder_is_a_founder():
    """A26: "is a founder of" was "other" (the pattern knew only co-founder and founded)."""
    (item,) = _coi_items("G.L. is a founder of Acme Bio.")
    assert item.evidence["relationship"] == "founder"


def test_a_negative_clause_credits_nothing():
    assert _coi_items("G.L. declares no competing interests with Acme Therapeutics.") == []


def test_a_shared_run_records_the_whole_run():
    (item,) = _coi_items("G.L. and J.S. received fees from Acme Therapeutics; "
                         "J.S. is an employee of Paratek Pharmaceuticals, Inc.")
    assert item.evidence["attribution"] == "G.L. and J.S." and item.evidence["pi_mention"] == "G.L."


@pytest.mark.parametrize("statement", [
    "G.L. received fees from Acme Inc; the remaining authors are employees of Beta Inc.",
    "G.L. is a consultant for Acme Inc; Johns Hopkins University owns equity in Beta Inc.",
])
def test_a_clause_with_its_own_subject_credits_nothing_to_the_pi(statement):
    (item,) = _coi_items(statement)
    assert item.company_name == "Acme Inc" and item.evidence["pi_mention"] == "G.L."


def test_a_subjectless_continuation_credits_the_pi():
    items = _coi_items("G.L. is a founder of Acme Inc; and holds equity in Beta Inc.")
    assert [(i.company_name, i.evidence["relationship"]) for i in items] == [
        ("Acme Inc", "founder"), ("Beta Inc", "equity")]


def test_a_possessive_after_the_pi_credits_nothing():
    lee = {"last": "Lee", "fore": "Grace", "initials": "G", "collective": None}
    authors, pi = (lee, SMITH), pi_name("Grace Lee")
    assert _coi_items("Dr. Lee's spouse is an employee of Acme Inc.", authors, pi) == []
    (item,) = _coi_items("Dr. Lee is an employee of Acme Inc.", authors, pi)
    assert item.evidence["pi_mention"] == "Dr. Lee"


def test_coi_negative_statement_yields_nothing():
    assert _coi_items("The authors declare no competing interests.") == []


def test_coi_positive_relationship_survives_trailing_boilerplate_negation():
    (item,) = _coi_items("J.S. is an employee of Paratek Pharmaceuticals, Inc. and has no other "
                         "competing interests.", pi=pi_name("John Smith"))
    assert item.evidence["relationship"] == "employee" and "Paratek" in item.company_name


def test_a_pi_not_on_the_record_or_unnamed_yields_nothing():
    assert _coi_items("All authors are employees of Acme Therapeutics.", authors=(SMITH,)) == []
    assert evidence_from_record(coi("All authors are employees of Acme Therapeutics."),
                                pi_year_ok=True, pi=None) == []


def test_matching_runs_on_the_normalised_text():
    (item,) = _coi_items("G.L. is a consultant for Acme Therapeutics.")
    assert item.company_name == "Acme Therapeutics"


def test_accents_are_kept_in_the_company_name():
    (item,) = _coi_items("Dr. Lamichhane holds equity in Société Générale Biosciences.")
    assert item.company_name == "Société Générale Biosciences"
    assert item.evidence["relationship"] == "equity"


@pytest.mark.parametrize("phrase,false_company", [
    ("Johns Hopkins Consortium on Aging", "Johns Hopkins Co"),
    ("Stanford Cooperative Group", "Stanford Co"),
    ("American Cancer Society, Colorado Chapter", "American Cancer Society, Co"),
    ("the NIH SAIL trial", "NIH SA"),
    ("Mount Sinai Corporate Education", "Mount Sinai Corp"),
])
def test_company_suffix_does_not_match_inside_longer_words(phrase, false_company):
    """Ported from the fix wave (0fb613c), with the PI named: " Co", " Corp" and " SA"
    matched inside Consortium/Cooperative/Colorado/Corporate/SAIL."""
    names = {i.company_name for i in _coi_items(f"G.L. consults for {phrase} and reports no other interests.")}
    assert false_company not in names
    assert not any(n.rstrip(".").endswith(("Co", "Corp", "SA")) for n in names), names


def test_real_company_with_trailing_period_still_matches():
    names = {i.company_name for i in _coi_items(
        "J.S. is an employee of Paratek Pharmaceuticals, Inc. All authors vouch for the data.",
        pi=pi_name("John Smith"))}
    assert names & {"Paratek Pharmaceuticals, Inc", "Paratek Pharmaceuticals"}, names


def test_empty_pi_orcid_never_matches_any_author():
    assert evidence_from_work(work(2021, ["I145311948"], [("I4210091798", "Paratek Pharmaceuticals (United States)")]), "", 2018, company_funder_ids=set()) == []
