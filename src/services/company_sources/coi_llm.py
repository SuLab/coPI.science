"""Founder claims from a PI's own competing-interest statement, extracted by Claude
(spec §7.5 "Step 1, extraction", O14) and verified by code.

Per PubMed record: the deterministic gate in `coi_founders` (the PI located with no
initials collision, a statement that mentions founding) decides whether a call is made
at all; a gated-out record is "skipped" and costs nothing. A gated-in record is one
`llm.abeta_create` call with structured output, the server-side refusal fallback and
the rules in `prompts/company-discovery-coi.md`. Every untrusted value (author names,
the PI block, the statement) has "<" and ">" escaped (`_escape`) and is then fenced with
`prompt_safety.delimit`, so a forged tag can neither close a fence nor open a new one.

The model's answer is never trusted as is: `verify_claims` keeps a claim only when its
quote occurs in the statement within one sentence, the company occurs in that sentence
as a whole word, the PI is named in the same clause before a founder phrase that
precedes the company with no other author named in between (`_Statement.names_pi`), the
role is founder/co_founder and `pi_companies._clean_name` accepts the name; at most
`MAX_CLAIMS_PER_RECORD` survive. Claims become manager-confirmed suggestions (O9), so a
dropped claim is a miss, never an error.

A refusal, an API error, a truncated or malformed reply, or a missing prompt file marks
the record "unavailable" with a short reason; nothing here raises for an upstream
problem (the job notes "coi: N of M disclosures unavailable" and carries on).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import anthropic

from src.agent.prompt_safety import delimit
from src.config import get_settings
from src.services import llm
from src.services.company_sources import PiName, fold, name_key, strip_accents
from src.services.company_sources.coi_founders import (
    FounderClaim,
    PiForms,
    _author_given,
    _author_last,
    _author_letter_forms,
    locate_pi,
    mentions_founding,
)
from src.services.pi_companies import CompanyValidationError, _clean_name, normalize_company_name

logger = logging.getLogger(__name__)

COI_PROMPT_PATH = "prompts/company-discovery-coi.md"
#: Records sent to the model per PI per discovery run (spec §7.5); the job enforces it.
MAX_COI_CALLS_PER_PI = 60
#: Verified claims kept per record; the rest count as dropped. A disclosure that credits
#: one PI with more founded companies than this is more likely a misread than a record.
MAX_CLAIMS_PER_RECORD = 5

#: The `fallbacks="default"` scalar form needs exactly this header; the older
#: `-2026-06-01` header gates the array form, and pairing either with the other is a 400.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ROLES = frozenset({"founder", "co_founder"})

CLAIMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string", "enum": ["founder", "co_founder"]},
                    "former": {"type": "boolean"},
                    "sentence": {"type": "string"},
                },
                "required": ["company", "role", "former", "sentence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class CoiOutcome:
    """One record's result. "skipped": the gate refused it, no call was made.
    "unavailable": the call was made (or attempted) and gave nothing usable; `reason`
    says why. "ok": `claims` are the verified ones (possibly none) and `dropped` counts
    the model's claims the verifier rejected or the per-record cap cut.

    `usage` is the reply's token counts (`input_tokens`, `output_tokens`,
    `cache_read_input_tokens`, `cache_creation_input_tokens`; a count the reply leaves
    unset is 0), so the job can report spend. None when no reply came back: a skipped
    record, a missing prompt, an API error, or a reply without usage."""

    status: Literal["ok", "skipped", "unavailable"]
    claims: list[FounderClaim] = field(default_factory=list)
    reason: str | None = None
    dropped: int = 0
    usage: dict[str, int] | None = None


# --- request ------------------------------------------------------------------


def _initials_forms(letters: frozenset[str]) -> list[str]:
    """Every written form of each initials string: "VEV", "V.E.V.", "V. E. V."."""
    out: list[str] = []
    for s in sorted(letters, key=lambda x: (-len(x), x)):
        out += [s, ".".join(s) + ".", ". ".join(s) + "."]
    return list(dict.fromkeys(out))


def _author_line(n: int, author: dict) -> str:
    if author.get("collective"):
        return f"{n}. group author: {author['collective']}"
    return (
        f"{n}. surname: {author.get('last') or ''}; forenames: {author.get('fore') or ''}; "
        f"initials: {author.get('initials') or ''}"
    )


def _pi_block(pi: PiName, forms: PiForms) -> str:
    names = [pi.full, f"{pi.first} {forms.surname}"]
    names += [f"{f.title()} {forms.surname}" for f in forms.first_names]
    names += [f"{forms.first_initial.upper()}. {forms.surname}", f"Dr {forms.surname}"]
    lines = [
        f"The PI is author {forms.position} in the author list." if forms.position else "",
        f"PI's name: {pi.full}",
        f"Surname: {forms.surname}",
        "Initials forms: " + ", ".join(_initials_forms(forms.letters)),
    ]
    if forms.surname_shared:
        lines.append(
            "Another author shares this surname: only the initials forms identify the PI."
        )
    else:
        lines.append("Name forms: " + ", ".join(dict.fromkeys(names)))
    return "\n".join(line for line in lines if line)


#: "<" and ">" in untrusted text become the single guillemets "‹" and "›" before it is
#: fenced: `delimit` only strips an exact tag, so "</sta</statement>tement>" would
#: reassemble one and a forged "<pi>" block would read as ours. `_TYPOGRAPHY` folds the
#: guillemets back, so a quote that echoes the escaped statement still matches the
#: original, which is what the verifier reads.
_FENCE_ESCAPE = str.maketrans({"<": "‹", ">": "›"})


def _escape(text: str) -> str:
    return text.translate(_FENCE_ESCAPE)


def _user_content(record: dict, pi: PiName, forms: PiForms) -> str:
    """The user turn: the author list, the PI's forms and the statement, each escaped
    (`_escape`) and fenced."""
    authors = [a for a in (record.get("authors") or []) if isinstance(a, dict)]
    author_text = "\n".join(_author_line(i, a) for i, a in enumerate(authors, 1))
    statement = (record.get("coi_statement") or "").strip()
    return (
        "Author list of the article:\n" + delimit(_escape(author_text), "authors")
        + "\n\nThe PI:\n" + delimit(_escape(_pi_block(pi, forms)), "pi")
        + "\n\nCompeting-interest statement:\n" + delimit(_escape(statement), "statement")
        + "\n\nReport every company the statement says the PI founded or co-founded."
    )


def _request(system: str, user: str) -> dict[str, Any]:
    return {
        "model": get_settings().llm_coi_model,
        # Answers are a few short claims; adaptive thinking at medium effort shares this.
        "max_tokens": 4000,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": CLAIMS_SCHEMA},
        },
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": system,  # abeta_create marks a str system prompt for caching
        "messages": [{"role": "user", "content": user}],
    }


def _load_prompt() -> str | None:
    """The system prompt, read per call like the review bot's (an edit applies to the
    next record); None when it is missing or empty."""
    try:
        text = Path(COI_PROMPT_PATH).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _api_error_reason(exc: anthropic.AnthropicError) -> str:
    """A short reason for an SDK exception, most specific class first
    (`APITimeoutError` subclasses `APIConnectionError`)."""
    if isinstance(exc, anthropic.APITimeoutError):
        return "api_timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "api_connection"
    if isinstance(exc, anthropic.APIStatusError):
        return f"api_status_{exc.status_code}"
    if isinstance(exc, anthropic.APIError):
        return "api_error"
    return "client_error"


# --- response -----------------------------------------------------------------


def _reply_payload(message: Any) -> tuple[object, str | None]:
    """(parsed JSON payload, None) or (None, reason) for a reply that is a refusal,
    truncated, or not one JSON document."""
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None)
        return None, f"refusal:{category}" if category else "refusal"
    if stop == "max_tokens":
        return None, "truncated"
    text = "".join(
        getattr(b, "text", "") or ""
        for b in (getattr(message, "content", None) or [])
        if getattr(b, "type", None) == "text"
    )
    try:
        return json.loads(text), None
    except (TypeError, ValueError):
        return None, "malformed_json"


_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)


def _usage(message: Any) -> dict[str, int] | None:
    """The reply's token counts (see `CoiOutcome.usage`), or None without a usage block."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    counts = {k: getattr(usage, k, None) for k in _USAGE_FIELDS}
    return {k: v if isinstance(v, int) else 0 for k, v in counts.items()}


