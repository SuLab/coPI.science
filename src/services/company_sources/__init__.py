"""Public sources for company discovery (spec §7.5): founder claims from a PI's own
PubMed competing-interest statements (gated by `coi_founders`, extracted by Claude in
`coi_llm`, spec O14), Wikidata "founded by" claims
by ORCID (`wikidata`), and SEC Form D funding (`sec_form_d`).

Nothing here imports the industry-evidence modules (tests/unit/test_enrichment_isolation.py)
or `industry_sources/pubmed_coi.py`, whose attribution is wrong (spec F13). This package
holds the name folding the three sources share, so a PI's name compares the same way
against a PubMed author list and a Form D related-person list (Review Focus #2).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


class SourceUnavailable(Exception):
    """A source could not answer (transport error, refused status, unreadable body,
    or missing configuration). The message is the short reason kept in evidence and
    logs; the caller turns it into a per-source note and carries on."""


# German umlaut/eszett expansion, applied to the ORIGINAL text only, so "Müller"
# yields both "muller" and "mueller" while ASCII "Muller" yields only "muller"
# (the asymmetry src/services/corpus.py `_surname_variants` documents).
_GERMAN = str.maketrans({"ü": "ue", "ö": "oe", "ä": "ae", "ß": "ss", "Ü": "Ue", "Ö": "Oe", "Ä": "Ae"})

# Surname particles, compared on the folded token. A PI's surname runs from the first
# particle after the given name ("Iris van 't Erve" -> "van 't Erve"); PubMed sometimes
# strands them at the end of ForeName ("Neal, Anya J O'").
PARTICLES = frozenset({
    "van", "von", "de", "del", "della", "der", "den", "di", "da", "du", "la", "le",
    "ter", "ten", "'t", "dos", "das", "do", "mc", "mac", "o'", "d'",
})

# Honorifics that Form D filers put into firstName ("Dr. Randall", C2N Diagnostics D/A
# 0002021597-24-000002) and that may precede a name in a COI sentence.
HONORIFICS = frozenset({"dr", "dr.", "prof", "prof.", "professor", "mr", "mr.", "ms", "ms.", "mrs", "mrs.", "sir"})


def strip_accents(text: str) -> str:
    """NFKD-decompose and drop combining marks, keeping case ("Müller" -> "Muller")."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold(text: str) -> str:
    """Accent-stripped and case-folded, punctuation and spacing kept."""
    return strip_accents(text).casefold().replace("’", "'")


def name_key(text: str) -> str:
    """`fold` with every non-alphanumeric character removed: "van 't Erve",
    "Van 't Erve" and "van't Erve" are one key; so are "Hamacher-Brady" and
    "Hamacher Brady"."""
    return re.sub(r"[^0-9a-z]", "", fold(text))


def surname_keys(surname: str) -> frozenset[str]:
    """Comparable keys of one surname: accent-stripped, and (only when the original
    carries ä/ö/ü/ß) German-expanded. Two surnames are equal when their key sets
    intersect."""
    raw = (surname or "").strip()
    if not raw:
        return frozenset()
    keys = {name_key(raw), name_key(raw.translate(_GERMAN))}
    return frozenset(k for k in keys if k)


def given_names_agree(a: str, b: str) -> bool:
    """First given names agree: equal first initials, and when both are spelled out
    (more than one letter), one is a prefix of the other after folding ("Bert" /
    "Bertram" agree; "Victor" / "Vlad" do not)."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb or ka[0] != kb[0]:
        return False
    if len(ka) == 1 or len(kb) == 1:
        return True
    return ka.startswith(kb) or kb.startswith(ka)


@dataclass(frozen=True)
class PiName:
    """The PI's name as `users.name` spells it: the given name (first token after any
    honorific) and every surname form that may be the indexed surname."""

    full: str
    first: str
    surname: str
    surname_keys: frozenset[str]


def pi_name(full_name: str) -> PiName | None:
    """Split `users.name` into a given name and candidate surnames. None when the name
    has fewer than two tokens (no surname to match on).

    Candidate surnames: the last token; the run from the first particle after the
    given name ("van 't Erve"); and, for three or more tokens, the last two tokens
    ("García Márquez"), since PubMed and Form D may index any of them."""
    tokens = [t for t in (full_name or "").split() if t]
    while tokens and fold(tokens[0]) in HONORIFICS:
        tokens = tokens[1:]
    if len(tokens) < 2:
        return None
    particle_run = next(
        (" ".join(tokens[i:]) for i in range(1, len(tokens)) if fold(tokens[i]) in PARTICLES), None
    )
    candidates = [tokens[-1]]
    if particle_run:
        candidates.append(particle_run)
    if len(tokens) >= 3:
        candidates.append(" ".join(tokens[-2:]))
    keys: set[str] = set()
    for candidate in candidates:
        keys |= surname_keys(candidate)
    return PiName(
        full=" ".join(tokens), first=tokens[0], surname=particle_run or tokens[-1],
        surname_keys=frozenset(keys),
    )


def strip_honorifics(given: str) -> str:
    """Drop leading honorific tokens from a given-name field ("Dr. Randall" -> "Randall")."""
    tokens = (given or "").split()
    while tokens and fold(tokens[0]) in HONORIFICS:
        tokens = tokens[1:]
    return " ".join(tokens)
