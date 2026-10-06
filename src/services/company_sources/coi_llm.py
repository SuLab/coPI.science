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
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import anthropic

from src.agent.prompt_safety import delimit
from src.config import get_settings
from src.services import llm
from src.services.assessment_chat_stream import usage_entries
from src.services.coi_attribution import Statement
from src.services.coi_attribution import last_run as _last_run
from src.services.coi_attribution import norm as _norm
from src.services.company_sources import PiName, strip_accents
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
    record, a missing prompt, an API error, or a reply without usage. `entries` is what
    the job prices the call by (src/services/company_discovery_budget.py)."""

    status: Literal["ok", "skipped", "unavailable"]
    claims: list[FounderClaim] = field(default_factory=list)
    reason: str | None = None
    dropped: int = 0
    usage: dict[str, int] | None = None
    #: The reply's usage as ledger entries (`assessment_chat_stream.usage_entries`): one
    #: per API iteration, each under the model that billed it. [] when no call was made or
    #: the API answered an error status (never billed); None when no usage is known (a
    #: timeout, a connection error, a reply without usage). Not part of equality.
    entries: list[dict] | None = field(default=None, compare=False)


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
#: reassemble one and a forged "<pi>" block would read as ours. `coi_attribution.TYPOGRAPHY`
#: folds the guillemets back, so a quote that echoes the escaped statement still matches the
#: original, which is what the verifier reads.
_FENCE_ESCAPE = str.maketrans({"<": "‹", ">": "›"})


def _escape(text: str) -> str:
    return text.translate(_FENCE_ESCAPE)


#: Most author lines one call shows. A consortium paper can list thousands, which would
#: make one call cost several times its budget reservation
#: (`company_discovery_budget.COI_RESERVE_USD`); past this the list is cut to a window
#: around the PI, keeping the original numbering (security review 2026-10-06).
MAX_AUTHOR_LINES = 100


def _author_lines(authors: list[dict], position: int) -> list[str]:
    """Each author's numbered line; past MAX_AUTHOR_LINES, only the MAX_AUTHOR_LINES
    around ``position`` (1-based, 0 when unknown), with the omissions marked."""
    if len(authors) <= MAX_AUTHOR_LINES:
        return [_author_line(i, a) for i, a in enumerate(authors, 1)]
    start = min(max(position - MAX_AUTHOR_LINES // 2, 1), len(authors) - MAX_AUTHOR_LINES + 1)
    end = start + MAX_AUTHOR_LINES - 1
    lines = [f"(authors 1-{start - 1} omitted)"] if start > 1 else []
    lines += [_author_line(i, authors[i - 1]) for i in range(start, end + 1)]
    if end < len(authors):
        lines.append(f"(authors {end + 1}-{len(authors)} omitted)")
    return lines


def _user_content(record: dict, pi: PiName, forms: PiForms) -> str:
    """The user turn: the author list (cut per `_author_lines`), the PI's forms and the
    statement, each escaped (`_escape`) and fenced."""
    authors = [a for a in (record.get("authors") or []) if isinstance(a, dict)]
    author_text = "\n".join(_author_lines(authors, forms.position))
    statement = (record.get("coi_statement") or "").strip()
    return (
        "Author list of the article:\n" + delimit(_escape(author_text), "authors")
        + "\n\nThe PI:\n" + delimit(_escape(_pi_block(pi, forms)), "pi")
        + "\n\nCompeting-interest statement:\n" + delimit(_escape(statement), "statement")
        + "\n\nReport every company the statement says the PI founded or co-founded."
    )


#: The output ceiling of one extraction call (`_request`); sizes its budget reservation.
MAX_TOKENS = 4000


def prompt_chars(record: dict, pi: PiName) -> int | None:
    """The characters one extraction call for `record` would send (system prompt and user
    turn), for sizing its budget reservation; None when the gate would not send it or
    the prompt is missing."""
    forms = locate_pi(record, pi)
    system = _load_prompt()
    if forms is None or system is None:
        return None
    return len(system) + len(_user_content(record, pi, forms))


def _request(system: str, user: str) -> dict[str, Any]:
    return {
        "model": get_settings().llm_coi_model,
        # Answers are a few short claims; adaptive thinking at medium effort shares this.
        "max_tokens": MAX_TOKENS,
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


#: A founder phrase: "founder(s)", "founded", "founding", with or without "co"
#: ("co-founder", "Cofounders", "founder's"); not "Foundation", not a bare "found".
_FOUNDER = re.compile(r"(?<![^\W\d_])(?:co)?found(?:ers?|ed|ing)(?![^\W\d_])", re.IGNORECASE)
#: The words between a company and a founder phrase after it that make the phrase a
#: relative clause about that company: ", which he co-", ", a company he ".
_RELATIVE = re.compile(r",\s(?:which|that|a company|the company)\b[^,;]*")


# --- verify -----------------------------------------------------------------------


class _Statement(Statement):
    """`coi_attribution.Statement` plus the founder rule (`names_pi`)."""

    def names_pi(self, text: str, company: tuple[int, int]) -> bool:
        """Whether `text` (a sentence as `strip_accents(_norm(…))`) says the PI founded
        the company at span `company`. True exactly when, inside the clause holding the
        company (`clause`: split at ";" and eLife block starts, a subjectless clause
        extended back across ";"):

        - the author names nearest before the company form one list
          (`coi_attribution.last_run`: names joined only by ",", "and", "&") and the PI
          (`coi_attribution.pi_regex`, `Person.mentions`) is in it, so no other author's
          initials or surname form stands between the PI's list and the company;
        - and a founder phrase (`_FOUNDER`) lies between the end of that list and the
          company, or right after the company as a relative clause (`_RELATIVE`: ",
          which he co-founded", ", a company he founded") with no author named between
          the company and the phrase.

        It does not check what the other words mean: "V.E.V. is a founder of X and an
        advisor to Acme" passes for Acme, and "B.V. and V.E.V. are founders of X and Y,
        respectively" passes for either company with either person."""
        cs, ce = self.clause(text, company)
        mentions = self.mentions(text, cs, ce)
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
      (`coi_attribution.sentence_spans`) and mentions founding; a quote spanning two sentences is
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
        return CoiOutcome("skipped", [], "no_statement", entries=[])
    forms = locate_pi(record, pi)
    if forms is None:
        return CoiOutcome("skipped", [], "pi_not_located", entries=[])
    if not mentions_founding(statement):
        return CoiOutcome("skipped", [], "no_founding_wording", entries=[])
    pmid = str(record.get("pmid") or "")
    system = _load_prompt()
    if system is None:
        logger.warning("coi_llm: %s missing or empty; PMID %s unavailable", COI_PROMPT_PATH, pmid)
        return CoiOutcome("unavailable", [], "prompt_missing", entries=[])
    message, reason = await _call(client, _request(system, _user_content(record, pi, forms)))
    payload, usage = None, None
    entries: list[dict] | None = None
    if reason is None:
        usage = _usage(message)
        entries = usage_entries(message) if usage is not None else None
        payload, reason = _reply_payload(message)
    elif reason.startswith("api_status_"):
        entries = []  # an error status is never billed (assessment_chat_suggestions' rule)
    if reason is None and _payload_claims(payload) is None:
        reason = "schema_mismatch"
    if reason is not None:
        logger.warning("coi_llm: PMID %s unavailable (%s)", pmid, reason)
        return CoiOutcome("unavailable", [], reason, usage=usage, entries=entries)
    year = record.get("year") if isinstance(record.get("year"), int) else None
    claims, dropped = verify_claims(
        statement, payload, pmid=pmid, year=year, forms=forms, authors=record.get("authors") or []
    )
    if dropped:
        logger.info("coi_llm: PMID %s: verifier dropped %d of the model's claims", pmid, dropped)
    return CoiOutcome("ok", claims, None, dropped, usage, entries=entries)
