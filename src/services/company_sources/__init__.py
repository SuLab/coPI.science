"""Public sources for company discovery (spec §7.5): founder claims from a PI's own
PubMed competing-interest statements (gated by `coi_founders`, extracted by Claude in
`coi_llm`, spec O14), Wikidata "founded by" claims
by ORCID (`wikidata`), and SEC Form D funding (`sec_form_d`).

Nothing here imports the industry-evidence modules (tests/unit/test_enrichment_isolation.py)
or `industry_sources/pubmed_coi.py`, whose attribution is wrong (spec F13). This package
holds the name folding the three sources share, re-exported from
`src/services/person_names.py`, so a PI's name compares the same way against a PubMed
author list and a Form D related-person list (Review Focus #2).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.services.person_names import (
    HONORIFICS,
    PARTICLES,
    fold,
    given_names_agree,
    name_key,
    parse_person_name,
    strip_accents,
    strip_honorifics,
    surname_keys,
)

__all__ = [
    "HONORIFICS",
    "PARTICLES",
    "PiName",
    "SourceUnavailable",
    "fold",
    "given_names_agree",
    "name_key",
    "pi_name",
    "strip_accents",
    "strip_honorifics",
    "surname_keys",
]


class SourceUnavailable(Exception):
    """A source could not answer (transport error, refused status, unreadable body,
    or missing configuration). The message is the short reason kept in evidence and
    logs; the caller turns it into a per-source note and carries on."""


@dataclass(frozen=True)
class PiName:
    """The PI's name as `users.name` spells it: the given name (first token after any
    honorific) and every surname form that may be the indexed surname."""

    full: str
    first: str
    surname: str
    surname_keys: frozenset[str]


def pi_name(full_name: str) -> PiName | None:
    """Split `users.name` into a given name and candidate surnames
    (`person_names.parse_person_name`). None when the name has fewer than two tokens
    (no surname to match on).

    Candidate surnames: the last token; the run from the first particle after the
    given name ("van 't Erve"); and, for three or more tokens, the last two tokens
    ("García Márquez"), since PubMed and Form D may index any of them. A trailing
    suffix or comma degree ("Jr.", ", PhD") is not a candidate."""
    parsed = parse_person_name(full_name)
    if not parsed.display_surname:
        return None
    return PiName(
        full=parsed.full, first=parsed.first, surname=parsed.display_surname,
        surname_keys=parsed.surname_keys,
    )