def _payload_claims(payload: object) -> list | None:
    """The `claims` list of a schema-shaped payload, or None."""
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        return None
    return payload["claims"]


_TYPOGRAPHY = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
    "‹": "<", "›": ">",  # undo `_escape` in a quote that echoes the escaped statement
})


def _norm_map(text: str) -> tuple[str, list[int]]:
    """`_norm(text)` and, for each of its characters, the index in `text` of the
    character it came from, so a match in the normalised form maps back to the
    original. NFKC runs per base character plus its combining marks, so a decomposed
    accent composes the same way on both sides."""
    chars: list[str] = []
    origin: list[int] = []
    i = 0
    while i < len(text):
        j = i + 1
        while j < len(text) and unicodedata.combining(text[j]):
            j += 1
        for ch in unicodedata.normalize("NFKC", text[i:j]).translate(_TYPOGRAPHY):
            chars.append(ch)
            origin.append(i)
        i = j
    out: list[str] = []
    where: list[int] = []
    gap: int | None = None  # index of the first whitespace character of a pending run
    for ch, at in zip(chars, origin, strict=True):
        if ch.isspace():
            gap = at if gap is None else gap
            continue
        if gap is not None and out:
            out.append(" ")
            where.append(gap)
        gap = None
        out.append(ch)
        where.append(at)
    return "".join(out), where


