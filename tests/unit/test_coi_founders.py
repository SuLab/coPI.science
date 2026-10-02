"""The COI gate helpers (spec §7.5 Step 1, O14): locating the PI, the initials-collision
skip and the "found" gate. Attribution itself is Claude's (`coi_llm`); the regex
suite's (statement, authors, PI, expected) cases moved to the live eval case set
`tests/fixtures/company_discovery/coi_eval_cases.json`.

The `pubmed_*.xml` fixtures are real PubMed records from E-utilities efetch, fetched
2026-10-02, cut down to PMID, PubDate, ArticleTitle, AuthorList and CoiStatement (copied
byte for byte; PMID 39433569 keeps its full AuthorList, the others drop AffiliationInfo
and Identifier from each Author).
"""
from pathlib import Path

import pytest

from src.services.company_sources import pi_name
from src.services.company_sources.coi_founders import locate_pi, mentions_founding
from src.services.pubmed import _parse_pubmed_xml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "company_discovery"

VELCULESCU = {"last": "Velculescu", "fore": "Victor E", "initials": "VE", "collective": None}
LEAL = {"last": "Leal", "fore": "Alessandro", "initials": "A", "collective": None}
VOGELSTEIN = {"last": "Vogelstein", "fore": "Bert", "initials": "B", "collective": None}
JING_WANG = {"last": "Wang", "fore": "Jing", "initials": "J", "collective": None}


def _record(pmid: str) -> dict:
    (rec,) = _parse_pubmed_xml((FIXTURES / f"pubmed_{pmid}.xml").read_text(encoding="utf-8"))
    return rec


def _synthetic(authors=(VELCULESCU, LEAL), coi: str = "V.E.V. is a founder of Acme Bio.") -> dict:
    return {"pmid": "1", "year": 2020, "authors": list(authors), "coi_statement": coi}


# --- locate_pi on real records --------------------------------------------------


def test_velculescu_is_located_with_his_initials_forms():
    forms = locate_pi(_record("39433569"), pi_name("Victor Velculescu"))
    assert forms is not None
    assert {"VEV", "VV"} <= forms.letters
    assert forms.surname == "Velculescu" and forms.first_initial == "v"
    assert not forms.surname_shared
    authors = [a for a in _record("39433569")["authors"] if isinstance(a, dict)]
    assert authors[forms.position - 1]["last"] == "Velculescu"


def test_a_middle_initial_in_the_users_name_changes_nothing():
    rec = _record("39433569")
    assert locate_pi(rec, pi_name("Victor E. Velculescu")) == locate_pi(rec, pi_name("Victor Velculescu"))


def test_a_first_author_with_a_particle_is_located():
    forms = locate_pi(_record("39433569"), pi_name("Iris van 't Erve"))
    assert forms is not None and "IV" in forms.letters and forms.position == 1


def test_an_initials_collision_skips_the_record():
    """Blair C and Bettegowda C both reduce to "CB" on 37552989; on 41115959 there is no
    other "CB", so Bettegowda is located there."""
    assert locate_pi(_record("37552989"), pi_name("Chetan Bettegowda")) is None
    assert locate_pi(_record("37552989"), pi_name("Cherie Blair")) is None
    assert locate_pi(_record("41115959"), pi_name("Chetan Bettegowda")) is not None
    assert locate_pi(_record("37552989"), pi_name("Christopher Douville")) is not None


def test_a_collective_author_is_never_the_pi():
    rec = _record("41115959")
    assert any(a.get("collective") for a in rec["authors"])
    assert locate_pi(rec, pi_name("Peter Gibbs")) is not None


# --- locate_pi on synthetic author lists ------------------------------------------


@pytest.mark.parametrize("other", [
    {"last": "Vogel", "fore": "Val E", "initials": "VE", "collective": None},  # VEV
    {"last": "Vasquez", "fore": "Victor", "initials": "V", "collective": None},  # VV
    {"last": "Velculescu", "fore": "Victor", "initials": "V", "collective": None},  # twice
])
def test_a_synthetic_collision_skips_the_record(other):
    assert locate_pi(_synthetic((VELCULESCU, LEAL, other)), pi_name("Victor Velculescu")) is None


