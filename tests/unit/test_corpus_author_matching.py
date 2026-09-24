"""``match_pi_author`` — the D2/D3 attribution-remediation matcher fixes
(2026-09-22 plan, Task 1, items 1-4).

Every positive fixture below reproduces a REAL production record's author
fields (surname/forename/affiliation), taken from the plan's own measured
evidence for the named PMID — not invented data. Negative fixtures are
"of your choosing" per the plan for item 4b, and item 3's negative is the
Hermann Joseph Muller / Ulrich Mueller collision the plan names directly.

``tests/unit/test_corpus.py::test_a_forename_mismatch_is_not_the_pi_even_with_the_same_surname``
(the "R Lara" Green pin) is the item-2 negative fixture and is deliberately
NOT duplicated or modified here.
"""

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
