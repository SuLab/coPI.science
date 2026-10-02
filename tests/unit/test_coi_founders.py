"""COI founder attribution (spec §7.5 Step 1, §8, F13, Review Focus #2).

The `pubmed_*.xml` fixtures are real PubMed records from E-utilities efetch, fetched
2026-10-02, cut down to PMID, PubDate, ArticleTitle, AuthorList and CoiStatement (copied
byte for byte; PMID 39433569 keeps its full AuthorList, the others drop AffiliationInfo
and Identifier from each Author). The F13
"OrisDx and Belay Diagnostics" sentences are Chetan Bettegowda's (C.B.), not Bert
Vogelstein's: attribution must give them to Bettegowda and never to Vogelstein.
"""
from pathlib import Path

import pytest

from src.services.company_sources import pi_name
from src.services.company_sources.coi_founders import (
    clauses,
    founder_claims,
    locate_pi,
    sentences,
)
from src.services.pubmed import _parse_pubmed_xml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "company_discovery"
ALL_PMIDS = ("39433569", "34290408", "37552989", "41115959", "39960487")
COAUTHOR_TIES = {"OrisDx", "Belay Diagnostics", "IMVAQ Therapeutics", "Egret Therapeutics"}

VELCULESCU = {"last": "Velculescu", "fore": "Victor E", "initials": "VE", "collective": None}
LEAL = {"last": "Leal", "fore": "Alessandro", "initials": "A", "collective": None}
VOGELSTEIN = {"last": "Vogelstein", "fore": "Bert", "initials": "B", "collective": None}
BETTEGOWDA = {"last": "Bettegowda", "fore": "Chetan", "initials": "C", "collective": None}
PAPADOPOULOS = {"last": "Papadopoulos", "fore": "Nickolas", "initials": "N", "collective": None}
KINZLER = {"last": "Kinzler", "fore": "Kenneth W", "initials": "KW", "collective": None}


def _record(pmid: str) -> dict:
    (rec,) = _parse_pubmed_xml((FIXTURES / f"pubmed_{pmid}.xml").read_text(encoding="utf-8"))
    return rec


def _claims(rec: dict, who: str) -> list[tuple[str, str]]:
    return [(c.company_name, c.pi_role) for c in founder_claims(rec, pi_name(who))]


def _synthetic(coi: str, authors=(VELCULESCU, LEAL)) -> dict:
    return {"pmid": "1", "year": 2020, "authors": list(authors), "coi_statement": coi}


# --- real records ------------------------------------------------------------


def test_velculescu_2024_yields_exactly_delfi_as_founder():
    claims = founder_claims(_record("39433569"), pi_name("Victor Velculescu"))
    assert [(c.company_name, c.pi_role) for c in claims] == [("DELFI Diagnostics", "founder")]
    (claim,) = claims
    assert claim.pmid == "39433569" and claim.year == 2024 and claim.former is False
    assert claim.sentence.startswith("V.E.V. is a founder of DELFI Diagnostics, serves on the Board")


def test_a_middle_initial_in_the_users_name_changes_nothing():
    assert _claims(_record("39433569"), "Victor E. Velculescu") == [("DELFI Diagnostics", "founder")]


def test_coauthors_in_a_subject_list_get_their_own_tie_not_the_pis():
    rec = _record("39433569")
    assert _claims(rec, "Robert Scharpf") == [("DELFI Diagnostics", "founder")]
    assert _claims(rec, "Alessandro Leal") == [("DELFI Diagnostics", "founder")]


def test_a_first_author_with_a_particle_is_located_but_has_no_tie():
    rec = _record("39433569")
    forms = locate_pi(rec, pi_name("Iris van 't Erve"))
    assert forms is not None and "IV" in forms.letters
    assert _claims(rec, "Iris van 't Erve") == []


def test_velculescu_2021_names_delfi_and_pgdx():
    assert _claims(_record("34290408"), "Victor Velculescu") == [
        ("Delfi Diagnostics", "founder"),
        ("Personal Genome Diagnostics", "founder"),
    ]


