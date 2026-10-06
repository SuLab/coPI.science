"""The persona's publication links (spec 2026-10-05 §6.3, P28, G-19). Links are asserted
with ``in``: the Recent Publications line appends ``(PMID n)`` after the link (D49)."""
from src.models import Publication
from src.services.profile_export import _citation, _validate_doi_journal

DOI_LINK = " https://doi.org/10.1016/j.cell.2020.01.001"
PUBMED_LINK = " https://pubmed.ncbi.nlm.nih.gov/123/"


def _pub(**kw):
    base = dict(title="A paper", journal="Cell", year=2020, pmid="123",
                doi="10.1016/j.cell.2020.01.001", doi_verified=None)
    base.update(kw)
    return Publication(**base)


def test_a_verified_doi_is_linked():
    text = _citation(_pub(doi_verified=True))
    assert DOI_LINK in text and "pubmed" not in text


def test_an_unverified_doi_gives_way_to_pubmed():
    for flag in (None, False):
        text = _citation(_pub(doi_verified=flag))
        assert PUBMED_LINK in text and "doi.org" not in text


def test_a_verified_doi_that_contradicts_the_journal_gives_way_to_pubmed():
    text = _citation(_pub(doi="10.1126/science.aaa1234", doi_verified=True))
    assert PUBMED_LINK in text and "doi.org" not in text


def test_a_pmidless_row_keeps_a_fitting_doi_and_loses_a_contradicting_one():
    assert DOI_LINK in _citation(_pub(pmid=None))
    bad = _citation(_pub(pmid=None, doi="10.1126/science.aaa1234"))
    assert "http" not in bad and bad.endswith("(2020).")


def test_cold_spring_harbor_protocols_dois_fit_their_journal():
    assert _validate_doi_journal("10.1101/pdb.prot5079", "Cold Spring Harbor protocols")
    assert not _validate_doi_journal("10.1101/pdb.prot5079", "bioRxiv")
