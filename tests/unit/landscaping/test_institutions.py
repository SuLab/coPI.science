import pytest

from src.landscaping.institutions import (
    EXCLUDED_OPENALEX_IDS,
    NSF_AWARDEE,
    REPORTER_ORGS,
    ROR_IDS,
    SCRIPPS_OPENALEX_ID,
    SCRIPPS_OPENALEX_IDS,
    at_scripps_la_jolla,
    is_scripps_la_jolla_affiliation,
    names_excluded_campus,
    qualifies_openalex,
)

LA_JOLLA = "Department of Chemistry, The Scripps Research Institute, La Jolla, CA 92037, USA"
JUPITER = "The Scripps Research Institute, Jupiter, United States"
CALIBR_ID = "I4210140527"
UF_SCRIPPS_ID = "I4387156289"
OCEANOGRAPHY_ID = "I150209017"


def test_institution_set_matches_spec():
    assert SCRIPPS_OPENALEX_ID == "I123431417"
    assert SCRIPPS_OPENALEX_IDS == frozenset({"I123431417"})
    assert EXCLUDED_OPENALEX_IDS == frozenset({
        "I4387156289", "I4210140527", "I150209017", "I1311914864", "I4210146328",
        "I2802091096", "I4210107570", "I4210103888", "I4210142769", "I4210146979",
        "I4210109568", "I16970960", "I4210089022",
    })
    assert not SCRIPPS_OPENALEX_IDS & EXCLUDED_OPENALEX_IDS
    assert REPORTER_ORGS == ("SCRIPPS RESEARCH INSTITUTE, THE",)
    assert NSF_AWARDEE == "The Scripps Research Institute"
    assert ROR_IDS == frozenset({"02dxx6824"})


@pytest.mark.parametrize("text", [
    LA_JOLLA,
    "Scripps Research, 10550 North Torrey Pines Road, La Jolla, California, USA",
    "Scripps Research Translational Institute, La Jolla, CA, USA",
    # "Fl." for a floor is not the state code; UC San Diego's Wertheim school is not UF Scripps.
    "The Scripps Research Institute, 2nd Fl., 10550 North Torrey Pines Road, La Jolla, CA",
    "Scripps Research, La Jolla, CA; Herbert Wertheim School of Public Health, UC San Diego",
])
def test_matcher_passes(text):
    assert is_scripps_la_jolla_affiliation(text)


@pytest.mark.parametrize("text", [
    JUPITER,
    "Scripps Research, 130 Scripps Way, Jupiter, FL 33458, USA",
    "Department of Molecular Medicine, Scripps Research, Florida, USA",
    "The Scripps Research Institute, FL 33458",
    "Scripps Health, San Diego, CA, USA",
    "Scripps Clinic, La Jolla, CA, USA",
    "Scripps Institution of Oceanography, UC San Diego, La Jolla, CA, USA",
    "Scripps College, Claremont, CA, USA",
    "The Herbert Wertheim UF Scripps Institute for Biomedical Innovation & Technology, Jupiter, FL",
    "Calibr-Skaggs Institute for Innovative Medicines, The Scripps Research Institute, La Jolla, CA",
    "California Institute for Biomedical Research, La Jolla, CA",
    # Jupiter named only as the UF Scripps Institute, or by the state code (gate 1 audit).
    "The Wertheim UF Scripps Research Institute",
    "UF Scripps Research Institute",
    "Department of Integrative Structural and Computational Biology, Scripps Research and The "
    "Herbert Wertheim UF Scripps Institute for Biomedical Innovation & Technology",
    "Center for Inflammation Science, Herbert Wertheim Scripps Research Institute",
    "The Skaggs Graduate School at Scripps Research, La Jolla, FL, USA",
    "Taiwan Semiconductor Research Institute (TSRI), Hsinchu, Taiwan",
    "Scripps",
    "",
])
def test_matcher_fails(text):
    assert not is_scripps_la_jolla_affiliation(text)


def test_excluded_campus():
    assert names_excluded_campus(JUPITER)
    assert names_excluded_campus("Calibr, La Jolla, CA")
    assert names_excluded_campus("UF-Scripps Biomedical Research")
    assert names_excluded_campus("Department of Molecular Medicine, Scripps Research, FL, USA")
    assert not names_excluded_campus(LA_JOLLA)
    assert not names_excluded_campus("Scripps Research, Fl. 3, La Jolla")
    assert not names_excluded_campus("Herbert Wertheim School of Public Health, La Jolla")
    assert not names_excluded_campus("")


def _auth(position="last", corresponding=False, inst_ids=(), raw=(), affs=None,
          name="Avery Quillfeather"):
    auth = {
        "author": {"display_name": name, "orcid": None},
        "raw_author_name": name,
        "author_position": position,
        "is_corresponding": corresponding,
        "institutions": [{"id": f"https://openalex.org/{i}"} for i in inst_ids],
        "raw_affiliation_strings": list(raw),
    }
    if affs is not None:
        auth["affiliations"] = [
            {"raw_affiliation_string": text, "institution_ids": [f"https://openalex.org/{i}" for i in ids]}
            for text, ids in affs
        ]
    return auth