def test_the_cb_sentences_yield_orisdx_and_belay_for_bettegowda():
    assert _claims(_record("41115959"), "Chetan Bettegowda") == [
        ("OrisDx", "co_founder"),
        ("Belay Diagnostics", "co_founder"),
    ]


def test_vogelstein_gets_only_his_own_ties():
    assert _claims(_record("41115959"), "Bert Vogelstein") == [
        ("Exact Sciences", "founder"),
        ("Clasp Therapeutics", "founder"),
        ("Haystack Oncology", "founder"),
    ]
    assert _claims(_record("34290408"), "Bert Vogelstein") == [
        ("Thrive Earlier Detection", "founder"),
        ("manaT Bio", "founder"),
        ("Personal Genome Diagnostics", "founder"),
    ]
    assert _claims(_record("37552989"), "Bert Vogelstein") == [
        ("Thrive Earlier Detection", "founder"),
        ("ManaT Bio", "founder"),
    ]


@pytest.mark.parametrize("pmid", ALL_PMIDS)
def test_coauthor_ties_never_reach_vogelstein(pmid):
    names = {name for name, _ in _claims(_record(pmid), "Bert Vogelstein")}
    assert not names & COAUTHOR_TIES


def test_the_f13_elife_sentence_is_attributed_to_nobody():
    """39960487 is where F13's "Co-founder of OrisDx and Belay Diagnostics" comes from:
    an eLife block ("CB Consultant to ... Co-founder of OrisDx ...") whose continuation
    sentence has no subject, so it names no one. The block's opening entry "BV Founder of
    Thrive Earlier Detection" does name its owner."""
    rec = _record("39960487")
    assert "Co-founder of OrisDx and Belay Diagnostics" in rec["coi_statement"]
    assert _claims(rec, "Bert Vogelstein") == [("Thrive Earlier Detection", "founder")]
    assert _claims(rec, "Chetan Bettegowda") == []


def test_tm_and_cj_sentences_yield_nothing():
    rec_tm = _record("34290408")
    assert "T.M. is a cofounder and holds equity in IMVAQ Therapeutics" in rec_tm["coi_statement"]
    assert _claims(rec_tm, "Taha Merghoub") == []
    rec_cj = _record("37552989")
    assert "C.J. is a co-founder with equity interest in Egret Therapeutics" in rec_cj["coi_statement"]
    assert _claims(rec_cj, "Christopher Jackson") == []


def test_an_initials_collision_skips_the_record():
    """Blair C and Bettegowda C both reduce to "CB" on 37552989."""
    rec = _record("37552989")
    assert locate_pi(rec, pi_name("Chetan Bettegowda")) is None
    assert _claims(rec, "Chetan Bettegowda") == []
    assert _claims(rec, "Christopher Douville") == [("Belay Diagnostics", "co_founder")]


# --- synthetic statements ----------------------------------------------------


@pytest.mark.parametrize("statement", [
    "V.E.V. is a founder of Acme Bio.",
    "V. E. V. is a founder of Acme Bio.",
    "VEV is a founder of Acme Bio.",
    "VV is a founder of Acme Bio.",
    "V.V. is a founder of Acme Bio.",
    "A.L., S.C., and V.E.V. are founders of Acme Bio.",
    "Victor Velculescu is a founder of Acme Bio.",
    "Victor E. Velculescu is a founder of Acme Bio.",
    "V. Velculescu is a founder of Acme Bio.",
    "Dr. Velculescu is a founder of Acme Bio.",
    "Professor Velculescu is a founder of Acme Bio.",
])
def test_every_pi_form_in_the_subject_counts(statement):
    assert _claims(_synthetic(statement), "Victor Velculescu") == [("Acme Bio", "founder")]