def _norm(text: str) -> str:
    """NFKC, typographic dashes and quotes folded to ASCII, whitespace runs collapsed:
    the form in which a quoted sentence must occur in the statement. The folding only
    forgives a model that retyped a curly quote or a non-breaking hyphen, or echoed an
    angle bracket as `_escape` sent it."""
    return _norm_map(text)[0]


# --- sentences ------------------------------------------------------------------

#: A candidate sentence end: ".", "!" or "?", any closing quotes or brackets, then
#: whitespace; group 1 is the next word character, which must be a capital or a digit.
#: ";" and ":" never end a sentence here, so a semicolon-chained disclosure ("…
#: personal fees from X; and is a co-founder of Y.") and an eLife block entry stay whole.
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[\"“‘(\[]?(\w))")
#: Words whose period never ends a sentence, compared case-folded: honorifics, "et al.",
#: "vs.", "no." / "nos." (serial and patent numbers), "vol.", "fig.", "approx.", "ca.".
#: Any one-letter word is protected as well: initials ("V.E.V.", "A."), "e.g.", "i.e.",
#: "S.A.".
_ABBREVIATIONS = frozenset({
    "dr", "prof", "mr", "mrs", "ms", "mx", "jr", "sr", "st", "al", "vs",
    "no", "nos", "vol", "fig", "approx", "ca",
})
#: Corporate abbreviations ("A/S" counts too). Their period is part of the name only when
#: another corporate word follows ("Acme Co. Ltd."); before any other capital it ends the
#: sentence ("… Novo Nordisk A/S. V.E.V. is a founder of …"), so one sentence never
#: absorbs the next sentence's subject. A bracketed or quoted group right after the
#: period is looked past first (`_continues_after_corporate`).
_CORPORATE = frozenset({"inc", "ltd", "co", "corp", "llc", "gmbh", "ag"})
_CORPORATE_NEXT = _CORPORATE | {"kg", "sa", "plc", "pty", "limited", "company", "corporation"}
_LAST_WORD = re.compile(r"[^\W\d_]+$")
_NEXT_WORD = re.compile(r"[^\W\d_]+")
#: Opening brackets and quotes, each with its closer.
_OPENERS = {"(": ")", "[": "]", '"': '"', "“": "”", "‘": "’"}


