"""Pure decision logic for scripts/repair_pi_corpus.py and the persona-year
parser in scripts/audit_tenure_scope.py.

No DB, no network — fixtures only, per the plan's Task 3/Task 15 constraint
that unit tests exercise the removal/review partition, the duplicate-title
selection, and the persona-year parser as pure functions.
"""

import pytest

from scripts.audit_tenure_scope import (
    find_persona_tenure_leaks,
    parse_persona_publication_years,
)
from scripts.repair_pi_corpus import (
    StoredPublication,
    _record_has_surname,
    build_removal_and_review_plan,
    classify_excluded_type,
    classify_stored_publications,
    find_duplicate_title_removals,
    normalize_title,
    partition_duplicate_pmids,
)


def _pub(id_, pmid, title="Some Paper", year=2020):
    return StoredPublication(id=id_, pmid=pmid, title=title, year=year)


def _author(last=None, fore=None, initials=None, collective=None, affs=()):
    return {
        "last": last,
        "fore": fore,
        "initials": initials,
        "collective": collective,
        "affiliations": list(affs),
    }


def _record(pmid, title="Some Paper", year=2020, pub_types=("Journal Article",), authors=()):
    return {
        "pmid": str(pmid),
        "title": title,
        "year": year,
        "pub_types": list(pub_types),
        "authors": list(authors),
    }


# ---------------------------------------------------------------------------
# normalize_title / classify_excluded_type
# ---------------------------------------------------------------------------


def test_normalize_title_lowercases_and_strips_punctuation():
    assert normalize_title("The C. Elegans Study!") == "thecelegansstudy"


def test_classify_excluded_type_is_none_for_a_clean_journal_article():
    assert classify_excluded_type(["Journal Article"]) is None


def test_classify_excluded_type_flags_a_pure_excluded_row():
    assert classify_excluded_type(["Comment"]) == "excluded_type"


def test_classify_excluded_type_routes_a_secondary_hit_to_review_not_removal():
    # D4's caveat: a Comment alongside a real Journal Article must not be
    # auto-removed.
    assert (
        classify_excluded_type(["Comment", "Journal Article"])
        == "secondary_excluded_type"
    )


# ---------------------------------------------------------------------------
# bare initial (corpus._match_pi_author_detail)
# ---------------------------------------------------------------------------


def _bare(record, name):
    from src.services.corpus import _match_pi_author_detail

    return _match_pi_author_detail(record, name)[2]


def test_bare_initial_true_when_forename_is_a_single_letter():
    record = _record(1, authors=[_author("Rothstein", "J", "J")])
    assert _bare(record, "Jeffrey Rothstein") is True


def test_bare_initial_true_when_only_initials_present_no_forename():
    record = _record(1, authors=[_author("Rothstein", None, "JD")])
    assert _bare(record, "Jeffrey Rothstein") is True


def test_bare_initial_false_for_a_full_forename_match():
    record = _record(1, authors=[_author("Green", "Rachel", "R")])
    assert _bare(record, "Rachel Green") is False


def test_bare_initial_false_for_a_single_token_pi_name():
    # No first name to discriminate on at all — not a "bare initial" case.
    record = _record(1, authors=[_author("Cher", None, "C")])
    assert _bare(record, "Cher") is False


# ---------------------------------------------------------------------------
# partition_duplicate_pmids
# ---------------------------------------------------------------------------


def test_duplicate_pmid_keeps_the_first_row_and_removes_the_rest():
    stored = [_pub("a", "111"), _pub("b", "111"), _pub("c", "222")]
    removals, canonical = partition_duplicate_pmids(stored)
    assert [r.publication_id for r in removals] == ["b"]
    assert removals[0].reason == "duplicate_pmid"
    assert [p.id for p in canonical] == ["a", "c"]


def test_a_null_pmid_row_is_never_treated_as_a_duplicate():
    stored = [_pub("a", None), _pub("b", None)]
    removals, canonical = partition_duplicate_pmids(stored)
    assert removals == []
    assert [p.id for p in canonical] == ["a", "b"]


# ---------------------------------------------------------------------------
# classify_stored_publications — the removal/review partition
# ---------------------------------------------------------------------------


def test_no_pmid_row_goes_to_review_not_removal():
    stored = [_pub("a", None)]
    removals, review, survivors = classify_stored_publications(stored, {}, "Rachel Green")
    assert removals == []
    assert [r.reason for r in review] == ["no_pmid"]
    assert survivors == []


