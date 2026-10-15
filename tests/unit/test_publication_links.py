"""Publication links (spec 2026-10-15 §10 R5.12, R5.13; Q28, F22)."""
import re
from pathlib import Path

import pytest

from src.agent.dois import canonical_doi, extract_dois
from src.services.publication_links import doi_url, encode_doi, pubmed_url

ROOT = Path(__file__).resolve().parents[2]
SICI = "10.1002/(SICI)1097-4636(199601)30:1<1::AID-JBM1>3.0.CO;2-#"


def test_sici_doi_is_encoded():
    assert doi_url(SICI) == (
        "https://doi.org/10.1002/(SICI)1097-4636(199601)30:1%3C1::AID-JBM1%3E3.0.CO;2-%23")


def test_existing_escapes_are_not_double_encoded():
    assert encode_doi("10.1000/a%3Cb") == "10.1000/a%3Cb"
    assert encode_doi("10.1000/50%of") == "10.1000/50%25of"
    assert encode_doi("10.1000/a b") == "10.1000/a%20b"
    assert doi_url("https://doi.org/10.1000/a%3Cb") == "https://doi.org/10.1000/a%3Cb"


@pytest.mark.parametrize("doi", [SICI, "10.1000/a%3Cb", "10.1000/50%of", "10.1000/a b", "10.1000/ü"])
def test_encode_doi_is_idempotent(doi):
    assert encode_doi(encode_doi(doi)) == encode_doi(doi)


def test_doi_url_requires_a_doi_shaped_value():
    for value in (None, "", "  ", "N/A", "10.1234", "hello/world"):
        assert doi_url(value) is None, value
    assert doi_url("doi:10.1000/X") == "https://doi.org/10.1000/X"


@pytest.mark.parametrize(
    "doi", [SICI, "10.1016/S0092-8674(02)00722-5", "10.1093/bioinformatics/btad570"])
def test_agent_reads_back_encoded_persona_dois(doi):
    assert extract_dois(doi_url(doi)) == {canonical_doi(doi)}


def test_pubmed_url_requires_ascii_digits():
    assert pubmed_url("123") == "https://pubmed.ncbi.nlm.nih.gov/123/"
    assert pubmed_url(" 123 ") == "https://pubmed.ncbi.nlm.nih.gov/123/"
    for value in ("PMC123", "12a", "١٢٣", None, ""):
        assert pubmed_url(value) is None, value


def test_no_template_builds_a_raw_publication_link():
    """Every DOI and PubMed href goes through the doi_url/pubmed_url globals (R5.12, R5.13)."""
    pattern = re.compile(r"(?:pubmed\.ncbi\.nlm\.nih\.gov|doi\.org)/\{\{")
    offenders = [
        str(path.relative_to(ROOT)) for path in sorted((ROOT / "templates").rglob("*.html"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
