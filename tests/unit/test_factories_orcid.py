"""tests.factories.valid_orcid (R2.2): unique and checksum-valid."""
from src.services.identifiers import normalize_orcid
from tests.factories import valid_orcid


def test_valid_orcid_helper_is_unique_and_valid():
    values = [valid_orcid(n) for n in range(5000)]
    assert len(set(values)) == 5000
    assert all(normalize_orcid(value) == value for value in values)
