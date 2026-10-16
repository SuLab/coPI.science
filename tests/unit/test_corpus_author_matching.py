"""``match_pi_author`` — the D2/D3 attribution-remediation matcher fixes
(Task 1, items 1-4, of
docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md).

Every positive fixture below reproduces a REAL production record's author
fields (surname/forename/affiliation), taken from the plan's own measured
evidence for the named PMID — not invented data. Negative fixtures are
"of your choosing" per the plan for item 4b, and item 3's negative is the
Hermann Joseph Muller / Ulrich Mueller collision the plan names directly.

``tests/unit/test_corpus.py::test_a_forename_mismatch_is_not_the_pi_even_with_the_same_surname``
(the "R Lara" Green pin) is the item-2 negative fixture and is deliberately
NOT duplicated or modified here.
"""

import pytest

from src.services import corpus
from src.services.corpus import match_pi_author


def _author(last=None, fore=None, initials=None, affs=()):
    return {
        "last": last,
        "fore": fore,
        "initials": initials,
        "collective": None,
        "affiliations": list(affs),
    }


def _rec(pmid, authors):
    return {
        "pmid": str(pmid),
        "title": f"Paper {pmid}",
        "authors": list(authors),
        "pub_types": ["Journal Article"],
    }


# ---------------------------------------------------------------------------
# Item 1 — spaced-initials ForeName ("J D", "A L") compares on the first
# initial rather than a doomed `startswith`.
# ---------------------------------------------------------------------------


def test_spaced_initials_forename_matches_the_pi_by_first_initial():
    # PMID 8785064 (1996): Rothstein indexed with ForeName="J D".
    record = _rec(
        8785064,
        [_author("Rothstein", "J D", "JD", affs=["Johns Hopkins University"])],
    )
    kind, affs = match_pi_author(record, "Jeffrey Rothstein")
    assert kind == "individual"
    assert affs == ["Johns Hopkins University"]


def test_a_different_first_initial_still_rejects_with_spaced_initials():
    # A spaced-initials ForeName whose first letter genuinely differs from
    # the expected first name's initial is still a reject — the relaxation
    # is about SPACING, not about dropping the initial check entirely.
    record = _rec(1, [_author("Liu", "A N", "AN")])
    kind, _ = match_pi_author(record, "David Liu")
    assert kind == "no_match"


# ---------------------------------------------------------------------------
# Item 2 — initial-plus-name ForeName ("J Marie"), guarded to fire only when
# the EXPECTED first name is itself an initial and the non-initial token
# equals the expected middle name.
# ---------------------------------------------------------------------------


def test_initial_plus_middle_name_forename_matches_j_marie_hardwick():
    # PMID 40874741: indexed ForeName="J Marie"; her stored first name is the
    # single token "J." (D2(a): every one of her 152 candidates is withheld
    # under the old matcher for exactly this reason).
    record = _rec(
        40874741,
        [_author("Hardwick", "J Marie", "JM", affs=["Johns Hopkins University"])],
    )
    kind, affs = match_pi_author(record, "J. Marie Hardwick")
    assert kind == "individual"
    assert affs == ["Johns Hopkins University"]


def test_initial_plus_name_does_not_fire_when_the_expected_first_name_is_full():
    # Guard: this path must only ever ADD an acceptance when the expected
    # first name is itself an initial. A full expected first name ("Marie")
    # takes the strict startswith path regardless of a middle-name-shaped
    # ForeName token.
    record = _rec(1, [_author("Hardwick", "J Marie", "JM")])
    kind, _ = match_pi_author(record, "Marie Hardwick")
    assert kind == "no_match"


# ---------------------------------------------------------------------------
# Item 3 — surname diacritic folding (a variant SET, not one normalization),
# with the asymmetry the plan requires: expected "Mueller" must match a
# record spelled "Müller", but must NOT also match a genuinely different
# "Muller" (plain ASCII).
# ---------------------------------------------------------------------------


def test_diacritic_folding_matches_ulrich_mueller_spelled_with_an_umlaut():
    # PMID 27798175 (Scripps Research, before his 2017 move to JHU):
    # indexed LastName="Müller", ForeName="Ulrich".
    record = _rec(
        27798175,
        [_author("Müller", "Ulrich", "U", affs=["Scripps Research"])],
    )
    kind, affs = match_pi_author(record, "Ulrich Mueller")
    assert kind == "individual"
    assert affs == ["Scripps Research"]


