"""DOI and PMID extraction shared by the agent and the tool layer (spec §7.5; PMIDs: spec 2026-10-05 D49). Dependency-free."""

import re

# Matches a bare DOI. The character class deliberately excludes the delimiters
# that wrap DOIs in Slack posts (whitespace, quotes, angle brackets from
# <https://doi.org/...> links, and the ) ] that close markdown/parentheticals).
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]]+", re.IGNORECASE)


def extract_dois(text: str | None) -> set[str]:
    """Return the set of normalized DOIs found in ``text`` (lowercased)."""
    out: set[str] = set()
    for raw in _DOI_RE.findall(text or ""):
        out.add(raw.rstrip(".,;").lower())
    return out


# A PMID written as "PMID 123", "PMID: 123" or as a PubMed link.
_PMID_RE = re.compile(r"(?:\bPMID:?\s*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{1,9})\b", re.IGNORECASE)


def extract_pmids(text: str | None) -> set[str]:
    """Return the PMIDs written in ``text`` as ``PMID n``/``PMID: n`` or as a PubMed link,
    without leading zeros. Bare numbers are not PMIDs here."""
    return {str(int(raw)) for raw in _PMID_RE.findall(text or "")}


def paper_ids_in(text: str | None) -> set[str]:
    """DOIs (lowercased) and labelled PMIDs in ``text``; the two never collide (a DOI
    starts with ``10.``)."""
    return extract_dois(text) | extract_pmids(text)
