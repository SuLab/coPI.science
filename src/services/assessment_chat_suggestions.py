"""The assessment chat's opening questions: generated per assessment by the worker,
with a deterministic template set whenever no generated set is ready.

Generation (``generate_due``) runs in the worker while it has no job: one
(assessment, tier) at a time, newest assessment first, for the current verdict
revision. It sends that tier's chat record — the same five documents the chat answers
from, so a reviewer's questions never draw on a staff-only field — with
``prompts/assessment-chat-suggestions.md`` and a numbered list of the Verdict
document's blocks, and asks for questions as structured JSON, each tied to one block
by id. A question survives only when its block id is one of those listed, so every
suggestion is about a block of the record and carries that block's page anchor (where
the page shows it inline). The staff tier is always generated; the reviewer tier only
once a reviewer account exists.

A row is written BEFORE the model is called (``failed``/``in_progress``, attempts + 1,
and a placeholder usage entry the ceiling prices at the chat's reserve), so a paid
attempt is counted even if the worker dies before storing its result. A failing
assessment is retried at most ``MAX_ATTEMPTS`` times, ``RETRY_AFTER`` apart; an
overloaded, rate-limited or 5xx answer (never billed) is retried without using up an
attempt. A refusal is never retried for that revision. The daily dollar ceiling
(``assessment_chat_suggestions_daily_usd_limit``) counts these rows only.

The page (``page_suggestions``) reads the current revision's ``ready`` row and
otherwise builds ``template_suggestions`` from the fields every tier's page shows, so
the drawer and the inline asks never wait on the model. Nothing here logs question text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import get_settings
from src.models import USER_ROLE_REVIEWER, AssessmentChatSuggestionSet, OpportunityAssessment, User
from src.models.assessment_chat import (
    CHAT_TIER_REVIEWER,
    CHAT_TIER_STAFF,
    SUGGESTION_STATUS_FAILED,
    SUGGESTION_STATUS_READY,
    SUGGESTION_STATUS_REFUSED,
    TOKEN_FIELDS,
)
from src.services import llm
from src.services.assessment_chat import FALLBACK_BETA, row_spend
from src.services.assessment_chat_record import (
    DOC_ORDER,
    DOC_VERDICT,
    BlockTarget,
    ChatRecord,
    load_chat_record,
    tier_for,
)
from src.services.assessment_chat_stream import served_by, strip_private_use, usage_entries
from src.services.llm_pricing import PRICES

logger = logging.getLogger(__name__)

#: Read per generation, like the chat's own prompt: `prompts/` is bind-mounted into
#: the worker, so an edit applies to the next generation (existing rows are kept).
PROMPT_PATH = Path("prompts/assessment-chat-suggestions.md")

MAX_SUGGESTIONS = 4
#: Fewer valid questions than this and the row is `failed` (the template set shows).
MIN_SUGGESTIONS = 2
MAX_QUESTION_CHARS = 200
MIN_QUESTION_CHARS = 10
#: Paid attempts per (assessment, tier, revision) before the template set is final.
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)
#: The worker asks for due work at most this often while idle.
SWEEP_INTERVAL_SECONDS = 30.0
#: One call's limit, and no SDK retry (the 30-minute retry is ours): a job waits behind
#: at most this long. Four short questions after ~90 k tokens of record take well under it.
CALL_TIMEOUT_SECONDS = 180.0
_WINDOW = timedelta(hours=24)

KIND_GENERATED = "generated"
KIND_TEMPLATE = "template"
ERROR_IN_PROGRESS = "in_progress"

#: The page sections that show inline asks (element ids in
#: templates/admin/_assessment_detail_body.html). A block anchored anywhere else — an
#: interview message, a consult card — is offered in the drawer only.
INLINE_SECTIONS = frozenset(
    {"brief", "signals", "ask", "verdict", "panel", "gating", "red-flags", "rationale", "scores"}
)
#: `score-rationale` sits inside the brief card.
_SECTION_OF_ANCHOR = {"score-rationale": "brief"}

#: The drawer's original static starters: the last resort when a verdict has nothing
#: specific to ask about.
GENERIC_QUESTIONS = (
    "What is being proposed, in plain terms?",
    "What were the hub's main concerns, and how did the lab's agent answer them?",
    "What would Blackbird fund next, and what result would change the recommendation?",
)

SUGGESTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["block", "question"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["questions"],
    "additionalProperties": False,
}

_WHITESPACE_RE = re.compile(r"\s+")
#: Anything a plain one-line question has no business carrying: a link, markup, or a
#: code span. Such a question is dropped rather than repaired.
_UNWANTED_RE = re.compile(r"https?://|www\.|[<>`]|\]\(", re.IGNORECASE)


@dataclass(frozen=True)
class Suggestion:
    """One opening question as the page renders it."""

    text: str
    #: The page element id of the record block it is about, or None.
    anchor: str | None
    kind: str  # KIND_GENERATED | KIND_TEMPLATE

    @property
    def section(self) -> str | None:
        """The page section that shows it inline, or None (drawer only)."""
        if self.anchor is None:
            return None
        section = _SECTION_OF_ANCHOR.get(self.anchor, self.anchor)
        return section if section in INLINE_SECTIONS else None


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def template_suggestions(detail: dict[str, Any]) -> list[Suggestion]:
    """Up to MAX_SUGGESTIONS questions built from the verdict's own fields, every one of
    which both tiers' pages render (gates, dimension scores, red flags, recommendation,
    the ask): a not-met or else unconfirmed gate, the lowest-scored dimension, the first
    red flag, then the recommendation or the ask. Padded with GENERIC_QUESTIONS when the
    verdict has fewer than two of those. Never raises on a malformed stored value."""
    a = detail["assessment"]
    out: list[Suggestion] = []

    gating = a.gating if isinstance(a.gating, dict) else {}
    definitions = detail.get("gating_definitions") or {}

    def gate_title(key: object) -> str:
        meta = definitions.get(key) if isinstance(key, str) else None
        if isinstance(meta, dict) and meta.get("title"):
            return _clip(str(meta["title"]), 60)
        return _clip(str(key).replace("_", " "), 60)

    not_met = [k for k, v in gating.items() if v == "not_met"]
    unconfirmed = [k for k, v in gating.items() if v == "unconfirmed"]
    if not_met:
        out.append(Suggestion(
            f"Why did the hub find the gate “{gate_title(not_met[0])}” not met, and how did"
            " the lab's agent respond?",
            "gating", KIND_TEMPLATE,
        ))
    elif unconfirmed:
        out.append(Suggestion(
            f"The gate “{gate_title(unconfirmed[0])}” was never confirmed. What does the"
            " interview say about it?",
            "gating", KIND_TEMPLATE,
        ))

    scored = [
        d for d in detail.get("dimensions") or []
        if isinstance(d, dict) and isinstance(d.get("score"), (int, float))
        and not isinstance(d.get("score"), bool)
    ]
    if scored:
        # `min` keeps the first of equal scores, and the page lists dimensions
        # heaviest first, so a tie goes to the dimension that moves the score most.
        low = min(scored, key=lambda d: d["score"])
        title = _clip(str(low.get("title") or low.get("key") or "this dimension"), 60)
        scale = detail.get("scale_max")
        of = f" out of {scale:g}" if isinstance(scale, (int, float)) and scale else ""
        out.append(Suggestion(
            f"Why did the hub score {title} {low['score']:g}{of}, and what in the interview"
            " supports that?",
            "scores", KIND_TEMPLATE,
        ))

    flags = [
        str(f).strip() for f in (a.red_flags if isinstance(a.red_flags, list) else [])
        if f is not None and str(f).strip()
    ]
    if flags:
        out.append(Suggestion(
            f"How did the lab's agent respond to the red flag “{_clip(flags[0], 90)}”?",
            "red-flags", KIND_TEMPLATE,
        ))

    if a.recommendation == "conditional":
        out.append(Suggestion(
            "What would have to be true for this to move from conditional to advance?",
            "verdict", KIND_TEMPLATE,
        ))
    elif a.recommendation == "pass":
        out.append(Suggestion(
            "What was the decisive reason the hub declined, and is it a fixable gap or a"
            " fundamental one?",
            "verdict", KIND_TEMPLATE,
        ))
    elif a.recommendation == "advance":
        out.append(Suggestion(
            "What is the weakest part of the case for advancing this?",
            "verdict", KIND_TEMPLATE,
        ))
    if a.recommended_next_experiment and len(out) < MAX_SUGGESTIONS:
        out.append(Suggestion(
            "What result from the recommended next experiment would change the"
            " recommendation?",
            "ask", KIND_TEMPLATE,
        ))

    if len(out) < MIN_SUGGESTIONS:
        for question in GENERIC_QUESTIONS:
            if len(out) >= 3:
                break
            out.append(Suggestion(question, None, KIND_TEMPLATE))
    return out[:MAX_SUGGESTIONS]


def _stored(value: object) -> list[Suggestion]:
    """A `ready` row's stored suggestions, skipping any entry that is not well formed."""
    out: list[Suggestion] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        anchor = item.get("anchor")
        out.append(Suggestion(
            item["text"], anchor if isinstance(anchor, str) else None, KIND_GENERATED,
        ))
    return out