def _continues_after_corporate(text: str, after: int) -> bool:
    """Whether the text from `after` continues the name or sentence that a corporate
    abbreviation's period sits in: another corporate word ("Acme Co. Ltd."), or, when a
    bracketed or quoted group opens there, a lower-case word or punctuation after its
    closer ("Acme Inc. (Baltimore, MD) and owns stock"). An unclosed group, or a capital
    after it, ends the sentence."""
    if after < len(text) and text[after] in _OPENERS:
        close = text.find(_OPENERS[text[after]], after + 1)
        if close < 0:
            return False
        rest = text[close + 1:].lstrip()
        if rest[:1] and (rest[0] in ",;:.!?)]" or rest[0].islower()):
            return True  # a later stop, if any, is the sentence end
        after = len(text) - len(rest)
    nxt = _NEXT_WORD.match(text, after)
    return nxt is not None and nxt.group().casefold() in _CORPORATE_NEXT


def _protected_period(text: str, at: int, after: int) -> bool:
    """Whether the "." at `at` closes an initial or an abbreviation that does not end
    the sentence; `after` is where the next sentence would start."""
    word = _LAST_WORD.search(text, max(0, at - 12), at)
    if word is None:
        return False
    w = word.group().casefold()
    slash = word.start() > 0 and text[word.start() - 1] == "/"  # the "S" of "A/S"
    if w in _ABBREVIATIONS or (len(w) == 1 and not slash):
        return True
    if w in _CORPORATE or slash:
        return _continues_after_corporate(text, after)
    return False


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of each sentence of `text`, whitespace trimmed. A sentence
    ends at "." "!" or "?" (plus closing quotes or brackets) followed by whitespace and
    a capital or a digit, unless the "." closes an initial, a listed abbreviation or a
    corporate abbreviation that continues the name (`_protected_period`)."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        nxt = m.group(1)
        if not (nxt.isupper() or nxt.isdigit()):
            continue
        if text[m.start()] == "." and _protected_period(text, m.start(), m.end()):
            continue
        spans.append(_trimmed(text, start, m.start() + len(m.group().rstrip())))
        start = m.end()
    spans.append(_trimmed(text, start, len(text)))
    return [(s, e) for s, e in spans if s < e]


# --- names ------------------------------------------------------------------------

_HONORIFIC = r"(?:Drs|Dr|Profs|Prof|Professors|Professor|Mr|Mrs|Ms|Mx)\.?\s"


def _initials_pattern(letters: str) -> str:
    """One initials string in any written form ("VEV", "V.E.V.", "V. E. V.", "V.E.V"),
    never as part of a longer run: no letter or period before it, no word character,
    ".X" or " X." after it ("V.E.V.S." and "V. E. V. S." are someone else).

    A two-letter string written without a period ("CA", "MD") is too often a state or a
    degree: it counts only at the start of the text or right after ", " or "; ", and
    only when a space and a word follow ("CA is a founder", "…, BV Founder of"), never
    in "(San Diego, CA)". Its dotted forms ("C.A.", "C. A.") count anywhere."""
    head = r"(?<![\w.])(?<![A-Z]\.\s)"
    tail = r"(?!\w)(?!\.[^\W\d_])(?!\.?\s[A-Z]\.)"
    if len(letters) == 2:
        a, b = (re.escape(ch) for ch in letters)
        dotted = rf"{head}{a}\.\s?{b}\.?{tail}"
        bare = rf"(?:^|(?<=, )|(?<=; )){a}{b}(?=\s\w)(?!\s[A-Z]\.)"
        return rf"(?:{dotted}|{bare})"
    body = r"\.?\s?".join(re.escape(ch) for ch in letters)
    return rf"{head}{body}\.?{tail}"


