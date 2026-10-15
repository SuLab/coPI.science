"""Identifier normalisers shared by every ingest path: contract C1 of the 2026-10-15
publication-handling design (main SPEC ID-1, ID-2, ID-4, ID-5).

Pure: imports only ``re`` and ``urllib.parse`` (tests/unit/test_identifiers.py pins that).
Each function is defined here and nowhere else; ``pubmed.normalize_doi`` is this module's
function re-exported, and every ORCID parser that validates input delegates to
``normalize_orcid`` (R2.2)."""

import re
import urllib.parse

_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
#: One leading DOI prefix: "doi:", "doi " before "10.", "info:doi/" or a doi.org URL.
_DOI_PREFIX = re.compile(
    r"^(?:doi\s*:\s*|doi\s+(?=10\.)|info:doi/|(?:https?://)?(?:www\.)?(?:dx\.)?doi\.org/)",
    re.IGNORECASE,
)
_DOI_SHAPE = re.compile(r"^10\.[0-9.]+/\S")
_PMID = re.compile(
    r"(?:pmid\s*:?\s*|(?:https?://)?(?:www\.)?"
    r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/))?0*(\d{1,9})/?",
    re.IGNORECASE,
)
_ORCID_URL = re.compile(r"^(?:https?://)?(?:www\.)?orcid\.org/", re.IGNORECASE)
_ORCID_FORM = re.compile(r"(\d{4})[- ]?(\d{4})[- ]?(\d{4})[- ]?(\d{3}[0-9X])")
_BIORXIV_PREPRINT = re.compile(r"10\.1101/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{6,}(?:v\d+)?", re.IGNORECASE)
#: Preprint-server DOI prefixes (main ID-5), compared lower-cased.
_PREPRINT_PREFIXES = (
    "10.48550/arxiv.", "10.21203/rs.", "10.20944/preprints", "10.31234/", "10.31235/",
    "10.31219/osf.", "10.26434/chemrxiv", "10.2139/ssrn.", "10.22541/au.", "10.36227/techrxiv",
)


def normalize_doi(value: str | None) -> str | None:
    """A DOI with its prefix and trailing punctuation removed, case kept; None when
    ``value`` is not text, is blank, or is not DOI-shaped (``10.<digits>/<non-space>``).

    A ``%xx`` escape is decoded once, so a URL-encoded DOI reads as the DOI. ``)`` is never
    stripped: parentheses are legal DOI characters (SICI and Cell DOIs)."""
    if not isinstance(value, str) or not value.strip():
        return None
    doi = value.strip()
    if _PERCENT_ESCAPE.search(doi):
        doi = urllib.parse.unquote(doi)
    doi = _DOI_PREFIX.sub("", doi.strip(), count=1).strip()
    doi = doi.rstrip(" .,;")
    return doi if _DOI_SHAPE.match(doi) else None


def normalize_pmid(value: str | int | None) -> str | None:
    """The PMID's digits without leading zeros, from a bare number, an int, ``PMID: n`` or a
    PubMed URL; None for anything else, for 0, and for more than nine digits."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    match = _PMID.fullmatch(value.strip())
    if match is None or int(match.group(1)) == 0:
        return None
    return match.group(1)


def normalize_orcid(value: str | None) -> str | None:
    """The hyphenated ``NNNN-NNNN-NNNN-NNNC`` form of an ORCID iD, bare, hyphenless,
    single-space separated or as an orcid.org URL; None unless the ISO 7064 MOD 11-2
    check character matches (Q21). The single-space form is a blackbird adaptation of
    main ID-4: the PubMed parser accepted it before (R2.2)."""
    if not isinstance(value, str):
        return None
    text = _ORCID_URL.sub("", value.strip(), count=1)
    if text.endswith("/"):
        text = text[:-1]
    match = _ORCID_FORM.fullmatch(text.upper())
    if match is None:
        return None
    digits = "".join(match.groups())
    total = 0
    for char in digits[:15]:
        total = (total + int(char)) * 2
    check = (12 - total % 11) % 11
    if digits[15] != ("X" if check == 10 else str(check)):
        return None
    return "-".join(digits[i:i + 4] for i in range(0, 16, 4))


def is_preprint_doi(doi: str | None) -> bool:
    """Whether ``doi`` (any form ``normalize_doi`` reads) is a preprint server's DOI. CSHL
    journal DOIs under 10.1101 (``10.1101/gr.``, ``10.1101/gad.``) are not."""
    normalized = normalize_doi(doi)
    if normalized is None:
        return False
    return bool(_BIORXIV_PREPRINT.fullmatch(normalized)) or normalized.lower().startswith(
        _PREPRINT_PREFIXES
    )