def test_a_stored_pmid_pubmed_no_longer_returns_goes_to_review_as_unrefetchable():
    stored = [_pub("a", "999")]
    removals, review, survivors = classify_stored_publications(stored, {}, "Rachel Green")
    assert removals == []
    assert [r.reason for r in review] == ["unrefetchable"]
    assert survivors == []


def test_no_individual_author_match_is_an_automatic_removal():
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author("Smith", "Ann", "A")])}
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Rachel Green"
    )
    assert [r.reason for r in removals] == ["no_individual_author_match"]
    assert review == []
    assert survivors == []


def test_a_consortium_only_record_goes_to_review_not_removal():
    # resolve_corpus declines to ADD a consortium-only paper (R1); deleting
    # one a human already verified is a different and one-way act.
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author(collective="N3C Consortium")])}
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Rachel Green"
    )
    assert removals == []
    assert [r.reason for r in review] == ["consortium_only"]


def test_an_empty_author_list_is_never_an_automatic_removal():
    # Ana Pombo / PMID 37336950: PubMed returns the record with no AuthorList
    # at all. No authors is absence of evidence, not evidence of absence.
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[])}
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Ana Pombo"
    )
    assert removals == []
    assert [r.reason for r in review] == ["unverifiable_no_authors"]


def test_a_forename_disagreement_goes_to_review_not_removal():
    # Ioannis Kevrekidis / PMID 42215486: the record says "Kevrekidis,
    # Yannis" — the same person under a different given-name form. A
    # forename disagreement is a judgement call in both directions.
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author("Kevrekidis", "Yannis", "Y")])}
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Ioannis Kevrekidis"
    )
    assert removals == []
    assert [r.reason for r in review] == ["forename_mismatch"]


def test_a_record_listing_authors_none_of_whom_share_the_surname_is_removed():
    # Rothstein / PMID 36284789: six named authors, no Rothstein. This is the
    # only case that positively contradicts the attribution.
    stored = [_pub("a", "1")]
    refetched = {
        "1": _record(
            1,
            authors=[_author("Cossu", "Giulia", "G"), _author("Atkins", "Tyler", "T")],
        )
    }
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Jeffrey Rothstein"
    )
    assert [r.reason for r in removals] == ["no_individual_author_match"]
    assert review == []


def test_a_pure_excluded_type_is_an_automatic_removal():
    stored = [_pub("a", "1")]
    refetched = {
        "1": _record(
            1, pub_types=["Comment"], authors=[_author("Green", "Rachel", "R")]
        )
    }
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Rachel Green"
    )
    assert [r.reason for r in removals] == ["excluded_type"]
    assert review == []


def test_a_secondary_excluded_type_goes_to_review_never_removal():
    stored = [_pub("a", "1")]
    refetched = {
        "1": _record(
            1,
            pub_types=["Comment", "Journal Article"],
            authors=[_author("Green", "Rachel", "R")],
        )
    }
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Rachel Green"
    )
    assert removals == []
    assert [r.reason for r in review] == ["secondary_excluded_type"]
    assert survivors == []


def test_a_bare_initial_match_goes_to_review_and_is_never_auto_removed():
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author("Rothstein", "J", "J")])}
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Jeffrey Rothstein"
    )
    assert removals == []
    assert [r.reason for r in review] == ["bare_initial_only"]
    assert survivors == []


def test_a_clean_full_name_match_survives_to_the_duplicate_title_pass():
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author("Green", "Rachel", "R")])}
    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Rachel Green"
    )
    assert removals == []
    assert review == []
    assert len(survivors) == 1
    assert survivors[0][0].id == "a"


def test_an_orcid_bearing_author_survives_only_when_the_pi_orcid_is_given():
    # "Wilson, D Scott" for "Scott Wilson": the forename disagrees, but the
    # author carries the PI's ORCID, which the pipeline's ORCID-first pass accepts.
    orcid = "0000-0002-1825-0097"
    author = _author("Wilson", "D Scott", "DS")
    author["orcid"] = orcid
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[author])}

    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Scott Wilson", orcid
    )
    assert removals == []
    assert review == []
    assert [p.id for p, _ in survivors] == ["a"]

    removals, review, survivors = classify_stored_publications(
        stored, refetched, "Scott Wilson"
    )
    assert removals == []
    assert [r.reason for r in review] == ["forename_mismatch"]
    assert survivors == []


# ---------------------------------------------------------------------------
# find_duplicate_title_removals — resolve_corpus's own rule
# ---------------------------------------------------------------------------