def _name_patterns(forms: PiForms) -> list[str]:
    """The surname forms (case as indexed, or with a capital first letter) after an
    honorific, an initial or one of the given names ("Dr Velculescu", "V. Velculescu",
    "Victor E. Velculescu"). A surname of four or more letters also counts alone, in the
    group named `bare` ("Velculescu"); a shorter one ("He", "Li") never does, so a
    pronoun or a common word is never the person. A `bare` match that is one word of a
    longer capitalised name is discarded by `_Person.mentions`."""
    surname = strip_accents(forms.surname).strip()
    if not surname:
        return []
    variants = sorted({surname, surname[:1].upper() + surname[1:]})
    tail = "(?:" + "|".join(re.escape(v) for v in variants) + r")(?!\w)"
    prefixes = [_HONORIFIC, r"[A-Z]\.\s?"] + [
        re.escape(strip_accents(f).title()) + r"\s(?:[A-Z]\.?\s)?" for f in forms.first_names
    ]
    patterns = [rf"(?<!\w){p}{tail}" for p in prefixes]
    if len(name_key(surname)) >= 4:
        patterns.append(rf"(?P<bare>(?<!\w){tail})")
    return patterns


def _pi_regex(forms: PiForms, *, names: bool) -> re.Pattern[str]:
    """What names one person in a text: every initials form, plus the surname forms
    when `names` is set. Run on `strip_accents(_norm(sentence))`; case-sensitive."""
    patterns = [_initials_pattern(s) for s in sorted(forms.letters, key=lambda x: (-len(x), x))]
    if names:
        patterns += _name_patterns(forms)
    return re.compile("|".join(patterns) or r"(?!)")


#: A capitalised word ending right before a bare surname, by a space or a hyphen
#: ("Johns Hopkins", "Bristol-Myers"); and a hyphen joining one right after it.
_CAPITALISED_BEFORE = re.compile(r"(?<![\w'’])[A-Z][\w'’]*[ -]$")
_CAPITALISED_AFTER = re.compile(r"-[A-Z]")


@dataclass(frozen=True)
class _Person:
    """How one author of the record is named in a sentence (`_pi_regex`)."""

    pattern: re.Pattern[str]

    def mentions(self, text: str) -> list[tuple[int, int]]:
        """(start, end) of each mention in `text`. A bare surname (`_name_patterns`)
        right after a capitalised word or hyphen-joined to one is part of another name
        ("Johns Hopkins" is not Dr Hopkins, "Bristol-Myers" is not Dr Myers) and is
        skipped; a given name or honorific before it is matched as a prefixed form."""
        out = []
        for m in self.pattern.finditer(text):
            if m.groupdict().get("bare") is not None and (
                _CAPITALISED_BEFORE.search(text[: m.start()]) or _CAPITALISED_AFTER.match(text, m.end())
            ):
                continue
            out.append(m.span())
        return out


def _other_authors(authors: list[dict], forms: PiForms) -> tuple[_Person, ...]:
    """Every author but the PI (`forms.position`), named by their initials forms and
    their surname forms. With `position` 0 the PI is not excluded, so every PI mention
    also reads as another author's and nothing verifies."""
    out = []
    for n, author in enumerate(authors, 1):
        if n == forms.position or author.get("collective"):
            continue
        given = _author_given(author)
        other = PiForms(
            letters=frozenset(_author_letter_forms(author)),
            first_names=(fold(given),) if len(name_key(given)) > 1 else (),
            first_initial=name_key(given)[:1],
            surname=_author_last(author),
        )
        out.append(_Person(_pi_regex(other, names=True)))
    return tuple(out)


def _clause_splits(authors: list[dict]) -> re.Pattern[str]:
    """Where a sentence splits into clauses for `_Statement.names_pi`: at ";" and at an
    eLife block start, ", " before an author's undotted initials form of two to four
    letters and a capitalised word ("…, BV Founder of …", "…, SZ Hold equity …")."""
    letters = {
        s for author in authors for s in _author_letter_forms(author) if 2 <= len(s) <= 4
    }
    alts = "|".join(sorted(letters, key=lambda x: (-len(x), x)))
    return re.compile(";" + (rf"|, (?=(?:{alts}) [A-Z])" if alts else ""))