@pytest.mark.parametrize(("statement", "expected"), [
    ("V.E.V. is a co-founder of Acme Bio.", [("Acme Bio", "co_founder")]),
    ("V.E.V. is a cofounder of Acme Bio.", [("Acme Bio", "co_founder")]),
    ("V.E.V. co-founded Acme Bio in 2015.", [("Acme Bio", "co_founder")]),
    ("V.E.V. founded Acme Bio and Beta Therapeutics in 2012.",
     [("Acme Bio", "founder"), ("Beta Therapeutics", "founder")]),
    ("V.E.V. is a founder of and owns equity in DELFI Diagnostics, Inc.",
     [("DELFI Diagnostics", "founder")]),
    ("V.E.V. is a founder of 23andMe and of the company.", [("23andMe", "founder")]),
    ("V.E.V. is an advisor to Viron Therapeutics, and A.L. is a founder of Acme Bio.", []),
    ("Acme Bio, a company founded by V.E.V., licensed the technology.", []),
    ("The authors declare no competing interests.", []),
])
def test_extraction(statement, expected):
    assert _claims(_synthetic(statement), "Victor Velculescu") == expected


@pytest.mark.parametrize("statement", [
    "V.E.V. was a co-founder of Personal Genome Diagnostics, and divested his equity in "
    "Personal Genome Diagnostics (PGDx) to LabCorp in February 2022.",
    "V.E.V. is a former founder of Personal Genome Diagnostics.",
    "V.E.V. was previously a founder of Personal Genome Diagnostics.",
    "V.E.V. is no longer a founder of Personal Genome Diagnostics.",
    "V.E.V. co-founded Personal Genome Diagnostics and sold his shares.",
])
def test_former_wording_sets_the_flag_and_keeps_the_claim(statement):
    (claim,) = founder_claims(_synthetic(statement), pi_name("Victor Velculescu"))
    assert claim.company_name == "Personal Genome Diagnostics" and claim.former is True


@pytest.mark.parametrize("other", [
    {"last": "Vogel", "fore": "Val E", "initials": "VE", "collective": None},  # VEV
    {"last": "Vasquez", "fore": "Victor", "initials": "V", "collective": None},  # VV
    {"last": "Velculescu", "fore": "Victor", "initials": "V", "collective": None},  # twice
])
def test_a_synthetic_collision_skips_the_record(other):
    rec = _synthetic("V.E.V. is a founder of DELFI Diagnostics.", authors=(VELCULESCU, LEAL, other))
    assert _claims(rec, "Victor Velculescu") == []


def test_a_different_given_name_is_not_the_pi():
    vlad = {"last": "Velculescu", "fore": "Vlad", "initials": "V", "collective": None}
    rec = _synthetic("V.V. is a founder of DELFI Diagnostics.", authors=(vlad, LEAL))
    assert _claims(rec, "Victor Velculescu") == []


def test_no_matching_author_or_no_statement_yields_nothing():
    assert _claims(_synthetic("V.E.V. is a founder of Acme Bio.", authors=(LEAL,)), "Victor Velculescu") == []
    assert _claims(_synthetic(""), "Victor Velculescu") == []


@pytest.mark.parametrize(("who", "author", "statement", "expected"), [
    ("Hans Mueller", {"last": "Müller", "fore": "Hans", "initials": "H"},
     "H.M. is a founder of Acme Bio.", [("Acme Bio", "founder")]),
    ("Hans Muller", {"last": "Müller", "fore": "Hans", "initials": "H"},
     "H.M. is a founder of Acme Bio.", [("Acme Bio", "founder")]),
    ("Heinz Müller", {"last": "Müller", "fore": "Hans", "initials": "H"},
     "H.M. is a founder of Acme Bio.", []),
    ("Anne Hamacher-Brady", {"last": "Hamacher-Brady", "fore": "Anne", "initials": "A"},
     "A.H.-B. is a co-founder of Acme Bio.", [("Acme Bio", "co_founder")]),
    ("Anne Hamacher Brady", {"last": "Hamacher-Brady", "fore": "Anne", "initials": "A"},
     "A.H.-B. is a co-founder of Acme Bio.", [("Acme Bio", "co_founder")]),
    ("Iris van 't Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"},
     "Iris van 't Erve is a founder of Acme Bio.", [("Acme Bio", "founder")]),
    ("Iris van 't Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"},
     "Dr. van 't Erve is a founder of Acme Bio.", [("Acme Bio", "founder")]),
    ("Iris van 't Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"},
     "I.V. is a founder of Acme Bio.", [("Acme Bio", "founder")]),
    ("Iris Erve", {"last": "van 't Erve", "fore": "Iris", "initials": "I"},
     "I.E. is a founder of Acme Bio.", []),
])
def test_diacritics_hyphens_and_particles(who, author, statement, expected):
    rec = _synthetic(statement, authors=({**author, "collective": None}, LEAL))
    assert _claims(rec, who) == expected