async def page_suggestions(
    db: AsyncSession, detail: dict[str, Any], user: Any, *, chat_enabled: bool
) -> list[Suggestion]:
    """What the detail page offers ``user``: the current revision's generated set for
    their tier, else the template set; nothing at all when the chat is off or the
    session is impersonating (the page then renders no chat control)."""
    if not chat_enabled or getattr(user, "_is_impersonated", False):
        return []
    assessment = detail["assessment"]
    stored = await db.scalar(
        select(AssessmentChatSuggestionSet.suggestions).where(
            AssessmentChatSuggestionSet.assessment_id == assessment.id,
            AssessmentChatSuggestionSet.context_tier == tier_for(user),
            AssessmentChatSuggestionSet.verdict_revision == (assessment.verdict_revision or 1),
            AssessmentChatSuggestionSet.status == SUGGESTION_STATUS_READY,
        )
    )
    return _stored(stored) or template_suggestions(detail)


# ---------------------------------------------------------------------------
# The request and the reply
# ---------------------------------------------------------------------------


def load_prompt() -> tuple[str, str] | None:
    """(prompt text, first 12 hex of its sha256), or None when missing or empty."""
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def verdict_blocks(record: ChatRecord) -> dict[str, BlockTarget]:
    """``{"V1": target, ...}``: the Verdict document's blocks, numbered from 1 in
    record order."""
    targets = record.targets[DOC_ORDER.index(DOC_VERDICT)]
    return {f"V{i}": target for i, target in enumerate(targets, 1)}