def test_ascii_muller_is_not_conflated_with_the_accented_mueller():
    # Hermann Joseph *Muller* (plain ASCII surname) must not be pulled in by
    # an expected "Mueller" generating "Muller" as a variant of ITSELF — the
    # asymmetry item 3 requires. Same forename on both sides to isolate the
    # surname comparison from the forename gate.
    record = _rec(2, [_author("Muller", "Ulrich", "U")])
    kind, _ = match_pi_author(record, "Ulrich Mueller")
    assert kind == "no_match"


# ---------------------------------------------------------------------------
# Item 4a — compound surname: the record's LastName carries an extra leading
# token, but its LAST whitespace token equals the PI's surname.
# ---------------------------------------------------------------------------


def test_compound_surname_matches_on_its_last_token():
    # PMID 33482124: indexed LastName="Van Dang", ForeName="Chi V".
    record = _rec(
        33482124,
        [_author("Van Dang", "Chi V", "CV", affs=["Johns Hopkins University"])],
    )
    kind, affs = match_pi_author(record, "Chi Dang")
    assert kind == "individual"
    assert affs == ["Johns Hopkins University"]


# ---------------------------------------------------------------------------
# Item 4b — particle splice: PubMed strands a name particle ("O'", "Mc",
# "Mac", "de", "van", "von", "del", "la") at the END of ForeName rather than
# the start of LastName. Distinct from 4a: last-token equality alone does not
# recover "Neal"/"Anya J O'" against "Anya O'Neal" (apostrophe-stripping
# "o'neal" -> "oneal" doesn't match "neal" either).
# ---------------------------------------------------------------------------


def test_particle_splice_recovers_oneal_from_a_stranded_apostrophe_o():
    # PMID 34140472: indexed LastName="Neal", ForeName="Anya J O'".
    record = _rec(
        34140472,
        [_author("Neal", "Anya J O'", "AJO", affs=["Johns Hopkins University"])],
    )
    kind, affs = match_pi_author(record, "Anya O'Neal")
    assert kind == "individual"
    assert affs == ["Johns Hopkins University"]


def test_particle_splice_does_not_override_a_forename_mismatch():
    # Over-match surface (plan-flagged): the splice recovers the SURNAME
    # ("O'Neal"), but the remainder forename must still be checked against
    # the PI's own first name. A different "Zara O'Neal" indexed the same
    # way as the Anya O'Neal fixture above must not be attributed to Anya.
    record = _rec(3, [_author("Neal", "Zara J O'", "ZJO")])
    kind, _ = match_pi_author(record, "Anya O'Neal")
    assert kind == "no_match"


def test_particle_splice_is_not_applied_when_it_would_not_help():
    # A ForeName that merely happens to end in a particle-like token for an
    # unrelated reason (here, a genuinely different surname "Ostrander" with
    # a ForeName ending in "Mc") must change nothing — the splice is only
    # adopted when it actually produces a surname match.
    record = _rec(4, [_author("Ostrander", "Kim Mc", "KM")])
    kind, _ = match_pi_author(record, "Anya O'Neal")
    assert kind == "no_match"


# ---------------------------------------------------------------------------
# ORCID on the author: identity by iD, with the surname still required.
# ---------------------------------------------------------------------------

_WILSON_ORCID = "0000-0001-8029-1036"


def _orcid_author(last, fore, orcid, initials=None, affs=()):
    return {**_author(last, fore, initials, affs), "orcid": orcid}


def test_an_author_carrying_the_pi_orcid_matches_despite_a_different_forename():
    record = _rec(
        1, [_orcid_author("Wilson", "D Scott", _WILSON_ORCID, "DS", ["Johns Hopkins"])]
    )
    assert match_pi_author(record, "Scott Wilson")[0] == "no_match"
    kind, affs = match_pi_author(record, "Scott Wilson", _WILSON_ORCID)
    assert kind == "individual"
    assert affs == ["Johns Hopkins"]


def test_the_orcid_pass_compares_case_insensitively():
    record = _rec(1, [_orcid_author("Wilson", "D Scott", "0000-0000-0000-001X")])
    assert match_pi_author(record, "Scott Wilson", "0000-0000-0000-001x")[0] == "individual"


def test_an_orcid_match_still_needs_the_surname():
    record = _rec(1, [_orcid_author("Smith", "Scott", _WILSON_ORCID)])
    assert match_pi_author(record, "Scott Wilson", _WILSON_ORCID)[0] == "no_match"


