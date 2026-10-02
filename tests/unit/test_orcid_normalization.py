"""D-18: the ORCID forms people paste — an orcid.org URL, a lowercase check-digit
x — normalize to the canonical iD before validation."""

import pytest

from src.services.pi_onboarding import normalize_orcid, validate_orcid


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("  0000-0002-1825-0097\n", "0000-0002-1825-0097"),
        ("0000-0002-1694-233x", "0000-0002-1694-233X"),
        ("https://orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("http://orcid.org/0000-0002-1694-233x", "0000-0002-1694-233X"),
        ("orcid.org/0000-0002-1825-0097", "0000-0002-1825-0097"),
        ("HTTPS://WWW.ORCID.ORG/0000-0002-1825-0097", "0000-0002-1825-0097"),
    ],
)
def test_validate_orcid_accepts_the_forms_people_paste(raw, expected):
    assert validate_orcid(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "orcid.org/",
        "0000-0002-1825-009",
        "https://evil.example/0000-0002-1825-0097",
        "0000-0002-1825-0097/extra",
    ],
)
def test_validate_orcid_still_rejects_malformed_input(raw):
    with pytest.raises(ValueError, match="Invalid ORCID"):
        validate_orcid(raw)


def test_normalize_orcid_does_not_validate():
    assert normalize_orcid(" not-an-orcid ") == "not-an-orcid"