# --- audit findings: subject, clauses, lexical leaks ------------------------


ELIFE_FIRST_ENTRY = ("AC, LD, EW, BP, BL, SP, EH, MP, KG, NW No competing interests declared, "
                     "SS Co-founder of Acme Therapeutics.")


@pytest.mark.parametrize(("who", "expected"), [
    ("Laura Dobbyn", []),
    ("Surojit Sur", [("Acme Therapeutics", "co_founder")]),
])
def test_an_elife_entry_credits_only_the_initials_that_open_it(who, expected):
    rec = {**_record("39960487"), "coi_statement": ELIFE_FIRST_ENTRY}
    assert _claims(rec, who) == expected


@pytest.mark.parametrize(("authors", "who", "statement", "expected"), [
    # the subject is the person list right before the phrase, not all text before it
    ((VELCULESCU, LEAL), "Alessandro Leal",
     "A.L. advises Viron Therapeutics, and V.E.V. is a founder of Acme Bio.", []),
    # a new clause starts at every person list after a delimiter, for every verb
    ((VOGELSTEIN, BETTEGOWDA), "Bert Vogelstein",
     "B.V. is a founder of Thrive Earlier Detection, and Chetan Bettegowda is a co-founder of OrisDx.",
     [("Thrive Earlier Detection", "founder")]),
    ((VOGELSTEIN, BETTEGOWDA), "Chetan Bettegowda",
     "B.V. is a founder of Thrive Earlier Detection, and Chetan Bettegowda is a co-founder of OrisDx.",
     [("OrisDx", "co_founder")]),
    ((VOGELSTEIN, PAPADOPOULOS), "Bert Vogelstein",
     "Bert Vogelstein is a consultant to Sysmex, and Nickolas Papadopoulos is a founder of "
     "Thrive Earlier Detection.", []),
    ((VOGELSTEIN, KINZLER), "Bert Vogelstein",
     "B.V. is a consultant to Sysmex and K.W. Kinzler is a founder of Thrive Earlier Detection.", []),
    ((VOGELSTEIN, PAPADOPOULOS), "Bert Vogelstein",
     "B.V. is a consultant to Sysmex and N.P. co-founded Thrive Earlier Detection.", []),
    ((VELCULESCU, LEAL), "Alessandro Leal",
     "A.L. is a consultant to Viron Therapeutics, and V.E.V. co-founded Acme Bio.", []),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is an advisor to Viron Therapeutics, while A.L. is a founder of Acme Bio.", []),
    # a declaration preamble ends at ":" or "that"
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "The authors declare the following competing interests: V.E.V. is a founder of Delfi Diagnostics.",
     [("Delfi Diagnostics", "founder")]),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "The authors report that V.E.V. is a founder of Delfi Diagnostics.",
     [("Delfi Diagnostics", "founder")]),
    # a coordinated founder phrase never jumps to another company
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. co-founded and serves on the board of DELFI Diagnostics, and owns equity in Exact Sciences.",
     [("DELFI Diagnostics", "co_founder")]),
    # negation, passive, "respectively" and title conjuncts
    ((VELCULESCU, LEAL), "Victor Velculescu", "V.E.V. is not a founder of Acme Bio.", []),
    ((VELCULESCU, LEAL), "Victor Velculescu", "V.E.V. has never been a founder of Acme Bio.", []),
    ((VELCULESCU, LEAL), "Alessandro Leal", "Neither V.E.V. nor A.L. is a founder of Acme Bio.", []),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is an advisor to Viron Therapeutics, which was co-founded by Johns Hopkins University "
     "and Acme Ventures.", []),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is an advisor to Viron Therapeutics, which was cofounded by Acme Ventures.", []),
    ((VELCULESCU, VOGELSTEIN), "Victor Velculescu",
     "B.V. and V.E.V. are founders of Thrive Earlier Detection and DELFI Diagnostics, respectively.", []),
    ((VELCULESCU, VOGELSTEIN), "Bert Vogelstein",
     "B.V. and V.E.V. are founders of Thrive Earlier Detection and DELFI Diagnostics, respectively.", []),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is a co-founder of Acme Bio and CEO of Beta Inc.", [("Acme Bio", "co_founder")]),
    # serial lists and a coordinated title
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is a founder of DELFI Diagnostics, Personal Genome Diagnostics, and Acme Bio.",
     [("DELFI Diagnostics", "founder"), ("Personal Genome Diagnostics", "founder"), ("Acme Bio", "founder")]),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is a founder of DELFI Diagnostics, Personal Genome Diagnostics and Acme Bio.",
     [("DELFI Diagnostics", "founder"), ("Personal Genome Diagnostics", "founder"), ("Acme Bio", "founder")]),
    ((VELCULESCU, LEAL), "Victor Velculescu",
     "V.E.V. is a co-founder and director of Acme Bio.", [("Acme Bio", "co_founder")]),
])
def test_audit_attribution_cases(authors, who, statement, expected):
    assert _claims(_synthetic(statement, authors=authors), who) == expected


