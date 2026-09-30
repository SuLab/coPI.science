"""DOI extraction shared by the agent and the tool layer (spec §7.5). Dependency-free."""

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
