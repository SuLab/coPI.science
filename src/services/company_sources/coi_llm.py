"""Founder claims from a PI's own competing-interest statement, extracted by Claude
(spec §7.5 "Step 1, extraction", O14) and verified by code.

Per PubMed record: the deterministic gate in `coi_founders` (the PI located with no
initials collision, a statement that mentions founding) decides whether a call is made
at all; a gated-out record is "skipped" and costs nothing. A gated-in record is one
`llm.abeta_create` call with structured output, the server-side refusal fallback and
the rules in `prompts/company-discovery-coi.md`. Every untrusted value (author names,
the statement) is fenced with `prompt_safety.delimit`.

The model's answer is never trusted as is: `verify_claims` keeps a claim only when its
sentence is a substring of the statement, the company is named in that sentence, the
role is founder/co_founder and `pi_companies._clean_name` accepts the name. Claims
become manager-confirmed suggestions (O9), so a dropped claim is a miss, never an error.

A refusal, an API error, a truncated or malformed reply, or a missing prompt file marks
the record "unavailable" with a short reason; nothing here raises for an upstream
problem (the job notes "coi: N of M disclosures unavailable" and carries on).
"""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anthropic

from src.agent.prompt_safety import delimit
from src.config import get_settings
from src.services import llm
from src.services.company_sources import PiName
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
    the model's claims the verifier rejected."""

    status: Literal["ok", "skipped", "unavailable"]
    claims: list[FounderClaim] = field(default_factory=list)
    reason: str | None = None
    dropped: int = 0


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


def _user_content(record: dict, pi: PiName, forms: PiForms) -> str:
    """The user turn: the author list, the PI's forms and the statement, each fenced."""
    authors = [a for a in (record.get("authors") or []) if isinstance(a, dict)]
    author_text = "\n".join(_author_line(i, a) for i, a in enumerate(authors, 1))
    statement = (record.get("coi_statement") or "").strip()
    return (
        "Author list of the article:\n" + delimit(author_text, "authors")
        + "\n\nThe PI:\n" + delimit(_pi_block(pi, forms), "pi")
        + "\n\nCompeting-interest statement:\n" + delimit(statement, "statement")
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


def _payload_claims(payload: object) -> list | None:
    """The `claims` list of a schema-shaped payload, or None."""
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        return None
    return payload["claims"]


_TYPOGRAPHY = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
})


def _norm(text: str) -> str:
    """NFKC, typographic dashes and quotes folded to ASCII, whitespace runs collapsed:
    the form in which a quoted sentence must occur in the statement. The folding only
    forgives a model that retyped a curly quote or a non-breaking hyphen."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHY).split())


def _verified(item: object, statement: str) -> tuple[int, int, str, str, bool, str] | None:
    """(sentence offset, company offset, stored name, role, former, sentence) for a
    claim that passes every check, else None."""
    if not isinstance(item, dict):
        return None
    company, role, sentence = item.get("company"), item.get("role"), item.get("sentence")
    if not (isinstance(company, str) and isinstance(sentence, str) and role in ROLES):
        return None
    quote = _norm(sentence)
    at = statement.find(quote) if quote else -1
    if at < 0 or not mentions_founding(quote):
        return None
    where = quote.casefold().find(_norm(company).casefold())
    if not company.strip() or where < 0:
        return None
    try:
        name, _ = _clean_name(company)
    except CompanyValidationError:
        return None
    return at, where, name, role, item.get("former") is True, quote


def verify_claims(
    statement: str, payload: object, *, pmid: str, year: int | None
) -> tuple[list[FounderClaim], int]:
    """(kept claims in statement order, number dropped). A claim is kept only when its
    sentence occurs verbatim in the statement (after `_norm`) and mentions founding,
    the company occurs in that sentence (case-insensitively), the role is founder or
    co_founder, and `pi_companies._clean_name` accepts the company. A repeat of an
    already-kept company (same `normalize_company_name`) is neither kept nor counted
    as dropped. A payload without a `claims` list yields ([], 0)."""
    items = _payload_claims(payload) or []
    norm_statement = _norm(statement or "")
    kept: list[tuple[int, int, str, str, bool, str]] = []
    dropped = 0
    for item in items:
        hit = _verified(item, norm_statement)
        if hit is None:
            dropped += 1
        else:
            kept.append(hit)
    kept.sort(key=lambda h: (h[0], h[1]))
    claims: list[FounderClaim] = []
    seen: set[str] = set()
    for _, _, name, role, former, quote in kept:
        key = normalize_company_name(name)
        if key in seen:
            continue
        seen.add(key)
        claims.append(FounderClaim(name, role, pmid, year, quote, former))
    return claims, dropped


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
    payload = None
    if reason is None:
        payload, reason = _reply_payload(message)
    if reason is None and _payload_claims(payload) is None:
        reason = "schema_mismatch"
    if reason is not None:
        logger.warning("coi_llm: PMID %s unavailable (%s)", pmid, reason)
        return CoiOutcome("unavailable", [], reason)
    year = record.get("year") if isinstance(record.get("year"), int) else None
    claims, dropped = verify_claims(statement, payload, pmid=pmid, year=year)
    if dropped:
        logger.info("coi_llm: PMID %s: verifier dropped %d of the model's claims", pmid, dropped)
    return CoiOutcome("ok", claims, None, dropped)
