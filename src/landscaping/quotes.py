"""The deterministic quote check on S3 and S6 extractions (spec 2026-10-30 §7.4 step 3).

A quote counts only when it appears in the stored text after Unicode NFKC folding,
whitespace collapse and case folding. Failed quotes are dropped and counted; a Target Rubric
item left without a verified quote becomes ``unclear``, so S4 sees only verified quotes
(anti-goal F13).
"""
from __future__ import annotations

import copy
import unicodedata

#: Typographic quote marks NFKC leaves alone, folded to their ASCII forms so a model that
#: straightens a curly quote (or curls a straight one) still matches the text.
_QUOTE_MARKS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
})
#: Per-idea lists whose entries each carry one ``quote``; an entry without a verified quote
#: is dropped whole.
_QUOTED_LISTS = ("key_results", "novelty_claims", "limitations", "unmet_need_statements")
#: The identifiers of a ``linked_publications`` entry, each of which must appear verbatim
#: in the document's text (D67).
_LINK_FIELDS = ("doi", "pmid", "title")


def fold_text(text: str) -> str:
    """``text`` NFKC-folded, with typographic quote marks made ASCII, case-folded, and
    every whitespace run collapsed to one space, trimmed."""
    folded = unicodedata.normalize("NFKC", text).translate(_QUOTE_MARKS).casefold()
    return " ".join(folded.split())


def _contains(quote: object, folded_text: str) -> bool:
    if not isinstance(quote, str):
        return False
    folded = fold_text(quote)
    return bool(folded) and folded in folded_text


def verify_quote(quote: str, text: str) -> bool:
    """Whether ``quote`` appears in ``text`` once both are folded by ``fold_text``.

    An empty or whitespace-only quote is never verified.
    """
    return _contains(quote, fold_text(text))


class _Tally:
    """Counts of non-empty quotes checked: verified and dropped."""

    def __init__(self) -> None:
        self.verified = 0
        self.dropped = 0

    def check(self, quote: object, folded_text: str) -> bool:
        """Whether ``quote`` is verified; an empty quote is neither verified nor counted."""
        if not isinstance(quote, str) or not quote.strip():
            return False
        if _contains(quote, folded_text):
            self.verified += 1
            return True
        self.dropped += 1
        return False


def _rubric_item(item: dict, folded_text: str, tally: _Tally) -> dict:
    if tally.check(item.get("quote"), folded_text):
        return item
    return {**item, "status": "unclear", "grade": None, "quote": ""}


def _link_verified(entry: object, folded_text: str) -> bool:
    if not isinstance(entry, dict):
        return False
    given = [entry.get(field) for field in _LINK_FIELDS if entry.get(field)]
    return bool(given) and all(_contains(value, folded_text) for value in given)


def apply_quote_check(extraction: dict, text: str) -> tuple[dict, int, int]:
    """Return ``(cleaned, verified, dropped)`` for one extraction against its stored text.

    ``extraction`` is the extract prompt's output (``ideas`` and ``linked_publications``)
    and is not mutated. In ``cleaned``:
    - each ``target_rubric`` item whose quote is empty or unverified has ``status``
      ``unclear``, ``grade`` None and an empty quote, whatever status the model gave;
    - each entry of ``key_results``, ``novelty_claims``, ``limitations`` and
      ``unmet_need_statements`` without a verified quote is removed;
    - each ``linked_publications`` entry is removed unless every non-empty ``doi``,
      ``pmid`` and ``title`` it gives appears in the text (D67).

    ``verified`` and ``dropped`` count the non-empty quotes checked; empty quotes and linked
    publications are not counted.
    """
    cleaned = copy.deepcopy(extraction)
    folded_text = fold_text(text)
    tally = _Tally()
    for idea in cleaned.get("ideas") or []:
        if not isinstance(idea, dict):
            continue
        idea["target_rubric"] = [
            _rubric_item(item, folded_text, tally)
            for item in idea.get("target_rubric") or []
            if isinstance(item, dict)
        ]
        for field in _QUOTED_LISTS:
            idea[field] = [
                entry for entry in idea.get(field) or []
                if isinstance(entry, dict) and tally.check(entry.get("quote"), folded_text)
            ]
    if "linked_publications" in cleaned:
        cleaned["linked_publications"] = [
            entry for entry in cleaned.get("linked_publications") or []
            if _link_verified(entry, folded_text)
        ]
    return cleaned, tally.verified, tally.dropped
