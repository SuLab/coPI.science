"""The persona's publication links (spec 2026-10-05 §6.3, P28, G-19). Links are asserted
with ``in``: the Recent Publications line appends ``(PMID n)`` after the link (D49)."""
import pytest

from src.models import Publication
from src.services.profile_export import _citation, _publication_lines, _validate_doi_journal
from src.services.tenure_scope import TenureScopedPublications

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


SICI = "10.1002/(SICI)1097-4636(199601)30:1<1::AID-JBM1>3.0.CO;2-#"


def _lines(*rows):
    return _publication_lines(TenureScopedPublications(publications=rows, tenure_start=None))


def test_persona_sici_link_is_encoded():
    text = _citation(_pub(pmid=None, doi=SICI, journal="Journal of Biomedical Materials Research"))
    assert text.endswith("%3E3.0.CO;2-%23")


def test_persona_pmidless_row_with_non_doi_gets_no_link():
    lines = _lines(_pub(title="T", journal="J", year=2020, pmid=None, doi="N/A"))
    assert "- T. *J*. (2020)." in lines
    assert not any("http" in line for line in lines)


def test_persona_skips_link_for_non_numeric_pmid():
    lines = _lines(_pub(pmid="PMC1", doi="10.1126/science.aaa1234", doi_verified=True))
    item = next(line for line in lines if line.startswith("- "))
    assert "http" not in item and "(PMID" not in item


@pytest.mark.parametrize(("doi", "journal", "expected"), [
    ("10.1101/mcs.a001234", "Cold Spring Harbor molecular case studies", True),
    ("10.1109/JLT.2020.1", "Journal of Lightwave Technology", True),
    ("10.1016/j.cellsig.2020.1", "Journal of X", True),
    ("10.1101/gr.123.4", "Genome Research", True),
    ("10.1101/gr.123.4", "bioRxiv", False),
    ("10.1101/2020.01.01.123456", "bioRxiv", True),
    ("10.1101/2020.01.01.123456", "Cell", False),
    ("10.1016/j.cell.2020.01.001", "Cell", True),
])
def test_journal_prefix_requires_a_non_letter_boundary(doi, journal, expected):
    assert _validate_doi_journal(doi, journal) is expected