#: A founder phrase: "founder(s)", "founded", "founding", with or without "co"
#: ("co-founder", "Cofounders", "founder's"); not "Foundation", not a bare "found".
_FOUNDER = re.compile(r"(?<![^\W\d_])(?:co)?found(?:ers?|ed|ing)(?![^\W\d_])", re.IGNORECASE)
#: What may separate two names of one list of subjects: ", ", " and ", ", and ", " & ".
_LIST_SEP = re.compile(r"\s*,?\s*(?:(?:and|&)\s+)?")
#: The words between a company and a founder phrase after it that make the phrase a
#: relative clause about that company: ", which he co-", ", a company he ".
_RELATIVE = re.compile(r",\s(?:which|that|a company|the company)\b[^,;]*")


def _last_run(text: str, mentions: list[tuple[int, int, bool]]) -> list[tuple[int, int, bool]]:
    """The trailing run of `mentions` (sorted, non-empty) separated only by `_LIST_SEP`:
    the list of names that ends nearest the company ("B.V., K.W.K. and S.Z.")."""
    run = [mentions[-1]]
    for m in reversed(mentions[:-1]):
        if m[1] > run[0][0] or not _LIST_SEP.fullmatch(text, m[1], run[0][0]):
            break
        run.insert(0, m)
    return run


# --- verify -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Statement:
    """A statement prepared for verification: the original text, its normalised form
    with the map back to original offsets, its sentence spans, the PI, the other
    authors and the clause splitter."""

    text: str
    norm: str
    origin: list[int]
    spans: list[tuple[int, int]]
    pi: _Person
    others: tuple[_Person, ...]
    splits: re.Pattern[str]

    @classmethod
    def of(cls, text: str, forms: PiForms, authors: Sequence[object]) -> _Statement:
        """`authors` is the record's author list in the order `forms.position` counts
        (non-dict entries are dropped, as `locate_pi` drops them)."""
        dicts = [a for a in authors if isinstance(a, dict)]
        norm, origin = _norm_map(text)
        return cls(
            text, norm, origin, _sentence_spans(text),
            # Another author with the PI's surname: only the initials forms name the PI.
            _Person(_pi_regex(forms, names=not forms.surname_shared)),
            _other_authors(dicts, forms), _clause_splits(dicts),
        )

    def sentence(self, quote: str) -> tuple[int, str] | None:
        """(offset, original text) of the one sentence that contains the normalised
        `quote`'s first occurrence; None when the quote is absent or spans sentences."""
        at = self.norm.find(quote) if quote else -1
        if at < 0:
            return None
        start, end = self.origin[at], self.origin[at + len(quote) - 1] + 1
        for s, e in self.spans:
            if s <= start and end <= e:
                return s, self.text[s:e]
        return None

    def _clause(self, text: str, company: tuple[int, int]) -> tuple[int, int]:
        """The clause (`_clause_splits`) holding the company span; a split inside the
        company's own name is ignored. A clause that names no author before the company
        continues the subject of the clause before it across a ";" ("C. Bettegowda
        reports fees from X; and is a co-founder of OrisDx"), never across an eLife
        block start."""
        splits = list(self.splits.finditer(text))
        after = [m.start() for m in splits if m.start() >= company[1]]
        before = [m for m in splits if m.end() <= company[0]]
        start = before[-1].end() if before else 0
        while before and before[-1].group() == ";" and not self._mentions(text, start, company[0]):
            before.pop()
            start = before[-1].end() if before else 0
        return start, after[0] if after else len(text)

    def _mentions(self, text: str, start: int, end: int) -> list[tuple[int, int, bool]]:
        """(start, end, is the PI) of every author mention inside text[start:end], sorted."""
        found = [(s, e, True) for s, e in self.pi.mentions(text)]
        found += [(s, e, False) for p in self.others for s, e in p.mentions(text)]
        return sorted(m for m in found if start <= m[0] and m[1] <= end)

    def names_pi(self, text: str, company: tuple[int, int]) -> bool:
        """Whether `text` (a sentence as `strip_accents(_norm(…))`) says the PI founded
        the company at span `company`. True exactly when, inside the clause holding the
        company (`_clause`: split at ";" and eLife block starts, a subjectless clause
        extended back across ";"):

        - the author names nearest before the company form one list (`_last_run`: names
          joined only by ",", "and", "&") and the PI (`_pi_regex`, `_Person.mentions`)
          is in it, so no other author's initials or surname form stands between the
          PI's list and the company;
        - and a founder phrase (`_FOUNDER`) lies between the end of that list and the
          company, or right after the company as a relative clause (`_RELATIVE`: ",
          which he co-founded", ", a company he founded") with no author named between
          the company and the phrase.

        It does not check what the other words mean: "V.E.V. is a founder of X and an
        advisor to Acme" passes for Acme, and "B.V. and V.E.V. are founders of X and Y,
        respectively" passes for either company with either person."""
        cs, ce = self._clause(text, company)
        mentions = self._mentions(text, cs, ce)
        before = [m for m in mentions if m[1] <= company[0]]
        if not before:
            return False
        run = _last_run(text, before)
        if not any(is_pi for _, _, is_pi in run):
            return False
        if any(run[-1][1] <= f.start() for f in _FOUNDER.finditer(text, cs, company[0])):
            return True
        f = _FOUNDER.search(text, company[1], ce)
        return (
            f is not None
            and not any(company[1] <= m[0] < f.start() for m in mentions)
            and _RELATIVE.fullmatch(text, company[1], f.start()) is not None
        )