S = SCRIPPS_OPENALEX_ID


def test_tagged_la_jolla_counts_and_tagged_jupiter_does_not():
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[(LA_JOLLA, [S])]))
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], affs=[(JUPITER, [S])]))
    uf = "The Wertheim UF Scripps Research Institute"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S, UF_SCRIPPS_ID],
                                         affs=[(uf, [S, UF_SCRIPPS_ID])]))


def test_both_campuses_count():
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[(JUPITER, [S]), (LA_JOLLA, [S])]))


def test_only_tagged_entries_decide():
    # The spec reads OpenAlex's own tags: an untagged La Jolla address beside a tagged Jupiter
    # one does not rescue the authorship.
    untagged = "Department of Chemistry, La Jolla, CA"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], raw=[JUPITER, untagged],
                                         affs=[(JUPITER, [S]), (untagged, [])]))


def test_no_usable_detail_counts():
    assert at_scripps_la_jolla(_auth(inst_ids=[S]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], affs=[("", [S])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], raw=["Department of Chemistry, La Jolla"],
                                     affs=[("Department of Chemistry, La Jolla", [])]))


def test_untagged_jupiter_only_does_not_count():
    jupiter_school = "Skaggs Graduate School, Jupiter, United States"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], raw=[jupiter_school],
                                         affs=[(jupiter_school, [])]))


def test_untagged_scripps_strings_decide():
    # With no tagged entry, the strings naming Scripps decide: another institution's address
    # beside a Jupiter one does not rescue the authorship (audit 2026-11-20).
    other = "Max Planck Institute of Biochemistry, Martinsried, Germany"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S], raw=[JUPITER, other],
                                         affs=[(JUPITER, []), (other, [])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], raw=[LA_JOLLA, other],
                                     affs=[(LA_JOLLA, []), (other, [])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S], raw=[JUPITER, LA_JOLLA]))


def test_calibr():
    calibr = "Calibr-Skaggs Institute for Innovative Medicines, La Jolla, CA"
    assert not at_scripps_la_jolla(_auth(inst_ids=[CALIBR_ID], affs=[(calibr, [CALIBR_ID])]))
    both_ids = "Calibr-Skaggs Institute for Innovative Medicines, The Scripps Research Institute, La Jolla"
    assert not at_scripps_la_jolla(_auth(inst_ids=[S, CALIBR_ID], affs=[(both_ids, [S, CALIBR_ID])]))
    assert at_scripps_la_jolla(_auth(inst_ids=[S, CALIBR_ID],
                                     affs=[(calibr, [CALIBR_ID]), (LA_JOLLA, [S])]))


def test_raw_strings_decide_when_scripps_is_not_listed():
    assert at_scripps_la_jolla(_auth(raw=[LA_JOLLA]))
    assert at_scripps_la_jolla(_auth(raw=[JUPITER, LA_JOLLA]))
    assert not at_scripps_la_jolla(_auth(raw=["Scripps Health, San Diego, CA"]))
    # OpenAlex filed a La Jolla address under another institution (gate 1 audit, 2026-11-23).
    assert at_scripps_la_jolla(_auth(inst_ids=["I999"], raw=[LA_JOLLA]))
    mis_tagged = "Dept. of Integrative Structural and Computational Biology, Scripps Research, La Jolla, CA, USA"
    assert at_scripps_la_jolla(_auth(inst_ids=[OCEANOGRAPHY_ID], raw=[mis_tagged],
                                     affs=[(mis_tagged, [OCEANOGRAPHY_ID])]))
    assert not at_scripps_la_jolla(_auth(inst_ids=[OCEANOGRAPHY_ID],
                                         raw=["Scripps Institution of Oceanography, La Jolla, CA"]))
    assert not at_scripps_la_jolla(_auth(inst_ids=[UF_SCRIPPS_ID],
                                         raw=["The Wertheim UF Scripps Research Institute"]))
    assert not at_scripps_la_jolla(_auth(inst_ids=["I999"], raw=["University of Osaka, Japan"]))


def _work(*auths):
    return {"id": "https://openalex.org/W1",
            "authorships": [_auth("first", inst_ids=["I999"]), *auths]}


def test_qualifies_openalex():
    last = _auth("last", inst_ids=[S], affs=[(LA_JOLLA, [S])])
    assert qualifies_openalex(_work(last)) == (True, "last", last)
    corr_jupiter = _auth("middle", corresponding=True, inst_ids=[S], affs=[(JUPITER, [S])])
    assert qualifies_openalex(_work(corr_jupiter, last)) == (True, "last", last)
    middle = _auth("middle", inst_ids=[S], affs=[(LA_JOLLA, [S])])
    assert qualifies_openalex(_work(middle)) == (False, None, None)
    collective = _auth("last", inst_ids=[S], affs=[(LA_JOLLA, [S])],
                       name="Invented Cohort Study Consortium")
    assert qualifies_openalex(_work(collective)) == (False, None, None)
