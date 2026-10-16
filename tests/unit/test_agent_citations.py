"""C2 (src/agent/dois.py), R2.3 of the 2026-10-15 publication-handling design: A-D1 to
A-D4 and main ID-6 to ID-10."""
import time

import pytest

from src.agent.agent import Agent
from src.agent.dois import (
    canonical_doi,
    canonical_pmid,
    extract_dois,
    extract_pmids,
    find_dois,
    paper_ids_in,
)
from src.agent.tools import _requested_paper_ids
from src.services.identifiers import normalize_doi, normalize_pmid

CELL = "10.1016/S0092-8674(02)00722-5"
SICI = "10.1002/1097-0134(20010201)42:2<279::AID-PROT150>3.0.CO;2-U"
DOIS = [
    CELL, SICI, "10.1002/1521-3773(20020802)41:15<2840::AID-ANIE2840>3.0.CO;2-#",
    "10.1007/978-1-4939-7514-3_14", "10.1023/A:1023485632520", "10.1042/0264-6021:3380753",
]
WRAPPERS = [
    "https://doi.org/{}", "<https://doi.org/{}|label>", "<https://doi.org/{}>", "({})",
    "[t](https://doi.org/{})", "*{}*", "`{}`", "_{}_", "{}.", "{}?", "{}:", "{}!",
    "see {} and more", "doi:{}", "&lt;{}&gt;",
]


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("doi", DOIS)
def test_wrapper_matrix(doi, wrapper):
    assert extract_dois(wrapper.format(doi)) == {doi.lower()} == {canonical_doi(doi)}


def test_slack_escaped_sici():
    escaped = SICI.replace("<", "&lt;").replace(">", "&gt;")
    assert extract_dois(f"see {escaped} ok") == {SICI.lower()}


def test_url_encoded_doi():
    assert extract_dois("https://doi.org/10.1016%2FS0092-8674%2802%2900722-5") == {
        "10.1016/s0092-8674(02)00722-5"}
    assert extract_dois("10.1234%2Fabc%29") == {"10.1234/abc"}
    # Review Focus 1: the decoded-then-trimmed %29 stays inside the raw span.
    text = "x 10.1234%2Fabc%29 y"
    [c] = find_dois(text)
    assert text[c.start:c.end] == "10.1234%2Fabc%29"


@pytest.mark.parametrize("text", ["110.1234/abc", "v10.1234/abc", "10.1234/", "10.123/abc"])
def test_rejects_non_dois(text):
    assert extract_dois(text) == set()


def test_query_and_glued_dash():
    assert extract_dois("https://doi.org/10.1234/abc?utm_source=x") == {"10.1234/abc"}
    assert extract_dois("10.1234/abc—great") == {"10.1234/abc"}


def test_span_includes_the_prefix():
    for text, span in (("see https://doi.org/10.1234/abc.", "https://doi.org/10.1234/abc"),
                       ("x doi:10.1038/abc y", "doi:10.1038/abc")):
        [citation] = find_dois(text)
        assert text[citation.start:citation.end] == span


def test_pmid_forms():
    for text in ("PMID: 123", "pmid 00123", "PMID:123", "https://pubmed.ncbi.nlm.nih.gov/123/"):
        assert extract_pmids(text) == {"123"}, text
    assert extract_pmids("https://www.ncbi.nlm.nih.gov/pubmed/456") == {"456"}
    assert extract_pmids("ncbi.nlm.nih.gov/pubmed/789") == {"789"}
    assert extract_pmids("PMID: 0") == set()
    assert extract_pmids("PMID: 1234567890") == set()


def test_a_pmid_inside_a_doi_is_not_a_pmid():  # A-D3
    assert extract_pmids("10.1038/pmid12345") == set()
    assert paper_ids_in("10.1038/pmid12345") == {"10.1038/pmid12345"}


def test_a_sibling_paren_doi_is_not_the_labs_own_paper():  # A-D1
    agent = Agent("lab", "LabBot", "Lab PI")
    agent._public_profile = "https://doi.org/10.1016/S0092-8674(02)00722-5"
    assert agent.cites_own_paper(
        "…https://doi.org/10.1016/S0092-8674(02)01111-0 from the Smith lab") is False
    assert agent.cites_own_paper("see 10.1016/S0092-8674(02)00722-5.") is True


def test_slack_label_and_bold_dois():  # A-D2
    assert extract_dois("<https://doi.org/10.1038/s41586-024-07000-1|Nature 2024>") == {
        "10.1038/s41586-024-07000-1"}
    assert extract_dois("*10.1038/abc*") == {"10.1038/abc"}


def test_no_match_without_a_left_boundary():  # A-D4
    assert extract_dois("110.1234/x") == set()


AGREEMENT = [
    "https://www.doi.org/10.1234/x", "doi.org/10.1234/x", "dx.doi.org/10.1234/x", "10.1234/x,",
    "10.1234/x;", "10.1234%2Fx", "https%3A%2F%2Fdoi.org%2F10.1038%2FXYZ", "info:doi/10.1234/x",
    "DOI 10.1234/x", "doi:10.1234/x", "DOI: 10.1038/x", "https://doi.org/10.1038/x",
    "http://dx.doi.org/10.1038/x", "  10.1038/x  ", "10.1037/0033-2909.87.2.245.",
    "10.1042/BJ20141349", CELL, "10.1002/(SICI)1097-4636(199905)45:2<115::AID-JBM6>3.0.CO;2-B",
    *DOIS[1:], "N/A", "pending", "not a doi", "10.1038/",
]


@pytest.mark.parametrize("value", AGREEMENT)
def test_canonical_doi_agrees_with_normalize_doi(value):  # main ID-10
    assert len(AGREEMENT) == 27
    assert canonical_doi(value) == ((normalize_doi(value) or "").lower() or None)


@pytest.mark.parametrize("value, canonical, normalized", [
    ("10.123/abc", None, "10.123/abc"),
    ("10.1234/a b", "10.1234/a", "10.1234/a b"),
    ('10.1234/a"b', "10.1234/a", '10.1234/a"b'),
    ("10.1234/abc)", "10.1234/abc", "10.1234/abc)"),
    *[(f"10.1234/abc{c}", "10.1234/abc", f"10.1234/abc{c}") for c in ":?!*~_`"],
    ("10.1234/abc?utm=1", "10.1234/abc", "10.1234/abc?utm=1"),
])
def test_pinned_divergences(value, canonical, normalized):
    assert canonical_doi(value) == canonical
    assert normalize_doi(value) == normalized


@pytest.mark.parametrize("value", [
    "PMID: 31000000 ", "31000000", 31000000, "https://pubmed.ncbi.nlm.nih.gov/31000000/",
    "http://www.ncbi.nlm.nih.gov/pubmed/31000000", "0031000000", "abc", "0", "", None, True,
    "1234567890", "PMC1234567", "PMID: 0031000000", "pmid:7",
])
def test_canonical_pmid_agrees_with_normalize_pmid(value):
    assert canonical_pmid(value) == normalize_pmid(value)


def test_requested_paper_ids():
    assert _requested_paper_ids("0456") == {"456"}
    assert _requested_paper_ids("https%3A%2F%2Fdoi.org%2F10.1038%2FXYZ") == {"10.1038/xyz"}


def test_find_dois_is_linear():
    text = " ".join(f"10.{1000 + i}/abc{i}" for i in range(2000))
    started = time.perf_counter()
    assert len(find_dois(text)) == 2000
    assert time.perf_counter() - started < 1.0