@dataclass(frozen=True)
class _Hit:
    start: int  # the sentence's offset in the statement
    where: int  # the company's offset in the sentence
    name: str
    role: str
    former: bool
    sentence: str


def _company_spans(text: str, company: str) -> list[tuple[int, int]]:
    """Every occurrence of `company` as a whole word in `text` (both accent-stripped
    and `_norm`ed), case-insensitively: "Gen" is not in "Genentech", "Acme Bio" is not
    in "Acme Bio-Sciences", and "Acme Bio" is in "Acme Bio's"."""
    needle = strip_accents(_norm(company))
    if not needle:
        return []
    pattern = rf"(?<!\w){re.escape(needle)}(?!\w)(?!-[^\W\d_])"
    return [m.span() for m in re.finditer(pattern, text, re.IGNORECASE)]


def _verified(item: object, statement: _Statement) -> _Hit | None:
    """The claim with its quote expanded to the enclosing sentence, when it passes
    every check, else None."""
    if not isinstance(item, dict):
        return None
    company, role, quote = item.get("company"), item.get("role"), item.get("sentence")
    if not (isinstance(company, str) and isinstance(quote, str) and role in ROLES):
        return None
    quote = _norm(quote)
    if not mentions_founding(quote):
        return None
    found = statement.sentence(quote)
    if found is None:
        return None
    start, sentence = found
    text = strip_accents(_norm(sentence))
    spans = [s for s in _company_spans(text, company) if statement.names_pi(text, s)]
    if not spans:
        return None
    try:
        name, _ = _clean_name(company)
    except CompanyValidationError:
        return None
    return _Hit(start, spans[0][0], name, role, item.get("former") is True, sentence)


def _merge_repeats(hits: list[_Hit]) -> list[_Hit]:
    """One hit per company (`normalize_company_name`), the first in order, with
    `former` set when any repeat sets it."""
    by_key: dict[str, _Hit] = {}
    for hit in hits:
        key = normalize_company_name(hit.name)
        first = by_key.get(key)
        by_key[key] = hit if first is None else replace(first, former=first.former or hit.former)
    return list(by_key.values())