def test_the_orcid_authors_affiliations_win_over_an_earlier_namesake():
    record = _rec(
        1,
        [
            _author("Wilson", "Scott", "S", affs=["Elsewhere University"]),
            _orcid_author("Wilson", "D Scott", _WILSON_ORCID, affs=["Johns Hopkins"]),
        ],
    )
    kind, affs = match_pi_author(record, "Scott Wilson", _WILSON_ORCID)
    assert kind == "individual"
    assert affs == ["Johns Hopkins"]


def test_an_orcid_like_name_matches_nobody_even_with_an_orcid():
    record = _rec(1, [_orcid_author("0097", "0000-0002-1825", "0000-0002-1825-0097")])
    kind, _ = match_pi_author(record, "0000-0002-1825-0097", "0000-0002-1825-0097")
    assert kind == "no_match"


# ---------------------------------------------------------------------------
# A stored initial-only first name ("D. Scott Wilson") against a spelled-out
# PubMed forename ("David S"): a bare-initial match the gate must corroborate.
# PMIDs 18787098 / 26631690 index him as "Wilson, David S".
# ---------------------------------------------------------------------------


def test_a_stored_initial_matches_a_spelled_out_forename_as_a_bare_initial():
    from src.services.corpus import _match_pi_author_detail

    record = _rec(18787098, [_author("Wilson", "David S", "DS")])
    assert _match_pi_author_detail(record, "D. Scott Wilson") == ("individual", [], True)


def test_a_stored_initial_rejects_a_forename_with_another_initial():
    record = _rec(1, [_author("Wilson", "Scott", "S")])
    assert match_pi_author(record, "D. Scott Wilson")[0] == "no_match"


def test_a_second_forename_initial_that_contradicts_the_stored_middle_name_rejects():
    record = _rec(1, [_author("Wilson", "Daniel R", "DR")])
    assert match_pi_author(record, "D. Scott Wilson")[0] == "no_match"


def test_a_stored_initial_first_name_needs_the_middle_initial_to_agree():
    from src.services.corpus import _match_pi_author_detail

    name = "D. Scott Wilson"
    assert match_pi_author(_rec(1, [_author("Wilson", "Deborah", "D")]), name)[0] == "no_match"
    assert match_pi_author(_rec(2, [_author("Wilson", "D A", "DA")]), name)[0] == "no_match"
    assert _match_pi_author_detail(_rec(3, [_author("Wilson", "D S", "DS")]), name) == (
        "individual", [], True,
    )
    # A first name that is spelled out in the stored name is unaffected.
    assert match_pi_author(_rec(4, [_author("Rothstein", "J D", "JD")]), "Jeffrey Rothstein")[0] == "individual"


def test_an_author_with_a_different_orcid_is_not_the_pi():
    other = _author("Wilson", "D Scott", "DS")
    other["orcid"] = "0000-0001-5133-7373"
    record = _rec(5, [other])
    assert match_pi_author(record, "D. Scott Wilson", "0000-0001-8029-1036")[0] == "no_match"
    # Without the PI's ORCID to compare, the name pass is unchanged.
    assert match_pi_author(record, "D. Scott Wilson")[0] == "individual"


# ---------------------------------------------------------------------------
# Spec 2026-10-15 Phase 3 (R3.1-R3.8, R3.11)
# ---------------------------------------------------------------------------

_FOREIGN_ORCID = "0000-0001-5133-7373"


def _kind(last, fore, name, initials=None, **kwargs):
    return match_pi_author(_rec(1, [_author(last, fore, initials)]), name, **kwargs)[0]


@pytest.mark.parametrize(("name", "last", "fore"), [
    ("Chi Kjaer", "Kjær", "Chi"),
    ("Søren Kjær", "Kjaer", "Soren"),
    ("Hans Orsted", "Ørsted", "Hans"),
    ("Jan Lopuszanski", "Łopuszański", "Jan"),
    ("Hans Brondsted", "Brøndsted", "Hans"),
])
def test_surnames_fold_the_letters_nfkd_keeps(name, last, fore):
    assert _kind(last, fore, name) == "individual"


@pytest.mark.parametrize(("name", "last", "fore"), [
    ("Jose Garcia", "García", "José"),
    ("Søren Brage", "Brage", "Soren"),
    ("Jürgen Schmidt", "Schmidt", "Juergen"),
    ("Juergen Schmidt", "Schmidt", "Jürgen"),
])
def test_given_names_are_folded(name, last, fore):
    assert _kind(last, fore, name) == "individual"


