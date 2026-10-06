"""coi_attribution (spec 2026-10-05 §6.2, P4, D10): the COI rule `Statement.credit` and
the length-preserving `accent_free`. The helpers moved here from coi_llm keep
their own tests in tests/unit/test_coi_llm.py."""
import pytest

from src.services.coi_attribution import ALL_AUTHORS, Statement, accent_free, norm
from src.services.company_sources import pi_name
from src.services.company_sources.coi_founders import locate_pi

LAM = {"last": "Lamichhane", "fore": "Gyanu", "initials": "G", "collective": None}
SMITH = {"last": "Smith", "fore": "John", "initials": "J", "collective": None}


def _credits(statement: str, company: str, authors=(LAM, SMITH), pi="Gyanu Lamichhane"):
    record = {"authors": list(authors), "coi_statement": statement}
    forms = locate_pi(record, pi_name(pi))
    assert forms is not None
    prepared = Statement.of(statement, forms, list(authors), count_all_authors=True)
    text = accent_free(norm(statement))
    at = text.index(company)
    credit = prepared.credit(text, (at, at + len(company)))
    return None if credit is None else (text[credit[0]:credit[1]], text[credit[2]:credit[3]])


def test_credit_reads_the_run_nearest_before_the_company():
    s = "G.L. is a consultant for Acme Therapeutics and J.S. is an employee of Paratek Pharmaceuticals."
    assert _credits(s, "Acme Therapeutics") == ("G.L.", "G.L.")
    assert _credits(s, "Paratek Pharmaceuticals") is None


def test_a_shared_run_credits_every_name_in_it():
    s = "G.L. and J.S. received fees from Acme Therapeutics; J.S. is an employee of Paratek Pharmaceuticals."
    assert _credits(s, "Acme Therapeutics") == ("G.L. and J.S.", "G.L.")
    assert _credits(s, "Paratek Pharmaceuticals") is None


def test_a_subjectless_clause_continues_the_one_before_it():
    s = "J.S. reports fees from Acme Therapeutics; and G.L. is a co-founder of Beta Bio."
    assert _credits(s, "Acme Therapeutics") is None
    assert _credits(s, "Beta Bio") == ("G.L.", "G.L.")


@pytest.mark.parametrize("subject", ["All authors are", "Each author is", "All of the authors are"])
def test_all_or_each_author_names_the_pi(subject):
    assert _credits(f"{subject} employees of Acme Therapeutics.", "Acme Therapeutics")[1] == subject.rsplit(" ", 1)[0]


def test_the_founder_rule_does_not_count_all_authors():
    record = {"authors": [LAM, SMITH], "coi_statement": "All authors founded Acme Bio."}
    forms = locate_pi(record, pi_name("Gyanu Lamichhane"))
    text = accent_free(norm(record["coi_statement"]))
    at = text.index("Acme Bio")
    assert Statement.of(record["coi_statement"], forms, [LAM, SMITH]).credit(text, (at, at + 8)) is None


def test_full_names_surnames_and_titles_name_the_pi():
    assert _credits("Gyanu Lamichhane consults for Pfizer Inc.", "Pfizer Inc")[1] == "Gyanu Lamichhane"
    assert _credits("Dr. Lamichhane holds equity in Acme Biosciences.", "Acme Biosciences")[1] == "Dr. Lamichhane"


def test_no_person_named_credits_nothing():
    assert _credits("The authors declare that Pfizer Inc. funded this work.", "Pfizer Inc") is None


def test_accent_free_keeps_length_and_offsets():
    shown = norm("Dr. Lamichhane holds equity in Société Générale Biosciences.")
    assert len(accent_free(shown)) == len(shown)
    assert accent_free(shown).index("Societe") == shown.index("Société")


def test_all_authors_pattern_is_whole_words_only():
    assert ALL_AUTHORS.search("all authorship disputes") is None
    assert ALL_AUTHORS.search("Overall authors") is None


LEE = {"last": "Lee", "fore": "Grace", "initials": "G", "collective": None}


@pytest.mark.parametrize("statement", [
    "G.L. received fees from Acme Inc; the remaining authors are employees of Beta Inc.",
    "G.L. received fees from Acme Inc; the other authors are employees of Beta Inc.",
    "G.L. is a consultant for Acme Inc; Johns Hopkins University owns equity in Beta Inc.",
    "G.L. is a consultant for Acme Inc; the university owns equity in Beta Inc.",
    "G.L. is a consultant for Acme Inc; his spouse is an employee of Beta Inc.",
    "G.L. is a consultant for Acme Inc; and his spouse is an employee of Beta Inc.",
    "G.L. consults for Acme Inc; however, the remaining authors are employees of Beta Inc.",
    "G.L. consults for Acme Inc; co-authors are employees of Beta Inc.",
    "G.L. consults for Acme Inc; funding was provided by Beta Inc.",
    "G.L. consults for Acme Inc; spouse is employed by Beta Inc.",
    "G.L. consults for Acme Inc; Beta Inc funded this study.",
    "G.L. consults for Acme Inc; Novartis and Beta Inc had no role in the design.",
    "G.L. is a consultant for Acme Inc; for this study, Beta Inc provided the antibody free of charge.",
    "G.L. received honoraria from Acme Inc; in 2019, Beta Inc acquired Acme Inc.",
    "G.L. consults for Acme Inc; to our knowledge, Beta Inc had no role.",
    "G.L. holds equity in Acme Inc; Funding: Beta Inc.",
    "G.L. holds equity in Acme Inc; Sponsor: Beta Inc.",
])
def test_a_clause_with_its_own_subject_does_not_continue_the_one_before(statement):
    assert _credits(statement, "Acme Inc") == ("G.L.", "G.L.")
    assert _credits(statement, "Beta Inc") is None


