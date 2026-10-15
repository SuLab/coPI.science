"""Outbound links to a publication, built in one place for the persona and every template
(main SPEC EXP-4, EXP-5, EXP-10; Q28 and F22 of the 2026-10-15 publication-handling design).

A DOI link is built only for a DOI-shaped value (``identifiers.normalize_doi``). It is
percent-encoded with main EXP-4's safe set, and any ``%xx`` escape already present is kept,
so encoding is idempotent and a stored escaped DOI is never encoded twice. A PubMed link is
built only for an all-ASCII-digit PMID. A row with neither gets no link (F22). Pure:
imports only the stdlib and ``identifiers``.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from src.services.identifiers import normalize_doi

#: Characters a DOI keeps unescaped in a doi.org URL (main SPEC EXP-4).
DOI_URL_SAFE = "-._~!$'()*+;=:@/"
#: An escape already present in a stored DOI; kept verbatim (Q28).
_ESCAPE_RE = re.compile(r"(%[0-9A-Fa-f]{2})")
_DOI_RESOLVER = "https://doi.org/"
_PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{}/"


def encode_doi(doi: str) -> str:
    """``doi`` percent-encoded for a URL path: every ``%xx`` escape is kept, and every other
    character outside ``DOI_URL_SAFE`` and the ASCII letters and digits is encoded as UTF-8
    (a lone ``%`` becomes ``%25``). Idempotent."""
    parts = _ESCAPE_RE.split(doi)
    return "".join(
        part if i % 2 else quote(part, safe=DOI_URL_SAFE) for i, part in enumerate(parts)
    )


def doi_url(doi: object) -> str | None:
    """The doi.org URL of a stored DOI, or None when it is empty or not DOI-shaped (F22).
    ``normalize_doi`` strips a ``doi:`` or doi.org prefix and decodes ``%xx`` once; the
    result is encoded with ``encode_doi``."""
    normalized = normalize_doi(str(doi)) if doi else None
    if normalized is None:
        return None
    return _DOI_RESOLVER + encode_doi(normalized)


def pubmed_url(pmid: object) -> str | None:
    """The PubMed page of a PMID made only of ASCII digits (surrounding blanks ignored),
    else None."""
    value = str(pmid or "").strip()
    if value.isascii() and value.isdigit():
        return _PUBMED_URL.format(value)
    return None