def build_request(*, model: str, system_prompt: str, record: ChatRecord) -> dict[str, Any]:
    """One structured-output request over the tier's record. Citations are switched off
    on every document: the API refuses citations together with ``output_config.format``.
    """
    documents = [{**doc, "citations": {"enabled": False}} for doc in record.documents]
    catalog = "\n".join(f"{bid}: {target.label}" for bid, target in verdict_blocks(record).items())
    instruction = (
        "Verdict blocks (id: label):\n"
        f"{catalog}\n\n"
        f"Write {MAX_SUGGESTIONS} questions about this assessment, each tied to one of these"
        " block ids."
    )
    return {
        "model": model,
        # A literal: tests/unit/test_llm_nonstreaming_ceiling.py scans src/ for them.
        # Thinking and four short questions share it.
        "max_tokens": 6000,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SUGGESTIONS_SCHEMA},
        },
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": system_prompt,  # abeta_create marks a str system prompt for caching
        "messages": [
            {"role": "user", "content": [*documents, {"type": "text", "text": instruction}]}
        ],
    }


def clean_question(raw: object) -> str | None:
    """The question as stored, or None: one line, whitespace collapsed, private-use and
    invisible format characters (Unicode category Cf: tag characters, zero-width and
    bidi controls) removed, MIN_QUESTION_CHARS to MAX_QUESTION_CHARS long, and carrying
    no link, markup or code. A click sends exactly this text as the user's own
    question, so nothing in it may be invisible on the button."""
    if not isinstance(raw, str):
        return None
    visible = "".join(ch for ch in strip_private_use(raw) if unicodedata.category(ch) != "Cf")
    text = _WHITESPACE_RE.sub(" ", visible).strip()
    if not MIN_QUESTION_CHARS <= len(text) <= MAX_QUESTION_CHARS or _UNWANTED_RE.search(text):
        return None
    return text


