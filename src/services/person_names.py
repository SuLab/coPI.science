"""One parser for a person's name (spec 2026-10-05 §4.1; D22, D60).

It merges the repo's two copies: ``company_sources.pi_name`` (honorifics, surname
particles, multi-token surname candidates, accent and German folding,
``given_names_agree``) and ``agent_identity._surname`` (Jr./Sr./III/PhD suffixes, Roman
numerals only in capitals: "Ii" is a surname, "II" a generation). It also drops comma
degrees ("Jane Doe, PhD") and recognises a "name" that is an ORCID iD or holds no letter.

``slug_surname`` reproduces ``agent_identity._surname`` exactly, so agent slugs never
change; ``display_surname`` is what ``company_sources.pi_name`` called the surname, except
that a trailing suffix or comma degree is no longer taken for one.

Adoption: RePORTER identity (Phase 1); COI, USPTO and ClinicalTrials.gov (Phase 2); the
corpus gate (Phase 3). ``sanitize_person_name`` (ORCID- or OAuth-sourced names) is used,
through ``name_from_machine_source``, at login, Add-PI and pipeline step 1 from Phase 1;
``validate_person_name`` (human-entered names, D60) is adopted by the edit, Add-PI,
onboarding and CLI paths in Phase 4.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# German umlaut/eszett expansion, applied to the ORIGINAL text only, so "Müller"
# yields both "muller" and "mueller" while ASCII "Muller" yields only "muller"
# (the asymmetry src/services/corpus.py `_surname_variants` documents).
_GERMAN = str.maketrans(
    {"ü": "ue", "ö": "oe", "ä": "ae", "ß": "ss", "Ü": "Ue", "Ö": "Oe", "Ä": "Ae"}
)

# Surname particles, compared on the folded token. A PI's surname runs from the first
# particle after the given name ("Iris van 't Erve" -> "van 't Erve"); PubMed sometimes
# strands them at the end of ForeName ("Neal, Anya J O'").
PARTICLES = frozenset({
    "van", "von", "de", "del", "della", "der", "den", "di", "da", "du", "la", "le",
    "ter", "ten", "'t", "dos", "das", "do", "mc", "mac", "o'", "d'",
})

# Honorifics that Form D filers put into firstName ("Dr. Randall", C2N Diagnostics D/A
# 0002021597-24-000002) and that may precede a name in a COI sentence.
HONORIFICS = frozenset({
    "dr", "dr.", "prof", "prof.", "professor", "mr", "mr.", "ms", "ms.", "mrs", "mrs.", "sir",
})

# Latin letters NFKD does not decompose into an ASCII base letter.
_ASCII_LETTERS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "đ": "d", "Đ": "D", "ð": "d", "Ð": "D", "ł": "l", "Ł": "L", "þ": "th",
    "Þ": "Th", "ı": "i",
})

# Generational and degree suffixes an ORCID family-name field can carry
# ("Smith Jr.", "Jones III", "Picard PhD"). Roman numerals count only in capitals:
# "Ii" is a surname (Naoki Ii), "II" is a generation.
_NAME_SUFFIXES = frozenset({
    "jr", "sr", "phd", "md", "mph", "dphil", "dds", "dvm", "msc", "mba", "facs",
    "frs", "esq",
})
_ROMAN_SUFFIXES = frozenset({"II", "III", "IV"})

# Slack caps an app name at 35 characters; agent_identity builds bot names from this
# many surname characters (see its _DISPLAY_MAX comment).
SLUG_SURNAME_MAX = 28
NAME_MAX_CHARS = 100
_ORCID_ID = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
# D60 punctuation allowed besides letters, combining marks and Unicode dashes (Pd).
_EXTRA_ALLOWED = frozenset(" .,'’")


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


def strip_honorifics(given: str) -> str:
    """Drop leading honorific tokens from a given-name field ("Dr. Randall" -> "Randall")."""
    tokens = (given or "").split()
    while tokens and fold(tokens[0]) in HONORIFICS:
        tokens = tokens[1:]
    return " ".join(tokens)


def ascii_letters(text: str) -> str:
    """``text`` folded to ASCII with every character that is not an ASCII letter or
    digit dropped: "Müller" -> "Muller", "O'Brien" -> "OBrien"."""
    folded = unicodedata.normalize("NFKD", text.translate(_ASCII_LETTERS))
    return "".join(c for c in folded if c.isascii() and c.isalnum())


def _is_suffix(word: str) -> bool:
    return word.lower() in _NAME_SUFFIXES or word in _ROMAN_SUFFIXES


def slug_surname(full_name: str) -> str:
    """The last word of ``full_name`` that can be a surname, as ASCII letters (the agent
    slug's stem): a word holding a digit (an ORCID iD standing in for a private name)
    and a trailing generational or degree suffix are skipped, the last remaining word
    never. Empty when no word qualifies."""
    words = [ascii_letters(w) for w in (full_name or "").split()]
    words = [w for w in words if w and not any(c.isdigit() for c in w)]
    while len(words) > 1 and _is_suffix(words[-1]):
        words.pop()
    return words[-1][:SLUG_SURNAME_MAX] if words else ""


def is_orcid_like(text: str | None) -> bool:
    """An ORCID iD, or a non-empty text with no letter at all ("1234 5678")."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(_ORCID_ID.fullmatch(stripped)) or not any(c.isalpha() for c in stripped)


def _strip_comma_degrees(text: str) -> str:
    """'Jane Doe, PhD' / 'Jane Doe, M.D., Ph.D.' -> 'Jane Doe'. Kept as is when any part
    after a comma is not made of suffixes ('Doe, Jane' is not rewritten)."""
    head, *tail = text.split(",")
    if tail and all(
        part.strip() and all(_is_suffix(ascii_letters(w)) for w in part.split())
        for part in tail
    ):
        return head
    return text


def _tokens(full_name: str | None) -> list[str]:
    """NFC tokens without comma degrees, leading honorifics or trailing suffixes (a
    suffix is dropped only while more than two tokens remain)."""
    text = _strip_comma_degrees(unicodedata.normalize("NFC", full_name or ""))
    tokens = text.split()
    while tokens and fold(tokens[0]) in HONORIFICS:
        tokens = tokens[1:]
    while len(tokens) > 2 and _is_suffix(ascii_letters(tokens[-1])):
        tokens.pop()
    return tokens


def _particle_run(tokens: list[str]) -> str | None:
    return next(
        (" ".join(tokens[i:]) for i in range(1, len(tokens)) if fold(tokens[i]) in PARTICLES),
        None,
    )


def surname_candidates(full_name: str | None) -> tuple[str, ...]:
    """Every form that may be the indexed surname: the last token; the run from the first
    particle after the given name ("van 't Erve"); and for three or more tokens the last
    two ("García Márquez"). Empty with fewer than two tokens, and for an ORCID iD or a
    letter-less "name" ("1234 5678" must not match a RePORTER surname "5678")."""
    if is_orcid_like(full_name):
        return ()
    tokens = _tokens(full_name)
    if len(tokens) < 2:
        return ()
    out = [tokens[-1]]
    run = _particle_run(tokens)
    if run and run not in out:
        out.append(run)
    if len(tokens) >= 3 and " ".join(tokens[-2:]) not in out:
        out.append(" ".join(tokens[-2:]))
    return tuple(out)


@dataclass(frozen=True)
class PersonName:
    """A parsed name. ``display_surname`` is "" with fewer than two tokens;
    ``surname_keys`` is empty for an ORCID iD or a letter-less name."""

    full: str  # tokens after honorifics, comma degrees and trailing suffixes; NFC
    first: str  # first token ("" when none)
    surname_keys: frozenset[str]  # keys of every surname candidate
    display_surname: str  # what company_sources.pi_name reports as the surname
    slug_surname: str  # agent_identity._surname(full_name), exactly
    is_orcid_id: bool  # an ORCID iD, or no letter at all


def parse_person_name(full_name: str | None) -> PersonName:
    """``full_name`` split into its given name, surname candidates and slug stem."""
    tokens = _tokens(full_name)
    keys: set[str] = set()
    for candidate in surname_candidates(full_name):
        keys |= surname_keys(candidate)
    display = (_particle_run(tokens) or tokens[-1]) if len(tokens) >= 2 else ""
    return PersonName(
        full=" ".join(tokens),
        first=tokens[0] if tokens else "",
        surname_keys=frozenset(keys),
        display_surname=display,
        slug_surname=slug_surname(full_name or ""),
        is_orcid_id=is_orcid_like(full_name),
    )


def _allowed_char(ch: str) -> bool:
    category = unicodedata.category(ch)
    return category[0] in ("L", "M") or category == "Pd" or ch in _EXTRA_ALLOWED


def _has_letter(text: str) -> bool:
    return any(unicodedata.category(c)[0] == "L" for c in text)


class InvalidPersonName(ValueError):
    """A human-entered name outside the D60 allowlist; str(exc) is the form message."""


def validate_person_name(name: str, *, previous: str | None = None) -> str:
    """The NFC form of a human-entered name; raises InvalidPersonName outside the D60
    allowlist (letters of any script and combining marks, space, ``. , - ' ’``, Unicode
    dash punctuation; at most NAME_MAX_CHARS; at least one letter). A name equal (after
    NFC and strip) to ``previous`` is returned unchecked: D60 validates only a changed
    name."""
    normalised = unicodedata.normalize("NFC", (name or "").strip())
    if previous is not None and normalised == unicodedata.normalize("NFC", previous.strip()):
        return normalised
    if not normalised:
        raise InvalidPersonName("A name is required.")
    if len(normalised) > NAME_MAX_CHARS:
        raise InvalidPersonName(f"Names are limited to {NAME_MAX_CHARS} characters.")
    bad = sorted({ch for ch in normalised if not _allowed_char(ch)})
    if bad:
        raise InvalidPersonName(
            "Names may contain letters, spaces, hyphens, apostrophes, periods and commas; "
            "remove " + " ".join(repr(c) for c in bad) + "."
        )
    if not _has_letter(normalised):
        raise InvalidPersonName("A name needs at least one letter.")
    return normalised


@dataclass(frozen=True)
class SanitizedName:
    name: str
    changed: bool  # characters were removed or the text was cut (the D60 manager flag)


def sanitize_person_name(raw: str | None) -> SanitizedName | None:
    """A machine-sourced name cut to the D60 allowlist and NAME_MAX_CHARS, or None for an
    ORCID iD, a letter-less text or nothing left (the caller keeps today's fallback,
    spec §4.1). Collapsing whitespace alone is not a change."""
    text = unicodedata.normalize("NFC", raw or "")
    if is_orcid_like(text):
        return None
    collapsed = " ".join(text.split())
    cleaned = " ".join("".join(ch if _allowed_char(ch) else " " for ch in text).split())
    cleaned = cleaned[:NAME_MAX_CHARS].strip()
    if not _has_letter(cleaned):
        return None
    return SanitizedName(name=cleaned, changed=cleaned != collapsed)


_ASCII_DIGITS = frozenset("0123456789")


def _letterless_fallback(raw: str) -> SanitizedName:
    """``raw`` (an iD or a letter-less text) whitespace-collapsed and cut to the D60
    allowlist plus ASCII digits, at most NAME_MAX_CHARS: an ORCID iD survives intact, so
    ``is_orcid_like`` still recognises it, while newlines and markdown never get stored."""
    text = unicodedata.normalize("NFC", raw)
    collapsed = " ".join(text.split())
    cleaned = " ".join(
        "".join(ch if _allowed_char(ch) or ch in _ASCII_DIGITS else " " for ch in text).split()
    )
    cleaned = cleaned[:NAME_MAX_CHARS].strip()
    return SanitizedName(name=cleaned, changed=cleaned != collapsed)


def name_from_machine_source(raw: str | None) -> tuple[str | None, bool]:
    """(name to store, was it cut?) for an ORCID- or OAuth-sourced name (D60): the
    sanitised name, or, for an iD or a letter-less text (today's fallback, until Phase
    3's refusal and repair), ``raw`` with whitespace collapsed and only allowlisted
    characters and digits kept. An iD wrapped in stray symbols or a trailing newline
    also takes the fallback, which keeps the iD, rather than the sanitiser, which drops
    its digits. None stays None; a text with nothing left becomes ""."""
    if raw is None:
        return None, False
    fallback = _letterless_fallback(raw)
    cleaned = None if is_orcid_like(fallback.name) else sanitize_person_name(raw)
    chosen = cleaned or fallback
    return chosen.name, chosen.changed
