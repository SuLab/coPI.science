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
quote occurs in the statement within one sentence, that whole sentence names the PI by
one of the record's forms and names the company as a whole word, the role is
founder/co_founder and `pi_companies._clean_name` accepts the name; at most
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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import anthropic

from src.agent.prompt_safety import delimit
from src.config import get_settings
from src.services import llm
from src.services.company_sources import PiName, name_key, strip_accents
from src.services.company_sources.coi_founders import (
    FounderClaim,
    PiForms,
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
#: "vs.". Any one-letter word is protected as well: initials ("V.E.V.", "A."), "e.g.",
#: "i.e.", "S.A.".
_ABBREVIATIONS = frozenset({"dr", "prof", "mr", "mrs", "ms", "mx", "jr", "sr", "st", "al", "vs"})
#: Corporate abbreviations ("A/S" counts too). Their period is part of the name only when
#: another corporate word follows ("Acme Co. Ltd."); before any other capital it ends the
#: sentence ("… Novo Nordisk A/S. V.E.V. is a founder of …"), so one sentence never
#: absorbs the next sentence's subject.
_CORPORATE = frozenset({"inc", "ltd", "co", "corp", "llc", "gmbh", "ag"})
_CORPORATE_NEXT = _CORPORATE | {"kg", "sa", "plc", "pty", "limited", "company", "corporation"}
_LAST_WORD = re.compile(r"[^\W\d_]+$")
_NEXT_WORD = re.compile(r"[^\W\d_]+")


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
        nxt = _NEXT_WORD.match(text, after)
        return nxt is not None and nxt.group().casefold() in _CORPORATE_NEXT
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
    a capital or a digit, unless the "." closes an initial, an honorific or a corporate
    abbreviation that continues the name (`_protected_period`)."""
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


# --- the PI's forms -------------------------------------------------------------

_HONORIFIC = r"(?:Dr|Prof|Professor|Mr|Mrs|Ms|Mx)\.?\s"


def _initials_pattern(letters: str) -> str:
    """One initials string in any written form ("VEV", "V.E.V.", "V. E. V.", "V.E.V"),
    never as part of a longer run: no letter or period before it, no word character,
    ".X" or " X." after it ("V.E.V.S." and "V. E. V. S." are someone else)."""
    body = r"\.?\s?".join(re.escape(ch) for ch in letters)
    return rf"(?<![\w.])(?<![A-Z]\.\s){body}\.?(?!\w)(?!\.[^\W\d_])(?!\.?\s[A-Z]\.)"


def _name_patterns(forms: PiForms) -> list[str]:
    """The surname forms (case as indexed, or with a capital first letter). A surname of
    four or more letters counts alone ("Velculescu", "Dr Velculescu", "Victor E.
    Velculescu"); a shorter one ("He", "Li") only after an honorific, an initial or one
    of the PI's given names, so a pronoun or a common word is never the PI."""
    surname = strip_accents(forms.surname).strip()
    if not surname:
        return []
    variants = sorted({surname, surname[:1].upper() + surname[1:]})
    tail = "(?:" + "|".join(re.escape(v) for v in variants) + r")(?!\w)"
    if len(name_key(surname)) >= 4:
        return [rf"(?<!\w){tail}"]
    prefixes = [_HONORIFIC, r"[A-Z]\.\s?"] + [
        re.escape(strip_accents(f).title()) + r"\s(?:[A-Z]\.?\s)?" for f in forms.first_names
    ]
    return [rf"(?<!\w){p}{tail}" for p in prefixes]


def _pi_regex(forms: PiForms) -> re.Pattern[str]:
    """What names the PI in a sentence: every initials form, plus the surname forms
    unless another author shares the surname (then only initials identify the PI). Run
    on `strip_accents(_norm(sentence))`; case-sensitive."""
    patterns = [_initials_pattern(s) for s in sorted(forms.letters, key=lambda x: (-len(x), x))]
    if not forms.surname_shared:
        patterns += _name_patterns(forms)
    return re.compile("|".join(patterns) or r"(?!)")


# --- verify -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Statement:
    """A statement prepared for verification: the original text, its normalised form
    with the map back to original offsets, its sentence spans and the PI matcher."""

    text: str
    norm: str
    origin: list[int]
    spans: list[tuple[int, int]]
    pi: re.Pattern[str]

    @classmethod
    def of(cls, text: str, forms: PiForms) -> _Statement:
        norm, origin = _norm_map(text)
        return cls(text, norm, origin, _sentence_spans(text), _pi_regex(forms))

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

    def names_pi(self, norm_sentence: str) -> bool:
        return self.pi.search(strip_accents(norm_sentence)) is not None


@dataclass(frozen=True)
class _Hit:
    start: int  # the sentence's offset in the statement
    where: int  # the company's offset in the sentence
    name: str
    role: str
    former: bool
    sentence: str


def _company_at(norm_sentence: str, company: str) -> int:
    """Offset of `company` as a whole word in the sentence, case-insensitively ("Gen"
    is not in "Genentech"; "Acme Bio" is in "Acme Bio's"), else -1."""
    needle = _norm(company).casefold()
    if not needle:
        return -1
    m = re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", norm_sentence.casefold())
    return m.start() if m else -1


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
    norm_sentence = _norm(sentence)
    where = _company_at(norm_sentence, company)
    if where < 0 or not statement.names_pi(norm_sentence):
        return None
    try:
        name, _ = _clean_name(company)
    except CompanyValidationError:
        return None
    return _Hit(start, where, name, role, item.get("former") is True, sentence)


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
    statement: str, payload: object, *, pmid: str, year: int | None, forms: PiForms
) -> tuple[list[FounderClaim], int]:
    """(kept claims in statement order, number dropped). A claim is kept only when:

    - its quote (after `_norm`) occurs in the statement inside one sentence
      (`_sentence_spans`) and mentions founding; a quote spanning two sentences is
      dropped;
    - that whole sentence names the PI by one of `forms` (`_pi_regex`), so a claim
      whose sentence names the PI only by a pronoun is dropped;
    - the company occurs in that sentence as a whole word, case-insensitively;
    - the role is founder or co_founder and `pi_companies._clean_name` accepts the
      company.

    A kept claim's `sentence` is the enclosing sentence as the original statement
    writes it, not the model's quote. Repeats of one company (same
    `normalize_company_name`) become one claim, the first in statement order, with
    `former` set if any repeat sets it; repeats are not counted as dropped. Claims past
    `MAX_CLAIMS_PER_RECORD` are cut and counted as dropped. A payload without a
    `claims` list yields ([], 0)."""
    items = _payload_claims(payload) or []
    prepared = _Statement.of(statement or "", forms)
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
    claims, dropped = verify_claims(statement, payload, pmid=pmid, year=year, forms=forms)
    if dropped:
        logger.info("coi_llm: PMID %s: verifier dropped %d of the model's claims", pmid, dropped)
    return CoiOutcome("ok", claims, None, dropped, usage)
