"""The deterministic half of founder discovery from a PI's own competing-interest
statements (spec §7.5 Step 1, O11, O14): the gate that decides whether a record is
sent to Claude at all (`coi_llm`), and the claim shape both sides share.

The attribution itself (which sentence's subject is the PI, and which company that
sentence says they founded) is Claude's (`coi_llm`, O14), after three adversarial
rounds measured the regex parser that used to live here at 26 false positives in 60
realistic statements. What stays here is what a model should not decide:

1. Locate the PI in the author list: folded surname equal (`company_sources.surname_keys`),
   the first initial equal, and the spelled-out given names agreeing.
2. Build the PI's initials letter-strings from that author's own `Initials` plus the
   surname initial ("VE" + "V" -> "VEV", and first initial + surname initial "VV"); the
   written forms VEV, V.E.V., V. E. V., VV and V.V. all reduce to those strings. Skip the
   record when the PI matches twice or any other author reduces to one of the same
   strings (PMID 37552989: Blair C and Bettegowda C are both "CB", so "C.B." in that
   statement could be either). When another author shares the PI's surname, only the
   initials forms name the PI ("X.J. Wang" is not Jing Wang); `PiForms.surname_shared`
   carries that to the prompt.
3. Send only a statement that mentions founding at all (`mentions_founding`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.services.company_sources import (
    PARTICLES,
    PiName,
    fold,
    given_names_agree,
    name_key,
    strip_accents,
    surname_keys,
)


def mentions_founding(statement: str) -> bool:
    """Whether the statement contains "found" in any case ("founder", "co-founded",
    "Cofounder"), the gate before any API call. Over-inclusive on purpose ("found no
    conflicts" passes): the model returns no claim for those; a statement without the
    word can carry no founder claim worth paying for."""
    return "found" in (statement or "").casefold()


@dataclass(frozen=True)
class FounderClaim:
    """One verified founder claim (`coi_llm.verify_claims`): `sentence` is the whole
    enclosing sentence of the original statement that states it and names the PI,
    `former` is set for a former or divested role."""

    company_name: str
    pi_role: str  # "founder" | "co_founder"
    pmid: str
    year: int | None
    sentence: str
    former: bool


@dataclass(frozen=True)
class PiForms:
    """How one record writes the PI: initials letter-strings plus the name pieces
    the full-name forms are built from. `surname_shared` is set when another author
    has the PI's surname; then only the initials forms name the PI."""

    letters: frozenset[str]
    first_names: tuple[str, ...]
    first_initial: str
    surname: str
    surname_shared: bool = False
    #: 1-based position of the PI among the record's author dicts (the order
    #: `coi_llm` lists them in), 0 when unknown.
    position: int = 0


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z]", "", strip_accents(text).upper())


def _surname_initials(last: str) -> set[str]:
    """Initial variants of an indexed surname: the first letter ("van 't Erve" -> V), the
    last token's ("E"), every token's ("VTE"), and every hyphen/apostrophe part's
    ("Hamacher-Brady" -> "HB", "O'Callaghan" -> "OC")."""
    folded = strip_accents(last).strip()
    if not folded:
        return set()
    tokens = [t for t in folded.split() if any(ch.isalpha() for ch in t)]
    out = {_letters(folded)[:1]}
    if tokens:
        out.add(_letters(tokens[-1])[:1])
        out.add("".join(_letters(t)[:1] for t in tokens))
    parts = [p for p in re.split(r"[\s\-'’]+", folded) if any(ch.isalpha() for ch in p)]
    out.add("".join(_letters(p)[:1] for p in parts))
    return {s for s in out if s}


def _author_initials(author: dict) -> str:
    inits = _letters(author.get("initials") or "")
    if inits:
        return inits
    fore = strip_accents(author.get("fore") or "")
    return "".join(_letters(p)[:1] for p in re.split(r"[\s\-]+", fore) if p)


def _author_letter_forms(author: dict) -> set[str]:
    inits = _author_initials(author)
    if not inits:
        return set()
    out: set[str] = set()
    for s in _surname_initials(_author_last(author)):
        out.add(inits + s)
        out.add(inits[0] + s)
    return out


def _author_last(author: dict) -> str:
    """LastName, with a particle PubMed stranded at the end of ForeName spliced back
    ("Neal" + "Anya J O'" -> "O'Neal")."""
    last = (author.get("last") or "").strip()
    fore_tokens = (author.get("fore") or "").split()
    if last and fore_tokens and fold(fore_tokens[-1]) in PARTICLES:
        particle = fore_tokens[-1]
        joiner = "" if particle.endswith(("'", "’")) else " "
        return f"{particle}{joiner}{last}"
    return last


def _author_surname_keys(author: dict) -> frozenset[str]:
    if author.get("collective"):
        return frozenset()
    return surname_keys(_author_last(author)) | surname_keys(author.get("last") or "")


def _author_given(author: dict) -> str:
    fore_tokens = (author.get("fore") or "").split()
    if fore_tokens and fold(fore_tokens[-1]) in PARTICLES:
        fore_tokens = fore_tokens[:-1]
    return fore_tokens[0] if fore_tokens else (author.get("initials") or "")[:1]


def _is_pi(author: dict, pi: PiName) -> bool:
    if author.get("collective") or not (author.get("last") or "").strip():
        return False
    if not (_author_surname_keys(author) & pi.surname_keys):
        return False
    given = _author_given(author)
    return bool(given) and given_names_agree(given, pi.first)


def locate_pi(record: dict, pi: PiName) -> PiForms | None:
    """The PI's forms on this record, or None when no author is the PI, more than one
    is, or another author reduces to one of the PI's initials strings."""
    authors = [a for a in (record.get("authors") or []) if isinstance(a, dict)]
    matches = [i for i, a in enumerate(authors) if _is_pi(a, pi)]
    if len(matches) != 1:
        return None
    me = authors[matches[0]]
    letters = _author_letter_forms(me)
    if not letters:
        return None
    others = [a for i, a in enumerate(authors) if i != matches[0]]
    if any(_author_letter_forms(other) & letters for other in others):
        return None
    mine = _author_surname_keys(me)
    given = _author_given(me)
    firsts = tuple(dict.fromkeys(f for f in (fold(pi.first), fold(given)) if len(name_key(f)) > 1))
    return PiForms(
        letters=frozenset(letters),
        first_names=firsts,
        first_initial=name_key(pi.first)[:1],
        surname=_author_last(me),
        surname_shared=any(_author_surname_keys(other) & mine for other in others),
        position=matches[0] + 1,
    )