@dataclass(frozen=True)
class Outcome:
    status: str
    suggestions: list[dict[str, Any]] | None
    error_code: str | None
    entries: list[dict[str, Any]] | None
    served_by_model: str | None


def parse_reply(message: Any, record: ChatRecord) -> tuple[list[dict[str, Any]], str | None]:
    """``(suggestions, error_code)``: at most MAX_SUGGESTIONS valid questions, each with
    its block's anchor and label, and None — or what was kept and why the reply does
    not count (`truncated`, `malformed_json`, `too_few`). A question naming a block id
    that was not listed, or repeating a block or a question, is dropped."""
    if getattr(message, "stop_reason", None) == "max_tokens":
        return [], "truncated"
    text = "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return [], "malformed_json"
    items = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], "malformed_json"
    blocks = verdict_blocks(record)
    kept: list[dict[str, Any]] = []
    seen_blocks: set[str] = set()
    seen_texts: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        block_id = item.get("block")
        question = clean_question(item.get("question"))
        if question is None or not isinstance(block_id, str):
            continue
        block_id = block_id.strip()
        target = blocks.get(block_id)
        if target is None or block_id in seen_blocks or question.casefold() in seen_texts:
            continue
        seen_blocks.add(block_id)
        seen_texts.add(question.casefold())
        kept.append({"text": question, "anchor": target.anchor, "label": target.label})
        if len(kept) == MAX_SUGGESTIONS:
            break
    if len(kept) < MIN_SUGGESTIONS:
        return kept, "too_few"
    return kept, None


def outcome_from_message(message: Any, record: ChatRecord) -> Outcome:
    """What one reply stores. Usage is read first, so a reply this cannot use still
    records what it cost."""
    entries = usage_entries(message)
    model = served_by(message)
    if getattr(message, "stop_reason", None) == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        code = f"refusal:{category}" if category else "refusal"
        return Outcome(SUGGESTION_STATUS_REFUSED, None, code[:40], entries, model)
    try:
        kept, error = parse_reply(message, record)
    except Exception:  # noqa: BLE001 — a parsing bug must still store the paid attempt
        logger.exception("Assessment chat suggestions: could not read a reply")
        kept, error = [], "internal_error"
    if error is not None:
        return Outcome(SUGGESTION_STATUS_FAILED, None, error, entries, model)
    return Outcome(SUGGESTION_STATUS_READY, kept, None, entries, model)


def _in_flight_entry(model: str) -> dict[str, Any]:
    """A usage entry for an attempt whose cost is unknown (in flight, timed out, cut off):
    ``partial``, so ``row_spend`` prices the row at no less than the chat's reserve."""
    return {"model": model, "billed": True, "partial": True, **dict.fromkeys(TOKEN_FIELDS, 0)}


def _is_transient(exc: anthropic.AnthropicError) -> bool:
    """A rate limit or a server-side failure: the API answered with an error status, so
    nothing was billed, and the same request may well succeed later."""
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    return exc.status_code == 429 or exc.status_code >= 500


def _api_error_code(exc: anthropic.AnthropicError) -> str:
    """A short code for an SDK exception, most specific class first
    (`APITimeoutError` subclasses `APIConnectionError`)."""
    if isinstance(exc, anthropic.APITimeoutError):
        return "api_timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "api_connection"
    if isinstance(exc, anthropic.APIStatusError):
        return f"api_status_{exc.status_code}"
    return "api_error"


# ---------------------------------------------------------------------------
# The worker's side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    assessment_id: uuid.UUID
    tier: str
    revision: int
    attempts: int  # paid attempts so far
    usage_by_model: list[dict[str, Any]] | None