@pytest.mark.parametrize(("name", "last", "fore", "kind"), [
    ("Anne Hamacher-Brady", "Hamacher Brady", "Anne", "individual"),
    ("Brian O’Rourke", "O'Rourke", "Brian", "individual"),
    ("Anna van der Berg", "Vanderberg", "Anna", "individual"),
    ("Anne Brady", "Hamacher-Brady", "Anne", "individual"),
    ("Chi Dang", "Van Dang", "Chi V", "individual"),
    ("Rachel Green", "Greenberg", "Rachel", "no_match"),
])
def test_surname_joins(name, last, fore, kind):
    assert _kind(last, fore, name) == kind


@pytest.mark.parametrize(("name", "last", "fore"), [
    ("José Garcia", "Garcia-Lopez", "José"),
    ("Gabriel Garcia Marquez", "Garcia", "Gabriel"),
    ("Carolina Gonzalez-Cavazos", "Gonzalez Cavazos", "Carolina"),
])
def test_surname_containment_works_both_ways(name, last, fore):
    assert _kind(last, fore, name) == "individual"


@pytest.mark.parametrize(("name", "last"), [
    ("Anna de Souza", "De"),
    ("Anna de Souza", "de Oliveira"),
    ("Anna van der Berg", "Van"),
    ("Anna van der Berg", "van den Bosch"),
])
def test_containment_never_matches_on_a_particle(name, last):
    assert _kind(last, "Anna", name) == "no_match"


def test_containment_still_needs_the_forename():
    assert _kind("Garcia-Lopez", "Juan", "José Garcia") == "no_match"


def test_packed_initials_are_initials():
    for fore in ("JD", "J.D."):
        record = _rec(1, [_author("Rothstein", fore, "JD")])
        assert corpus._match_pi_author_detail(record, "Jeffrey Rothstein") == ("individual", [], True)


def test_an_all_caps_forename_with_other_initials_is_a_name():
    assert _kind("Rothstein", "JOHN", "Jeffrey Rothstein", "J") == "no_match"


@pytest.mark.parametrize("fore", ["DS", "D.S.", "D. S."])
def test_p6_packed_initials_corroborate_the_middle_name(fore):
    record = _rec(1, [_author("Wilson", fore, "DS")])
    assert corpus._match_pi_author_detail(record, "D. Scott Wilson") == ("individual", [], True)


def test_p6_initials_without_a_forename_need_the_middle_initial():
    name = "D. Scott Wilson"
    assert _kind("Wilson", None, name, "DA") == "no_match"
    for initials in ("DS", "D"):
        record = _rec(1, [_author("Wilson", None, initials)])
        assert corpus._match_pi_author_detail(record, name) == ("individual", [], True)


@pytest.mark.parametrize(("fore", "initials"), [
    ("D A", "DA"), ("DA", "DA"), ("Deborah", "D"), ("Daniel R", "DR"),
])
def test_p6_commit_intent_still_holds(fore, initials):
    assert _kind("Wilson", fore, "D. Scott Wilson", initials) == "no_match"


def test_a_shorter_spelled_out_forename_agrees():
    record = _rec(1, [_author("Vogelstein", "Bert")])
    assert corpus._match_pi_author_detail(record, "Bertram Vogelstein") == ("individual", [], False)
    assert _kind("Smith", "Al", "Alan Smith") == "no_match"


def test_hyphenated_and_joined_given_names_agree():
    assert _kind("Li", "Jian-Feng", "Jianfeng Li") == "individual"
    assert _kind("Li", "Jianfeng", "Jian-Feng Li") == "individual"


def test_named_cases_from_main_am_8():
    assert _kind("Wu", "Wei", "Di Wu") == "no_match"
    assert _kind("Zhang", "Wei", "Zhang Wen") == "no_match"
    assert _kind("Zhang", "Wen", "Wei Zhang") == "no_match"
    record = _rec(1, [_author("Wu", "D", "D")])
    assert corpus._match_pi_author_detail(record, "Di Wu") == ("individual", [], True)


def test_a_comma_form_name_matches():
    assert _kind("Doe", "Jane", "Doe, Jane") == "individual"
    assert _kind("Doe", "John", "Doe, Jane") == "no_match"
    assert _kind("Garcia Marquez", "Gabriel", "Garcia Marquez, Gabriel") == "individual"
    record = _rec(1, [_author("Wilson", "DS", "DS")])
    assert corpus._match_pi_author_detail(record, "Wilson, D. Scott") == ("individual", [], True)
    assert _kind("Wilson", "D A", "Wilson, D. Scott", "DA") == "no_match"