def test_duplicate_title_keeps_the_higher_year_and_removes_the_earlier():
    survivors = [
        (_pub("a", "100", title="CRISPR Screen of Organoid Lines"),
         _record(100, title="CRISPR Screen of Organoid Lines", year=2020)),
        (_pub("b", "200", title="CRISPR Screen of Organoid Lines (preprint)", year=2019),
         _record(200, title="CRISPR Screen of Organoid Lines!", year=2019)),
    ]
    removals, kept = find_duplicate_title_removals(survivors)
    assert [r.publication_id for r in removals] == ["b"]
    assert removals[0].reason == "duplicate_title"
    assert [p.id for p, _ in kept] == ["a"]


def test_duplicate_title_tiebreaks_on_higher_pmid_when_years_are_equal():
    survivors = [
        (_pub("a", "100", title="Same Title of a Long Enough Paper", year=2020),
         _record(100, title="Same Title of a Long Enough Paper", year=2020)),
        (_pub("b", "200", title="Same Title of a Long Enough Paper", year=2020),
         _record(200, title="Same Title of a Long Enough Paper", year=2020)),
    ]
    removals, kept = find_duplicate_title_removals(survivors)
    assert [r.publication_id for r in removals] == ["a"]
    assert [p.id for p, _ in kept] == ["b"]


@pytest.mark.parametrize(("title", "years", "removed"), [
    ("T", (2019, 2020), []),
    ("Mechanism of X in Y cells", (2019, 2020), ["a"]),
    ("Mechanism of X in Y cells", (2010, 2020), []),
])
def test_duplicate_title_needs_a_long_title_and_close_years(title, years, removed):
    survivors = [
        (_pub("a", "100", title=title, year=years[0]), _record(100, title=title, year=years[0])),
        (_pub("b", "200", title=title, year=years[1]), _record(200, title=title, year=years[1])),
    ]
    removals, _kept = find_duplicate_title_removals(survivors)
    assert [r.publication_id for r in removals] == removed


def test_the_matcher_reads_the_rows_anchor():
    # R3.8 hand-off: on an anchored row an author carrying a different ORCID iD does not by
    # itself disqualify the PI (Q15); on an unanchored one it does, and the surname makes it
    # a review item, never an exclusion.
    author = {**_author("Dang", "Chi V", "CV"), "orcid": "0000-0001-5133-7373"}
    refetched = {"1": _record(1, authors=[author])}
    anchored = [StoredPublication(id="a", pmid="1", title="T", year=2020, provenance="s1")]
    removals, review, survivors = classify_stored_publications(
        anchored, refetched, "Chi Dang", "0000-0002-4031-2522")
    assert (removals, review, [p.id for p, _ in survivors]) == ([], [], ["a"])
    bare = [StoredPublication(id="b", pmid="1", title="T", year=2020, provenance=None)]
    removals, review, survivors = classify_stored_publications(
        bare, refetched, "Chi Dang", "0000-0002-4031-2522")
    assert removals == [] and [r.reason for r in review] == ["forename_mismatch"]


def test_a_unique_title_is_never_flagged_as_a_duplicate():
    survivors = [
        (_pub("a", "1", title="Paper A"), _record(1, title="Paper A")),
        (_pub("b", "2", title="Paper B"), _record(2, title="Paper B")),
    ]
    removals, kept = find_duplicate_title_removals(survivors)
    assert removals == []
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# build_removal_and_review_plan — end to end over the pure pipeline
# ---------------------------------------------------------------------------


def test_the_whole_pipeline_combines_duplicate_pmid_identity_and_title_dedup():
    stored = [
        _pub("a", "1", title="Paper One", year=2019),
        _pub("b", "1", title="Paper One (dup pmid)", year=2019),  # duplicate_pmid
        _pub("c", "2", title="No Match Paper"),  # no_individual_author_match
        _pub("d", "3", title="Comment Only"),  # excluded_type
        _pub("e", "4", title="Same Title of a Long Enough Paper", year=2020),
        _pub("f", "5", title="Same Title of a Long Enough Paper!", year=2021),  # wins over e
    ]
    refetched = {
        "1": _record(1, title="Paper One", authors=[_author("Green", "Rachel", "R")]),
        "2": _record(2, authors=[_author("Smith", "Ann", "A")]),
        "3": _record(3, pub_types=["Comment"], authors=[_author("Green", "Rachel", "R")]),
        "4": _record(4, title="Same Title of a Long Enough Paper", year=2020,
                     authors=[_author("Green", "Rachel", "R")]),
        "5": _record(5, title="Same Title of a Long Enough Paper!", year=2021,
                     authors=[_author("Green", "Rachel", "R")]),
    }
    removals, review = build_removal_and_review_plan(stored, refetched, "Rachel Green")
    reasons_by_id = {r.publication_id: r.reason for r in removals}
    assert reasons_by_id == {
        "b": "duplicate_pmid",
        "c": "no_individual_author_match",
        "d": "excluded_type",
        "e": "duplicate_title",
    }
    assert review == []


