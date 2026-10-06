"""Per-destination escaping for the external searches a person's name or a stored id
reaches (spec 2026-10-05 §4.1, P29). Allowlists, not blocklists: kept is what a name can
legitimately hold (D60: letters of any script with their combining marks, spaces and
`. , - ' ’`) plus digits; every other character (quotes, backslashes, brackets, field and
boolean syntax, control and format characters) becomes a space. A query builder then puts
the result inside its own quotes, so the text can neither close the phrase nor add a
clause.

- USPTO ODP and ClinicalTrials.gov phrase queries: `phrase_term`.
- PubMed `[Author]` terms: `pubmed_author_term` (also drops "," and the bare operators
  AND, OR, NOT, which PubMed reads as boolean between words).
- OpenAlex filter values: `openalex_filter_value` (ASCII letters, digits and "-" only:
  "," separates filters, "|" ORs values, "!" negates, ":" starts a value)."""
from __future__ import annotations

import re
import unicodedata

_PHRASE_PUNCT = frozenset(" .,-'’")
_PUBMED_OPERATORS = frozenset({"AND", "OR", "NOT"})
_OPENALEX_UNSAFE = re.compile(r"[^0-9A-Za-z-]")


def _kept(ch: str) -> bool:
    return unicodedata.category(ch)[0] in ("L", "M", "N") or ch in _PHRASE_PUNCT


def phrase_term(text: str | None) -> str:
    """`text` (NFC) with every character outside the allowlist turned into a space and
    whitespace collapsed; "" when nothing is left."""
    normalized = unicodedata.normalize("NFC", text or "")
    return " ".join("".join(ch if _kept(ch) else " " for ch in normalized).split())


def pubmed_author_term(text: str | None) -> str:
    """`phrase_term` without commas and without the words AND, OR and NOT."""
    words = phrase_term(text).replace(",", " ").split()
    return " ".join(w for w in words if w not in _PUBMED_OPERATORS)


def openalex_filter_value(value: str | None) -> str:
    """`value` with everything but ASCII letters, digits and "-" removed."""
    return _OPENALEX_UNSAFE.sub("", value or "")