def _invalid(last, fore, initials=None, affs=(), orcid=None):
    author = {**_author(last, fore, initials, affs), "valid": False}
    if orcid:
        author["orcid"] = orcid
    return author


def test_an_invalid_author_entry_is_never_the_pi():
    assert match_pi_author(_rec(1, [_invalid("Green", "Rachel")]), "Rachel Green")[0] == "no_match"
    record = _rec(1, [_invalid("Green", "Rachel", affs=["X"]), _author("Green", "Rachel", affs=["Y"])])
    assert match_pi_author(record, "Rachel Green") == ("individual", ["Y"])


def test_an_invalid_entry_carrying_the_pi_orcid_is_skipped():
    record = _rec(1, [_invalid("Wilson", "D Scott", "DS", orcid=_WILSON_ORCID)])
    assert match_pi_author(record, "Scott Wilson", _WILSON_ORCID)[0] == "no_match"


def test_an_incomplete_list_without_the_pi():
    def kind(*authors, complete):
        record = {**_rec(1, authors), "author_list_complete": complete}
        return match_pi_author(record, "Rachel Green")[0]

    collective = {**_author(), "collective": "The X Consortium"}
    assert kind(_author("Smith", "Ann"), complete=False) == "incomplete_list"
    assert kind(_author("Smith", "Ann"), _author("Green", "Rachel"), complete=False) == "individual"
    assert kind(collective, complete=False) == "incomplete_list"
    assert kind(collective, complete=True) == "consortium"


def test_the_unique_spelled_out_forename_wins_over_an_initial():
    record = _rec(1, [_author("Doe", "J", affs=["X"]), _author("Doe", "Jane", affs=["Y"])])
    assert corpus._match_pi_author_detail(record, "Jane Doe") == ("individual", ["Y"], False)


def test_two_identical_candidates_are_ambiguous():
    record = _rec(1, [_author("Doe", "Jane", affs=["X"]), _author("Doe", "Jane", affs=["Y"])])
    match = corpus._match_pi_author_full(record, "Jane Doe")
    assert match.kind == "ambiguous"
    assert match.affiliations == []
    assert tuple(c.affiliations for c in match.candidates) == (("X",), ("Y",))


def test_p3_a_foreign_orcid_does_not_veto_an_anchored_record():
    record = _rec(1, [_orcid_author("Dang", "Chi V", _FOREIGN_ORCID)])
    pi_orcid = "0000-0002-4031-2522"
    assert match_pi_author(record, "Chi Dang", pi_orcid, anchored=True)[0] == "individual"
    assert match_pi_author(record, "Chi Dang", pi_orcid, anchored=False)[0] == "no_match"


def test_tie_break_1_prefers_the_candidate_without_an_orcid():
    record = _rec(1, [_orcid_author("Zhang", "Wei", _FOREIGN_ORCID, affs=["A"]),
                      _author("Zhang", "Wei", affs=["B"])])
    assert match_pi_author(record, "Wei Zhang", "0000-0002-1825-0097", anchored=True) == (
        "individual", ["B"],
    )


def test_two_foreign_candidates_on_an_anchored_record_are_ambiguous():
    record = _rec(1, [_orcid_author("Zhang", "Wei", _FOREIGN_ORCID),
                      _orcid_author("Zhang", "Wei", _WILSON_ORCID)])
    assert match_pi_author(record, "Wei Zhang", "0000-0002-1825-0097", anchored=True)[0] == "ambiguous"


def test_the_pis_orcid_elsewhere_disqualifies_a_foreign_namesake():
    pi_orcid = "0000-0002-1825-0097"
    record = _rec(1, [_orcid_author("Doe", "Jane", pi_orcid),
                      _orcid_author("Roe", "Jane", _FOREIGN_ORCID)])
    assert match_pi_author(record, "Jane Roe", pi_orcid, anchored=True)[0] == "no_match"