async def spend_24h(db: AsyncSession) -> Decimal:
    """Dollars the suggestion rows touched in the rolling 24 h cost, each row priced
    like a ledger row (an attempt with no reported usage counts the chat's reserve)."""
    usages = (
        await db.execute(
            select(AssessmentChatSuggestionSet.usage_by_model).where(
                AssessmentChatSuggestionSet.updated_at >= func.clock_timestamp() - _WINDOW
            )
        )
    ).scalars().all()
    return sum((row_spend(usage) for usage in usages), Decimal(0))


async def _tiers(db: AsyncSession) -> list[str]:
    """The staff tier always; the reviewer tier once any reviewer account exists."""
    has_reviewer = await db.scalar(
        select(exists().where(User.user_role == USER_ROLE_REVIEWER))
    )
    return [CHAT_TIER_STAFF, CHAT_TIER_REVIEWER] if has_reviewer else [CHAT_TIER_STAFF]


async def next_candidate(
    db: AsyncSession, *, skip: frozenset[tuple[uuid.UUID, str, int]] = frozenset()
) -> Candidate | None:
    """The newest assessment that needs a set for a tier at its current revision: no
    row yet, or a `failed` row with attempts left whose last attempt is RETRY_AFTER
    old. ``skip`` holds keys whose record could not be built in this process."""
    s = AssessmentChatSuggestionSet
    revision = func.coalesce(OpportunityAssessment.verdict_revision, 1)
    due = or_(
        s.id.is_(None),
        and_(
            s.status == SUGGESTION_STATUS_FAILED,
            s.attempts < MAX_ATTEMPTS,
            s.updated_at <= func.clock_timestamp() - RETRY_AFTER,
        ),
    )
    for tier in await _tiers(db):
        rows = (
            await db.execute(
                select(OpportunityAssessment.id, revision, s.attempts, s.usage_by_model)
                .outerjoin(
                    s,
                    and_(
                        s.assessment_id == OpportunityAssessment.id,
                        s.context_tier == tier,
                        s.verdict_revision == revision,
                    ),
                )
                .where(due)
                .order_by(OpportunityAssessment.created_at.desc(), OpportunityAssessment.id)
                .limit(len(skip) + 1)
            )
        ).all()
        for row in rows:
            if (row[0], tier, int(row[1])) in skip:
                continue
            return Candidate(
                assessment_id=row[0], tier=tier, revision=int(row[1]),
                attempts=int(row[2] or 0),
                usage_by_model=row[3] if isinstance(row[3], list) else None,
            )
    return None


async def _upsert(db: AsyncSession, candidate: Candidate, **values: Any) -> None:
    s = AssessmentChatSuggestionSet
    # clock_timestamp(), not now(): the retry wait and the 24 h window compare with it.
    values["updated_at"] = func.clock_timestamp()
    stmt = pg_insert(s).values(
        id=uuid.uuid4(),
        assessment_id=candidate.assessment_id,
        context_tier=candidate.tier,
        verdict_revision=candidate.revision,
        **values,
    )
    await db.execute(
        stmt.on_conflict_do_update(constraint="uq_assessment_chat_suggestions_key", set_=values)
    )


#: (assessment, tier, revision) keys whose record raised while being built. Kept in
#: memory, not stored: no attempt was paid for, and a fix ships with a restart.
_UNBUILDABLE: set[tuple[uuid.UUID, str, int]] = set()
_WARNED: set[str] = set()


def _warn_once(problem: str) -> None:
    """One WARNING per problem per process: the sweep asks every 30 s."""
    if problem not in _WARNED:
        _WARNED.add(problem)
        logger.warning("Assessment chat suggestions: %s; nothing is generated", problem)