@pytest.mark.parametrize("statement", [
    "G.L. is a founder of Acme Inc; and holds equity in Beta Inc.",
    "G.L. is a consultant to Acme Inc; also to Beta Inc.",
    "G.L. is a consultant to Acme Inc; he also holds equity in Beta Inc.",
    "G.L. is a consultant to Acme Inc; Beta Inc; and Gamma Inc.",
    "G.L. consults for Acme Inc; In addition, he holds equity in Beta Inc.",
    "G.L. is a consultant to Acme Inc; Beta Inc (equity); and Gamma Inc.",
    "G.L. consults for Acme Inc; also, Beta Inc.",
    "G.L. consults for Acme Inc; also to Beta Inc.",
    "G.L. consults for Acme Inc; and for Beta Inc.",
    "G.L. consults for Acme Inc; currently holds equity in Beta Inc.",
])
def test_a_subjectless_continuation_keeps_the_subject(statement):
    assert _credits(statement, "Acme Inc") == ("G.L.", "G.L.")
    assert _credits(statement, "Beta Inc") == ("G.L.", "G.L.")


def test_a_company_list_continues_past_an_aside():
    s = "G.L. is a consultant to Acme Inc; Beta Inc (equity); and Gamma Inc."
    assert _credits(s, "Gamma Inc") == ("G.L.", "G.L.")


def test_companies_that_are_the_clause_subject_are_not_credited():
    s = "G.L. consults for Acme Inc; Novartis and Pfizer had no role in the design."
    assert _credits(s, "Novartis") is None
    assert _credits(s, "Pfizer") is None


def test_a_continuation_stops_at_a_clause_with_a_subject():
    s = "G.L. consults for Acme Inc; Johns Hopkins University owns Beta Inc; and holds Gamma Inc."
    assert _credits(s, "Gamma Inc") is None


def test_a_possessive_after_the_pi_is_not_the_pi():
    assert _credits("Dr. Lee's spouse is an employee of Acme Inc.", "Acme Inc", (LEE, SMITH), "Grace Lee") is None
    assert _credits("Dr. Lee’s spouse is an employee of Acme Inc.", "Acme Inc", (LEE, SMITH), "Grace Lee") is None
    assert _credits("Dr. Lee is an employee of Acme Inc.", "Acme Inc", (LEE, SMITH), "Grace Lee") == ("Dr. Lee", "Dr. Lee")
    assert _credits("Dr. Lee's immediate family owns Acme Inc.", "Acme Inc", (LEE, SMITH), "Grace Lee") is None


@pytest.mark.parametrize("statement", ["G.L.'s company, Acme Inc, licenses the patent.",
                                       "G.L.'s startup Acme Inc holds the license."])
def test_a_possessive_of_a_non_relative_still_names_the_pi(statement):
    assert _credits(statement, "Acme Inc") == ("G.L.", "G.L.")
    s = "Dr. Lee's startup Acme Inc holds the license."
    assert _credits(s, "Acme Inc", (LEE, SMITH), "Grace Lee") == ("Dr. Lee", "Dr. Lee")


@pytest.mark.parametrize("statement", [
    "The spouse of G.L. is an employee of Acme Inc.",
    "spouse of G.L. is an employee of Acme Inc.",
])
def test_a_relative_of_the_pi_is_not_the_pi(statement):
    assert _credits(statement, "Acme Inc") is None


def test_a_relative_of_dr_lee_is_not_dr_lee():
    lee = ((LEE, SMITH), "Grace Lee")
    assert _credits("The wife of Dr. Lee is an employee of Acme Inc.", "Acme Inc", *lee) is None
    assert _credits("Dr. Lee is an employee of Acme Inc.", "Acme Inc", *lee) == ("Dr. Lee", "Dr. Lee")


def test_a_possessive_pi_mention_still_ends_the_run():
    s = "G.L. consults for Acme Inc; G.L.'s spouse is an employee of Beta Inc."
    assert _credits(s, "Beta Inc") is None
    s = "G.L. consults for Acme Inc and J.S.'s spouse is an employee of Beta Inc."
    assert _credits(s, "Beta Inc") is None


def test_clause_walk_stays_linear_on_a_long_statement():
    s = "G.L. consults for Acme Inc" + "; and also" * 2000 + " holds Beta Inc."
    assert len(s) > 20_000
    assert _credits(s, "Beta Inc") == ("G.L.", "G.L.")