def test_an_invalid_pi_orcid_neither_matches_nor_vetoes():
    bad, good, name = "0000-0002-1825-0098", "0000-0002-1825-0097", "D. Scott Wilson"
    record = _rec(1, [_orcid_author("Wilson", "David S", _FOREIGN_ORCID)])
    # A usable PI iD vetoes this foreign author; the bad check digit must not.
    assert match_pi_author(record, name, good)[0] == "no_match"
    match = corpus._match_pi_author_full(record, name, bad)
    # bare_initial is only ever True on the name path (the ORCID pass sets it False).
    assert (match.kind, match.affiliations, match.bare_initial, match.index) == (
        "individual", [], True, 0,
    )
    assert match.candidates[0].orcid == _FOREIGN_ORCID
    # An author carrying the invalid iD string itself is not matched by iD: a forename the
    # name pass rejects stays rejected, and an agreeing one matches through the name pass.
    rejected = _rec(2, [_orcid_author("Wilson", "Daniel R", bad, "DR")])
    assert match_pi_author(rejected, name, bad)[0] == "no_match"
    assert corpus._match_pi_author_detail(
        _rec(3, [_orcid_author("Wilson", "David S", bad, "DS")]), name, bad
    ) == ("individual", [], True)


def test_an_invalid_collective_entry_is_not_a_consortium():
    def collective(valid):
        return {**_author(), "collective": "The X Consortium", "valid": valid}

    assert match_pi_author(_rec(1, [collective(True)]), "Rachel Green")[0] == "consortium"
    assert match_pi_author(_rec(1, [collective(False)]), "Rachel Green")[0] == "no_match"


def test_no_swap_retry_after_a_foreign_candidate_is_dropped():
    pi_orcid = "0000-0002-1825-0097"
    swapped = _author("Scott", "Wilson", affs=["Swapped"])
    own_id_elsewhere = _orcid_author("Doe", "Jane", pi_orcid)
    # Alone, the swapped entry is the PI on an anchored record.
    alone = _rec(1, [own_id_elsewhere, swapped])
    assert match_pi_author(alone, "Scott Wilson", pi_orcid, anchored=True) == (
        "individual", ["Swapped"],
    )
    # A foreign namesake makes the name pass non-empty, so no swap retry runs; the PI's iD on
    # another entry then drops the namesake, leaving nobody.
    record = _rec(2, [_orcid_author("Wilson", "Scott", _FOREIGN_ORCID), own_id_elsewhere, swapped])
    assert match_pi_author(record, "Scott Wilson", pi_orcid, anchored=True)[0] == "no_match"


_R33_CASES = [
    ("Rothstein", "JD", "JD", "Jeffrey Rothstein"),
    ("Rothstein", "JOHN", "J", "Jeffrey Rothstein"),
    ("Wilson", "D.S.", "DS", "D. Scott Wilson"),
    ("Wilson", None, "DA", "D. Scott Wilson"),
    ("Wilson", "Daniel R", "DR", "D. Scott Wilson"),
    ("Vogelstein", "Bert", None, "Bertram Vogelstein"),
    ("Li", "Jian-Feng", None, "Jianfeng Li"),
    ("Wu", "D", "D", "Di Wu"),
]


@pytest.mark.parametrize(("last", "fore", "initials", "name"), _R33_CASES)
def test_detail_and_two_tuple_mirror_the_full_match(last, fore, initials, name):
    record = _rec(1, [_author(last, fore, initials, affs=["Somewhere"])])
    match = corpus._match_pi_author_full(record, name)
    assert corpus._match_pi_author_detail(record, name) == (
        match.kind, match.affiliations, match.bare_initial,
    )
    assert match_pi_author(record, name) == (match.kind, match.affiliations)


def test_a_swapped_name_matches_on_an_anchored_record():
    assert _kind("Wen", "Zhang", "Wen Zhang", anchored=True) == "individual"


def test_a_swapped_name_never_matches_unanchored():
    assert _kind("Wen", "Zhang", "Wen Zhang", anchored=False) == "no_match"


def test_the_swap_runs_only_after_zero_candidates():
    record = _rec(1, [_author("Wilson", "Scott", affs=["X"]), _author("Scott", "Wilson", affs=["Y"])])
    assert corpus._match_pi_author_detail(record, "Scott Wilson", anchored=True) == (
        "individual", ["X"], False,
    )


@pytest.mark.parametrize(("name", "last", "fore"), [
    ("Di Wu", "Wu", "Wei"), ("Zhang Wen", "Zhang", "Wei"), ("Wei Zhang", "Zhang", "Wen"),
])
def test_named_cases_stay_mismatches_when_anchored(name, last, fore):
    assert _kind(last, fore, name, anchored=True) == "no_match"
