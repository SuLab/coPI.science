"""Pure decision logic for scripts/repair_pi_corpus.py and the persona-year
parser in scripts/audit_tenure_scope.py.

No DB, no network — fixtures only, per the plan's Task 3/Task 15 constraint
that unit tests exercise the removal/review partition, the duplicate-title
selection, and the persona-year parser as pure functions.
"""

from scripts.audit_tenure_scope import (
    find_persona_tenure_leaks,
    parse_persona_publication_years,
)
from scripts.repair_pi_corpus import (
    StoredPublication,
    build_removal_and_review_plan,
    classify_excluded_type,
    classify_stored_publications,
    find_duplicate_title_removals,
    matched_used_bare_initial,
    normalize_title,
    partition_duplicate_pmids,
    select_additions,
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
# matched_used_bare_initial
# ---------------------------------------------------------------------------


def test_bare_initial_true_when_forename_is_a_single_letter():
    record = _record(1, authors=[_author("Rothstein", "J", "J")])
    assert matched_used_bare_initial(record, "Jeffrey Rothstein") is True


def test_bare_initial_true_when_only_initials_present_no_forename():
    record = _record(1, authors=[_author("Rothstein", None, "JD")])
    assert matched_used_bare_initial(record, "Jeffrey Rothstein") is True


def test_bare_initial_false_for_a_full_forename_match():
    record = _record(1, authors=[_author("Green", "Rachel", "R")])
    assert matched_used_bare_initial(record, "Rachel Green") is False


def test_bare_initial_false_for_a_single_token_pi_name():
    # No first name to discriminate on at all — not a "bare initial" case.
    record = _record(1, authors=[_author("Cher", None, "C")])
    assert matched_used_bare_initial(record, "Cher") is False


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


# ---------------------------------------------------------------------------
# find_duplicate_title_removals — resolve_corpus's own rule
# ---------------------------------------------------------------------------


def test_duplicate_title_keeps_the_higher_year_and_removes_the_earlier():
    survivors = [
        (_pub("a", "100", title="CRISPR Screen"), _record(100, title="CRISPR Screen", year=2020)),
        (_pub("b", "200", title="CRISPR Screen (preprint)"), _record(200, title="CRISPR Screen!", year=2019)),
    ]
    removals, kept = find_duplicate_title_removals(survivors)
    assert [r.publication_id for r in removals] == ["b"]
    assert removals[0].reason == "duplicate_title"
    assert [p.id for p, _ in kept] == ["a"]


def test_duplicate_title_tiebreaks_on_higher_pmid_when_years_are_equal():
    survivors = [
        (_pub("a", "100", title="Same Title", year=2020), _record(100, title="Same Title", year=2020)),
        (_pub("b", "200", title="Same Title", year=2020), _record(200, title="Same Title", year=2020)),
    ]
    removals, kept = find_duplicate_title_removals(survivors)
    assert [r.publication_id for r in removals] == ["a"]
    assert [p.id for p, _ in kept] == ["b"]


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
        _pub("e", "4", title="Same Title", year=2018),
        _pub("f", "5", title="Same Title!", year=2021),  # wins over e (higher year)
    ]
    refetched = {
        "1": _record(1, title="Paper One", authors=[_author("Green", "Rachel", "R")]),
        "2": _record(2, authors=[_author("Smith", "Ann", "A")]),
        "3": _record(3, pub_types=["Comment"], authors=[_author("Green", "Rachel", "R")]),
        "4": _record(4, title="Same Title", year=2018, authors=[_author("Green", "Rachel", "R")]),
        "5": _record(5, title="Same Title!", year=2021, authors=[_author("Green", "Rachel", "R")]),
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
# select_additions
# ---------------------------------------------------------------------------


def test_select_additions_excludes_already_stored_pmids():
    kept = [{"pmid": "1", "year": 2020}, {"pmid": "2", "year": 2019}]
    additions, over_cap = select_additions(kept, stored_pmids=["1"], survivors=1)
    assert [r["pmid"] for r in additions] == ["2"]
    assert over_cap == []


def test_select_additions_is_empty_when_everything_is_already_stored():
    kept = [{"pmid": "1"}, {"pmid": "2"}]
    assert select_additions(kept, stored_pmids=["1", "2"], survivors=2) == ([], [])


def test_select_additions_never_pushes_the_corpus_past_the_cap():
    # Rothstein's measured shape: 15 survivors + 42 candidates = 57 without a
    # budget, over a cap of 50 ("leung at 53",
    # docs/specs/2026-08-13-pi-profile-coverage-design.md). Only 35 may land.
    kept = [{"pmid": str(i), "year": 2026 - i} for i in range(42)]
    additions, over_cap = select_additions(kept, stored_pmids=[], survivors=15, cap=50)
    assert len(additions) == 35
    assert len(over_cap) == 7
    # The budget trims the OLDEST candidates, never the stored survivors.
    assert [r["pmid"] for r in over_cap] == [str(i) for i in range(35, 42)]


def test_select_additions_stores_nothing_when_the_corpus_is_already_at_the_cap():
    kept = [{"pmid": "900", "year": 2026}]
    additions, over_cap = select_additions(kept, stored_pmids=[], survivors=50, cap=50)
    assert additions == []
    assert [r["pmid"] for r in over_cap] == ["900"]


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


def test_parse_persona_publication_years_returns_empty_when_section_absent():
    assert parse_persona_publication_years("# Lab\n\nNo publications here.\n") == []


def test_find_persona_tenure_leaks_flags_years_before_the_tenure_start():
    assert find_persona_tenure_leaks([2022, 2019, 2015], tenure_start=2020) == [2019, 2015]


def test_find_persona_tenure_leaks_is_empty_when_tenure_start_is_unset():
    # D20: no window recorded at all — nothing can leak against it.
    assert find_persona_tenure_leaks([1990, 2000], tenure_start=None) == []


def test_find_persona_tenure_leaks_is_empty_when_all_years_are_in_window():
    assert find_persona_tenure_leaks([2021, 2022], tenure_start=2020) == []