def verify_claims(
    statement: str,
    payload: object,
    *,
    pmid: str,
    year: int | None,
    forms: PiForms,
    authors: Sequence[object],
) -> tuple[list[FounderClaim], int]:
    """(kept claims in statement order, number dropped). `forms` is `locate_pi`'s
    result for the record whose author list is `authors`. A claim is kept only when:

    - its quote (after `_norm`) occurs in the statement inside one sentence
      (`_sentence_spans`) and mentions founding; a quote spanning two sentences is
      dropped;
    - the company occurs in that sentence as a whole word, case-insensitively, not
      followed by "-" and a letter (`_company_spans`);
    - at one such occurrence, the clause names the PI in the list of names nearest
      before the company, with a founder phrase between them (`_Statement.names_pi`),
      so a claim whose sentence names the PI only by a pronoun, or names another
      author's founding in a clause of its own, is dropped;
    - the role is founder or co_founder and `pi_companies._clean_name` accepts the
      company.

    A kept claim's `sentence` is the enclosing sentence as the original statement
    writes it, not the model's quote. Repeats of one company (same
    `normalize_company_name`) become one claim, the first in statement order, with
    `former` set if any repeat sets it; repeats are not counted as dropped. Claims past
    `MAX_CLAIMS_PER_RECORD` are cut and counted as dropped. A payload without a
    `claims` list yields ([], 0)."""
    items = _payload_claims(payload) or []
    prepared = _Statement.of(statement or "", forms, authors)
    hits: list[_Hit] = []
    dropped = 0
    for item in items:
        hit = _verified(item, prepared)
        if hit is None:
            dropped += 1
        else:
            hits.append(hit)
    hits.sort(key=lambda h: (h.start, h.where))
    merged = _merge_repeats(hits)
    claims = [
        FounderClaim(h.name, h.role, pmid, year, h.sentence, h.former)
        for h in merged[:MAX_CLAIMS_PER_RECORD]
    ]
    return claims, dropped + len(merged) - len(claims)


# --- the call -----------------------------------------------------------------


async def _call(client: anthropic.Anthropic | None, request: dict[str, Any]) -> tuple[Any, str | None]:
    """(reply, None) or (None, reason) for an SDK failure."""
    try:
        return await llm.abeta_create(client or llm.get_anthropic_client(), **request), None
    except anthropic.AnthropicError as exc:
        return None, _api_error_reason(exc)


async def extract_founder_claims(
    record: dict, pi: PiName, *, client: anthropic.Anthropic | None = None
) -> CoiOutcome:
    """The verified founder claims `record`'s competing-interest statement makes for
    `pi`, or why there are none (see `CoiOutcome`). At most one API call; none when
    the gate refuses the record."""
    statement = (record.get("coi_statement") or "").strip()
    if not statement:
        return CoiOutcome("skipped", [], "no_statement")
    forms = locate_pi(record, pi)
    if forms is None:
        return CoiOutcome("skipped", [], "pi_not_located")
    if not mentions_founding(statement):
        return CoiOutcome("skipped", [], "no_founding_wording")
    pmid = str(record.get("pmid") or "")
    system = _load_prompt()
    if system is None:
        logger.warning("coi_llm: %s missing or empty; PMID %s unavailable", COI_PROMPT_PATH, pmid)
        return CoiOutcome("unavailable", [], "prompt_missing")
    message, reason = await _call(client, _request(system, _user_content(record, pi, forms)))
    payload, usage = None, None
    if reason is None:
        usage = _usage(message)
        payload, reason = _reply_payload(message)
    if reason is None and _payload_claims(payload) is None:
        reason = "schema_mismatch"
    if reason is not None:
        logger.warning("coi_llm: PMID %s unavailable (%s)", pmid, reason)
        return CoiOutcome("unavailable", [], reason, usage=usage)
    year = record.get("year") if isinstance(record.get("year"), int) else None
    claims, dropped = verify_claims(
        statement, payload, pmid=pmid, year=year, forms=forms, authors=record.get("authors") or []
    )
    if dropped:
        logger.info("coi_llm: PMID %s: verifier dropped %d of the model's claims", pmid, dropped)
    return CoiOutcome("ok", claims, None, dropped, usage)