# ---------------------------------------------------------------------------
# audit_tenure_scope's persona-year parser
# ---------------------------------------------------------------------------


def test_parse_persona_publication_years_reads_only_the_recent_publications_section():
    markdown = """# Some Lab

**Institution:** JHU

## Active Grants

- A grant from 1999 that must not be read as a publication year

## Recent Publications

- A Paper Title. *Some Journal*. (2022). https://pubmed.ncbi.nlm.nih.gov/1/
- Another Paper. *Other Journal*. (2019). https://doi.org/10.1/x

## Keywords

- something (2005)
"""
    assert parse_persona_publication_years(markdown) == [2022, 2019]


def test_parse_persona_publication_years_reads_the_year_not_a_year_in_the_journal():
    # A journal name can carry its own parenthesized year; the publication
    # year is the last one before the link (margolick, 2026-09-25).
    markdown = """## Recent Publications

- Sleep and Hypertension. *Journal of acquired immune deficiency syndromes (1999)*. (2023). https://doi.org/10.1097/QAI.0000000000003276
- A SICI DOI. *J*. (2001). https://doi.org/10.1002/(SICI)1097-0215(2000)80:1
"""
    assert parse_persona_publication_years(markdown) == [2023, 2001]


def test_parse_persona_publication_years_returns_empty_when_section_absent():
    assert parse_persona_publication_years("# Lab\n\nNo publications here.\n") == []


def test_find_persona_tenure_leaks_flags_years_before_the_tenure_start():
    assert find_persona_tenure_leaks([2022, 2019, 2015], tenure_start=2020) == [2019, 2015]


def test_find_persona_tenure_leaks_is_empty_when_tenure_start_is_unset():
    # D20: no window recorded at all — nothing can leak against it.
    assert find_persona_tenure_leaks([1990, 2000], tenure_start=None) == []


def test_find_persona_tenure_leaks_is_empty_when_all_years_are_in_window():
    assert find_persona_tenure_leaks([2021, 2022], tenure_start=2020) == []


@pytest.mark.parametrize("pi_name", ["John Smith Jr.", "John Smith, PhD"])
def test_a_suffix_or_degree_is_not_taken_for_the_surname_of_a_forename_mismatch(pi_name):
    # The surname comes from person_names.surname_candidates (spec 2026-10-05 §6.3): a
    # Smith with another forename is a judgement call, not a "no author shares the
    # surname" removal, which is what reading "Jr." or "PhD" as the surname produced.
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author("Smith", "Robert", "R")])}
    removals, review, _survivors = classify_stored_publications(stored, refetched, pi_name)
    assert removals == []
    assert [r.reason for r in review] == ["forename_mismatch"]


# ---------------------------------------------------------------------------
# Spec 2026-10-15 Phase 3: the repair agrees with the upgraded matcher
# ---------------------------------------------------------------------------


def test_record_has_surname_agrees_with_the_matcher_on_joins():
    record = _record(1, authors=[_author("Hamacher Brady", "Anne", "A")])
    assert _record_has_surname(record, "Anne Hamacher-Brady") is True


def test_record_has_surname_reads_a_comma_form_name():
    stored = [_pub("a", "1")]
    refetched = {"1": _record(1, authors=[_author("Doe", "John", "J")])}
    removals, review, _survivors = classify_stored_publications(stored, refetched, "Doe, Jane")
    assert removals == []
    assert [r.reason for r in review] == ["forename_mismatch"]


def test_repair_sends_ambiguous_and_incomplete_records_to_review():
    stored = [_pub("a", "1"), _pub("b", "2", title="Other Paper")]
    refetched = {
        "1": _record(1, authors=[_author("Doe", "Jane", "J", affs=["X"]),
                                 _author("Doe", "Jane", "J", affs=["Y"])]),
        "2": {**_record(2, title="Other Paper", authors=[_author("Smith", "Ann", "A")]),
              "author_list_complete": False},
    }
    removals, review, _survivors = classify_stored_publications(stored, refetched, "Jane Doe")
    assert removals == []
    assert [(r.pmid, r.reason) for r in review] == [
        ("1", "ambiguous_author"), ("2", "unverifiable_incomplete_list"),
    ]