def test_a_different_given_name_is_not_the_pi():
    vlad = {"last": "Velculescu", "fore": "Vlad", "initials": "V", "collective": None}
    assert locate_pi(_synthetic((vlad, LEAL)), pi_name("Victor Velculescu")) is None


def test_no_matching_author_is_not_located():
    assert locate_pi(_synthetic((LEAL,)), pi_name("Victor Velculescu")) is None


@pytest.mark.parametrize(("who", "author", "letters"), [
    ("Hans Mueller", {"last": "Müller", "fore": "Hans", "initials": "H"}, "HM"),
    ("Hans Muller", {"last": "Müller", "fore": "Hans", "initials": "H"}, "HM"),
    ("Anne Hamacher-Brady", {"last": "Hamacher-Brady", "fore": "Anne", "initials": "A"}, "AHB"),
    ("Anne Hamacher Brady", {"last": "Hamacher-Brady", "fore": "Anne", "initials": "A"}, "AHB"),
    ("Iris van 't Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"}, "IV"),
    ("Iris van 't Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"}, "IVTE"),
])
def test_diacritics_hyphens_and_particles_are_located(who, author, letters):
    forms = locate_pi(_synthetic(({**author, "collective": None}, LEAL)), pi_name(who))
    assert forms is not None and letters in forms.letters


@pytest.mark.parametrize(("who", "author"), [
    ("Heinz Müller", {"last": "Müller", "fore": "Hans", "initials": "H"}),
    ("Iris Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"}),
])
def test_a_different_given_name_or_surname_is_not_located(who, author):
    assert locate_pi(_synthetic(({**author, "collective": None}, LEAL)), pi_name(who)) is None


def test_a_particle_stranded_in_the_forename_is_spliced_back():
    author = {"last": "Neal", "fore": "Anya J O'", "initials": "AJ", "collective": None}
    forms = locate_pi(_synthetic((author, LEAL)), pi_name("Anya O'Neal"))
    assert forms is not None and forms.surname == "O'Neal"


@pytest.mark.parametrize(("other", "shared"), [
    ({"last": "Wang", "fore": "Xiao J", "initials": "XJ", "collective": None}, True),
    ({"last": "Wong", "fore": "Xiao J", "initials": "XJ", "collective": None}, False),
])
def test_a_shared_surname_is_flagged(other, shared):
    forms = locate_pi(_synthetic((JING_WANG, other, LEAL)), pi_name("Jing Wang"))
    assert forms is not None and forms.surname_shared is shared


# --- the "found" gate -------------------------------------------------------------


@pytest.mark.parametrize(("statement", "expected"), [
    ("V.E.V. is a founder of Acme Bio.", True),
    ("V.E.V. CO-FOUNDED Acme Bio.", True),
    ("BV Founder of Thrive Earlier Detection", True),
    ("V.E.V. is a cofounder of Acme Bio.", True),
    ("The authors found no conflicts.", True),  # over-inclusive by design
    ("V.E.V. is an advisor to Acme Bio.", False),
    ("The authors declare no competing interests.", False),
    ("", False),
])
def test_mentions_founding(statement, expected):
    assert mentions_founding(statement) is expected


# --- pi_name -------------------------------------------------------------------


@pytest.mark.parametrize(("full", "first", "surname"), [
    ("Victor E. Velculescu", "Victor", "Velculescu"),
    ("Dr. Victor Velculescu", "Victor", "Velculescu"),
    ("Iris van 't Erve", "Iris", "van 't Erve"),
    ("Anne Hamacher-Brady", "Anne", "Hamacher-Brady"),
])
def test_pi_name_splits_given_name_and_surname(full, first, surname):
    name = pi_name(full)
    assert name is not None and name.first == first and name.surname == surname


def test_pi_name_needs_two_tokens():
    assert pi_name("Cher") is None
    assert pi_name("") is None
