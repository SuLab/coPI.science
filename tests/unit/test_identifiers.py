"""C1 (src/services/identifiers.py), R2.1 of the 2026-10-15 publication-handling design."""
import ast
from pathlib import Path

import pytest

from src.services import identifiers, pubmed
from src.services.identifiers import is_preprint_doi, normalize_doi, normalize_orcid, normalize_pmid

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("raw, expected", [
    ("https://www.doi.org/10.1/x", "10.1/x"),
    ("doi.org/10.1/x", "10.1/x"),
    ("dx.doi.org/10.1/x", "10.1/x"),
    ("10.1/x,", "10.1/x"),
    ("10.1/x;", "10.1/x"),
    ("10.1%2Fx", "10.1/x"),
    ("https%3A%2F%2Fdoi.org%2F10.1038%2FXYZ", "10.1038/XYZ"),
    ("info:doi/10.1/x", "10.1/x"),
    ("DOI 10.1/x", "10.1/x"),
    ("DOI: 10.1038/x", "10.1038/x"),
    ("http://dx.doi.org/10.1038/x", "10.1038/x"),
    ("  10.1038/x  ", "10.1038/x"),
    ("10.1037/0033-2909.87.2.245.", "10.1037/0033-2909.87.2.245"),
    ("10.1042/BJ20141349", "10.1042/BJ20141349"),
    ("10.1016/S0092-8674(02)00722-5", "10.1016/S0092-8674(02)00722-5"),
    ("10.1016/S0092-8674(02)00722-5)", "10.1016/S0092-8674(02)00722-5)"),
    ("10.1002/(SICI)1097-4636(199905)45:2<115::AID-JBM6>3.0.CO;2-B",
     "10.1002/(SICI)1097-4636(199905)45:2<115::AID-JBM6>3.0.CO;2-B"),
])
def test_normalize_doi_vectors(raw, expected):
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize("raw", ["N/A", "pending", "not a doi", "10.1038/", None, "", "   "])
def test_normalize_doi_rejects_non_dois(raw):
    assert normalize_doi(raw) is None


@pytest.mark.parametrize("raw", [
    "PMID: 31000000 ", "31000000", 31000000, "https://pubmed.ncbi.nlm.nih.gov/31000000/",
    "http://www.ncbi.nlm.nih.gov/pubmed/31000000", "0031000000",
])
def test_normalize_pmid_vectors(raw):
    assert normalize_pmid(raw) == "31000000"


@pytest.mark.parametrize("raw", ["abc", "0", "", None, True, "1234567890", "PMC1234567"])
def test_normalize_pmid_rejects(raw):
    assert normalize_pmid(raw) is None


@pytest.mark.parametrize("raw, expected", [
    ("0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("0000000218250097", "0000-0002-1825-0097"),
    ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
    ("HTTPS://WWW.ORCID.ORG/0000-0002-1825-0097/", "0000-0002-1825-0097"),
    ("0000 0002 1825 0097", "0000-0002-1825-0097"),
    ("0000-0002-1694-233x", "0000-0002-1694-233X"),
    ("0000-0003-1918-106X", "0000-0003-1918-106X"),
    ("0000-0002-1825-0098", None),
    ("0000-0002-1825-009", None),
    ("0000-0002-1825-0097/extra", None),
    ("https://evil.example/0000-0002-1825-0097", None),
    ("orcid.org/", None),
    ("", None),
    (None, None),
    ("SPARSE-ABCDEF12", None),
])
def test_normalize_orcid_vectors(raw, expected):
    assert normalize_orcid(raw) == expected


@pytest.mark.parametrize("doi, expected", [
    ("10.1101/2023.05.01.538993", True), ("10.1101/564187", True),
    ("10.1101/2021.09.02.458726v2", True), ("10.48550/ARXIV.2605.30283", True),
    ("10.21203/rs.3.rs-123/v1", True), ("https://doi.org/10.1101/729475", True),
    ("10.1101/gr.1234567", False), ("10.1101/gad.123456.110", False),
    ("10.1101/pdb.prot5439", False), ("10.1093/bioinformatics/btag008", False), (None, False),
])
def test_is_preprint_doi_vectors(doi, expected):
    assert is_preprint_doi(doi) is expected


def test_identifiers_imports_only_stdlib():
    tree = ast.parse((ROOT / "src/services/identifiers.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {"re", "urllib.parse"}


def test_pubmed_reexports_normalize_doi():
    assert pubmed.normalize_doi is identifiers.normalize_doi