async def generate_due(session_factory: async_sessionmaker) -> bool:
    """Generate at most one due set; True when a model call was made (so the worker
    polls for a job before sleeping). Every refusal to spend — the switches, an
    unpriced model, a missing prompt, the daily ceiling, nothing due — returns False."""
    settings = get_settings()
    if not (settings.assessment_chat_enabled and settings.assessment_chat_suggestions_enabled):
        return False
    model = settings.llm_assessment_chat_suggestions_model
    if model not in PRICES:
        _warn_once(f"model {model!r} is not priced in src/services/llm_pricing.py")
        return False
    prompt = load_prompt()
    if prompt is None:
        _warn_once(f"{PROMPT_PATH} is missing or empty")
        return False
    system_prompt, prompt_sha = prompt
    limit = Decimal(str(settings.assessment_chat_suggestions_daily_usd_limit))

    async with session_factory() as db:
        if await spend_24h(db) >= limit:
            return False
        candidate = await next_candidate(db, skip=frozenset(_UNBUILDABLE))
        if candidate is None:
            return False
        key = (candidate.assessment_id, candidate.tier, candidate.revision)
        try:
            loaded = await load_chat_record(db, candidate.assessment_id, tier=candidate.tier)
        except SQLAlchemyError:
            raise  # the database, not this row: the worker loop logs it and tries again
        except Exception:  # noqa: BLE001 — one bad row must not stall the sweep
            logger.exception(
                "Assessment chat suggestions: could not build the %s record of assessment %s;"
                " skipped until the worker restarts", candidate.tier, candidate.assessment_id,
            )
            _UNBUILDABLE.add(key)
            return False
        if loaded is None:
            return False
        record, assessment = loaded
        if (assessment.verdict_revision or 1) != candidate.revision:
            return False  # revised since it was picked; the next sweep takes the new one
        prior = list(candidate.usage_by_model or [])
        # Claimed before the call: the attempt counts, at the reserve, even if the worker
        # dies before storing its result.
        await _upsert(
            db, candidate,
            status=SUGGESTION_STATUS_FAILED, error_code=ERROR_IN_PROGRESS,
            suggestions=None, attempts=candidate.attempts + 1, model=model,
            served_by_model=None, record_sha256_12=record.sha256_12,
            prompt_sha256_12=prompt_sha, usage_by_model=[*prior, _in_flight_entry(model)],
            latency_ms=None,
        )
        await db.commit()

    request = build_request(model=model, system_prompt=system_prompt, record=record)
    started = time.monotonic()
    attempts = candidate.attempts + 1
    try:
        message = await llm.abeta_create(
            llm.get_anthropic_client().with_options(timeout=CALL_TIMEOUT_SECONDS, max_retries=0),
            **request,
        )
    except anthropic.AnthropicError as exc:
        if isinstance(exc, anthropic.APIStatusError):
            entries: list[dict[str, Any]] = []  # an error status is never billed
            if _is_transient(exc):
                attempts = candidate.attempts  # not this assessment's fault
        else:
            entries = [_in_flight_entry(model)]  # timed out or cut off: cost unknown
        outcome = Outcome(SUGGESTION_STATUS_FAILED, None, _api_error_code(exc), entries, None)
    else:
        outcome = outcome_from_message(message, record)
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = [*prior, *(outcome.entries or [])]
    async with session_factory() as db:
        await _upsert(
            db, candidate,
            status=outcome.status, error_code=outcome.error_code,
            suggestions=outcome.suggestions, attempts=attempts, model=model,
            served_by_model=outcome.served_by_model, record_sha256_12=record.sha256_12,
            prompt_sha256_12=prompt_sha, usage_by_model=usage, latency_ms=latency_ms,
        )
        await db.commit()
    # Ids, status, models, tokens and latency — never question text.
    billed = [e for e in outcome.entries or [] if e.get("billed", True) and not e.get("partial")]
    logger.info(
        "Assessment chat suggestions: assessment=%s tier=%s revision=%d attempt=%d "
        "status=%s error=%s served_by=%s kept=%s input=%s output=%s latency_ms=%d",
        candidate.assessment_id, candidate.tier, candidate.revision, attempts,
        outcome.status, outcome.error_code, outcome.served_by_model,
        len(outcome.suggestions or []),
        sum(int(e.get("input_tokens") or 0) + int(e.get("cache_read_input_tokens") or 0)
            + int(e.get("cache_creation_input_tokens") or 0) for e in billed),
        sum(int(e.get("output_tokens") or 0) for e in billed),
        latency_ms,
    )
    return True