JING_WANG = {"last": "Wang", "fore": "Jing", "initials": "J", "collective": None}


@pytest.mark.parametrize(("other", "statement", "expected"), [
    ({"last": "Wang", "fore": "Xiao J", "initials": "XJ", "collective": None},
     "X.J. Wang is a co-founder of Acme Bio.", []),
    ({"last": "Wang", "fore": "Xiao-Jing", "initials": "XJ", "collective": None},
     "Xiao-Jing Wang is a co-founder of Acme Bio.", []),
    ({"last": "Wang", "fore": "Xiao J", "initials": "XJ", "collective": None},
     "J.W. is a co-founder of Acme Bio.", [("Acme Bio", "co_founder")]),
])
def test_a_shared_surname_leaves_only_the_initials_forms(other, statement, expected):
    rec = _synthetic(statement, authors=(JING_WANG, other, LEAL))
    assert _claims(rec, "Jing Wang") == expected


def test_a_shared_surname_drops_the_honorific_form():
    j_vogelstein = {"last": "Vogelstein", "fore": "Joshua", "initials": "J", "collective": None}
    rec = _synthetic("Dr. Vogelstein is a founder of Thrive Earlier Detection.",
                     authors=(VOGELSTEIN, j_vogelstein))
    assert _claims(rec, "Bert Vogelstein") == []


# --- helpers -----------------------------------------------------------------


def test_sentences_keep_initials_and_corporate_abbreviations_whole():
    text = ("A.L., S.C., R.B.S., and V.E.V. are inventors. K.L, L.K.M. own stock; in addition, "
            "R.J.A.F. has patents. The rest declare none. Paratek Pharmaceuticals, Inc. All good.")
    assert sentences(text) == [
        "A.L., S.C., R.B.S., and V.E.V. are inventors.",
        "K.L, L.K.M. own stock;",
        "in addition, R.J.A.F. has patents.",
        "The rest declare none.",
        "Paratek Pharmaceuticals, Inc. All good.",
    ]


def test_clauses_cut_where_a_new_person_subject_starts():
    sentence = "A.L. and R.B.S. are founders of DELFI Diagnostics, and R.B.S. is a consultant."
    assert [subject.strip(" ,") for subject, _ in clauses(sentence)] == ["A.L. and R.B.S.", "and R.B.S."]


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
