"""The assessment chat's service layer
(docs/specs/2026-09-24-assessment-chat-design.md §5.1-§5.3, §6, §7.3).

A question travels: the router -> ``prepare_turn`` (every guard, the record, the
request, one committed ``streaming`` turn and its ledger row) -> ``start_turn`` (a
background producer task, and the queue the response relays) -> ``run_turn`` (the
streaming call, then ``_persist`` with the producer's OWN session). The request's
session is committed and released before the model is called, so a streaming answer
holds no pooled connection; and a browser that goes away does not cancel the answer:
the producer finishes, persists it, and it appears the next time the drawer opens.

Nothing here logs question or answer text, or ``str()`` of a database exception — a
``DBAPIError`` message carries its bound parameters, which include both (F11).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_session_factory
from src.models import AssessmentChatTurn, AssessmentChatUsage, SimulationRun
from src.models.assessment_chat import (
    CHAT_REPLAYABLE_STATUSES,
    CHAT_STATUS_FAILED,
    CHAT_STATUS_INTERRUPTED,
    CHAT_STATUS_STREAMING,
    ONE_STREAMING_INDEX,
)
from src.services.assessment_chat_record import ChatRecord, load_chat_record, tier_for
from src.services.assessment_chat_stream import (
    CitationBook,
    Emit,
    StreamOutcome,
    UsageSnapshot,
    billed_sums,
    consume_stream,
    failure_outcome,
    outcome_from_final,
)
from src.services.llm import CLIENT_READ_TIMEOUT_SECONDS
from src.services.llm_pricing import PRICES, cost_for_tokens

logger = logging.getLogger(__name__)

#: §5.1 / §10.1, and coupled: 12 000 output tokens (the literal in build_request) at
#: the ≈60 tok/s measured for this project's Opus-class calls is ≈200 s, inside the
#: 240 s deadline; the 300 s sweep outlasts the deadline, so a live answer is never
#: swept; the heartbeat is well inside nginx's 120 s read timeout (F2).
DEADLINE_SECONDS = 240.0
STALE_AFTER_SECONDS = 300
HEARTBEAT_SECONDS = 15.0
#: D19: the most recent replayable turns up to this many characters (question +
#: answer) are sent; older turns stay visible in the drawer.
HISTORY_REPLAY_MAX_CHARS = 150_000
#: What a question counts toward the dollar ceilings when its cost is not known — in
#: flight, never reported, or on an unpriced model. Above one question's ≈$2.25
#: worst case (F13).
SPEND_RESERVE_USD = Decimal("2.50")
#: The beta that goes with the scalar `fallbacks: "default"` form (D15).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
#: A fixed key for `pg_advisory_xact_lock` (SB-9/PA1-14): derived once from a name
#: rather than a table id, so it needs no migration and collides with nothing else
#: that might someday take an advisory lock. Signed because Postgres's function
#: takes a signed bigint.
_SPEND_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"assessment_chat_spend").digest()[:8], "big", signed=True
)
#: RSEC-3: how long an ask will poll for the spend lock before giving up. Every
#: refusal that does not need the lock is checked first (§the reordering in
#: `prepare_turn`), so only the two dollar-ceiling reads and the row insert ever
#: wait on it — but a stuck holder must still degrade to one 503 rather than
#: pinning a pooled connection open indefinitely, since the pool (5 + 10 overflow)
#: is shared with the whole web tier.
SPEND_LOCK_WAIT_SECONDS = 5.0
_SPEND_LOCK_POLL_SECONDS = 0.05
#: Read per question: `prompts/` is bind-mounted into blackbird-app, so an edit
#: applies to the next question. There is no in-code copy to drift.
PROMPT_PATH = Path("prompts/assessment-chat.md")
_WINDOW = timedelta(hours=24)


class ChatError(Exception):
    """A refusal the router renders as ``{"error": code, **extra}`` with ``status``."""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# The model client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _async_client_for_key(api_key: str) -> anthropic.AsyncAnthropic:
    # The engine's read timeout (CLIENT_READ_TIMEOUT_SECONDS); the 240 s deadline
    # bounds the whole answer, SDK retries included (they stay at the default 2).
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=anthropic.Timeout(CLIENT_READ_TIMEOUT_SECONDS, connect=5.0),
    )


def get_async_anthropic_client() -> anthropic.AsyncAnthropic:
    """The chat's client, and its test seam. ASYNC on purpose: iterating the sync
    client's stream on the event loop would block every other request in the single
    uvicorn worker."""
    return _async_client_for_key(get_settings().anthropic_api_key)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def validate_question(raw: object, *, max_chars: int) -> str:
    """The question to store and send, or ``ChatError(400, "invalid_question")``: not
    a string, blank after stripping, longer than ``max_chars`` code points, holding a
    NUL (Postgres TEXT rejects it) or a lone surrogate (not encodable as UTF-8)."""
    if not isinstance(raw, str):
        raise ChatError(400, "invalid_question")
    question = raw.strip()
    if not question or len(question) > max_chars or chr(0) in question:
        raise ChatError(400, "invalid_question")
    try:
        question.encode("utf-8")
    except UnicodeEncodeError:
        raise ChatError(400, "invalid_question") from None
    return question


def load_system_prompt() -> tuple[str, str]:
    """(prompt text, first 12 hex of its sha256), or ``ChatError(503, "prompt_missing")``."""
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ChatError(503, "prompt_missing") from None
    if not text:
        raise ChatError(503, "prompt_missing")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def replay_window(turns: list[Any]) -> list[Any]:
    """The turns the model sees as history (§5.3): the most recent run of replayable
    turns — `complete` or `truncated`, with a non-blank answer — whose question +
    answer total at most HISTORY_REPLAY_MAX_CHARS, oldest first. ``turns`` is the
    conversation oldest first."""
    window: list[Any] = []
    total = 0
    for turn in reversed(turns):
        if turn.status not in CHAT_REPLAYABLE_STATUSES or not (turn.answer_text or "").strip():
            continue
        size = len(turn.question) + len(turn.answer_text)
        if total + size > HISTORY_REPLAY_MAX_CHARS:
            break
        window.append(turn)
        total += size
    window.reverse()
    return window


def build_messages(record: ChatRecord, window: list[Any], question: str) -> list[dict[str, Any]]:
    """§5.3: the five documents and the first question in the first user message, the
    explicit breakpoint on the last document (so it survives the window moving), then
    each prior answer as plain text — no thinking blocks (F6), no citation objects."""
    documents = [dict(doc) for doc in record.documents]
    documents[-1] = {**documents[-1], "cache_control": {"type": "ephemeral"}}
    first = window[0].question if window else question
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [*documents, {"type": "text", "text": first}]}
    ]
    for i, turn in enumerate(window):
        messages.append({"role": "assistant", "content": [{"type": "text", "text": turn.answer_text}]})
        following = window[i + 1].question if i + 1 < len(window) else question
        messages.append({"role": "user", "content": [{"type": "text", "text": following}]})
    return messages


def build_request(
    *, model: str, effort: str, system_prompt: str, messages: list[dict[str, Any]]
) -> dict[str, Any]:
    """The one place a chat request is shaped (§5.1)."""
    return dict(
        model=model,
        # A LITERAL: tests/unit/test_llm_nonstreaming_ceiling.py scans src/ for
        # literal max_tokens=N (F7). Thinking + answer share it (D17).
        max_tokens=12000,
        system=[{"type": "text", "text": system_prompt}],
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        # Automatic breakpoint for the conversation tail; the explicit one is on the
        # last document (build_messages). No `ttl`: the repo's 5-minute default.
        cache_control={"type": "ephemeral"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )


def _count(value: object) -> int:
    return value if type(value) is int else 0


def entries_cost(entries: list[Any]) -> Decimal | None:
    """Dollars for a row's billed `usage_by_model` entries, or None when one names an
    unpriced model (llm_pricing's rule: never a silent $0)."""
    total = Decimal(0)
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("billed", True):
            continue
        cost = cost_for_tokens(
            str(entry.get("model") or ""),
            input_tokens=_count(entry.get("input_tokens")),
            output_tokens=_count(entry.get("output_tokens")),
            cache_read=_count(entry.get("cache_read_input_tokens")),
            cache_creation=_count(entry.get("cache_creation_input_tokens")),
        )
        if cost is None:
            logger.warning(
                "Assessment chat: the usage ledger names unpriced model %r; that question "
                "counts the %s reserve",
                entry.get("model"), SPEND_RESERVE_USD,
            )
            return None
        total += cost
    return total


def row_spend(usage_by_model: object) -> Decimal:
    """One ledger row's dollars for the ceilings (§6.2 step 8): its priced usage, or
    the reserve when no usage is recorded (in flight, interrupted, never reported), an
    entry is unpriced, or an entry is `partial` (SEC-4/SB-5) — the stream ended before
    a `message_delta` ever landed, so the entry's output count is `message_start`'s
    placeholder of about 1 and understates a genuinely larger answer. A partial row's
    priced cost is therefore only a FLOOR, never the whole answer: the reserve wins
    whichever is larger, and an unpriced model still counts it."""
    if not isinstance(usage_by_model, list):
        return SPEND_RESERVE_USD
    cost = entries_cost(usage_by_model)
    if any(isinstance(entry, dict) and entry.get("partial") for entry in usage_by_model):
        return max(cost or SPEND_RESERVE_USD, SPEND_RESERVE_USD)
    return SPEND_RESERVE_USD if cost is None else cost


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """One Server-Sent Events frame. json.dumps never writes a raw newline, so the
    data is always one line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def sse_stream(
    queue: asyncio.Queue, *, heartbeat: float = HEARTBEAT_SECONDS
) -> AsyncIterator[str]:
    """Relay the producer's queue as SSE until it sends None, writing a ``: ping``
    comment after every ``heartbeat`` seconds of silence (§6.4)."""
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        if item is None:
            return
        event, data = item
        yield sse_frame(event, data)


def turn_payload(row: Any, *, current_sha: str | None, in_window: bool) -> dict[str, Any]:
    """One turn as the GET response and the `done` event carry it (§6.5)."""
    return {
        "id": str(row.id),
        "question": row.question,
        "answer_text": row.answer_text or "",
        "segments": row.answer_segments or [],
        "citations": row.citations or [],
        "allowed_links": row.allowed_links or [],
        "status": row.status,
        "stop_reason": row.stop_reason,
        "refusal_category": row.refusal_category,
        "error_code": row.error_code,
        "served_by_model": row.served_by_model,
        "fallback_used": bool(row.fallback_used),
        "in_window": in_window,
        "record_changed": current_sha is not None and row.record_sha256_12 != current_sha,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _own_session() -> AsyncIterator[AsyncSession]:
    """The producer's own session (§6.2): never the request's, which is committed and
    released before the model is called. The test seam for integration tests."""
    async with get_session_factory()() as session:
        yield session


def _since(delta: timedelta):
    # clock_timestamp(), not now(): now() is the transaction's START, so inside one
    # long transaction (every integration test is one) it would misjudge every age.
    return func.clock_timestamp() - delta


async def sweep_stale(db: AsyncSession) -> int:
    """Mark EVERY user's `streaming` turn and ledger row older than
    STALE_AFTER_SECONDS `interrupted` (§7.3). Frees a user whose answer died with the
    process; a live answer is never older than the 240 s deadline."""
    cutoff = _since(timedelta(seconds=STALE_AFTER_SECONDS))
    await db.execute(
        update(AssessmentChatUsage)
        .where(
            AssessmentChatUsage.status == CHAT_STATUS_STREAMING,
            AssessmentChatUsage.created_at < cutoff,
        )
        .values(status=CHAT_STATUS_INTERRUPTED, completed_at=func.clock_timestamp())
        .execution_options(synchronize_session=False)
    )
    swept = (
        await db.execute(
            update(AssessmentChatTurn)
            .where(
                AssessmentChatTurn.status == CHAT_STATUS_STREAMING,
                AssessmentChatTurn.created_at < cutoff,
            )
            .values(status=CHAT_STATUS_INTERRUPTED, completed_at=func.clock_timestamp())
            .returning(AssessmentChatTurn.id)
            .execution_options(synchronize_session=False)
        )
    ).scalars().all()
    if swept:
        logger.warning(
            "Assessment chat: swept %d stale streaming turn(s) to interrupted: %s",
            len(swept), ", ".join(str(turn_id) for turn_id in swept),
        )
    return len(swept)


async def _questions_in_window(
    db: AsyncSession, *, user_id: uuid.UUID
) -> tuple[int, datetime | None]:
    count, oldest = (
        await db.execute(
            select(func.count(AssessmentChatUsage.id), func.min(AssessmentChatUsage.created_at))
            .where(
                AssessmentChatUsage.user_id == user_id,
                AssessmentChatUsage.created_at >= _since(_WINDOW),
            )
        )
    ).one()
    return int(count), oldest


async def questions_used_24h(db: AsyncSession, *, user_id: uuid.UUID) -> int:
    """Every accepted question counts, whatever its outcome (§6.2 step 7)."""
    return (await _questions_in_window(db, user_id=user_id))[0]


async def spend_24h(db: AsyncSession, *, user_id: uuid.UUID | None = None) -> Decimal:
    """Dollars in the rolling 24 h window — one user's, or everyone's."""
    query = select(AssessmentChatUsage.usage_by_model).where(
        AssessmentChatUsage.created_at >= _since(_WINDOW)
    )
    if user_id is not None:
        query = query.where(AssessmentChatUsage.user_id == user_id)
    usages = (await db.execute(query)).scalars().all()
    return sum((row_spend(usage) for usage in usages), Decimal(0))


async def verdict_may_change(db: AsyncSession, assessment: Any) -> bool:
    """True while the engine can still supersede — and so delete — this row (F10,
    SB-10): its headline has not been announced, and its run is either still
    `running` or is the run `src/agent/main.py` would resume on the next restart —
    the LATEST run by `started_at`, whatever that run's own `status` says. A
    stopped run that is not the latest can never be resumed and so can never
    supersede anything again."""
    if assessment.summary_posted_at is not None:
        return False
    status = await db.scalar(
        select(SimulationRun.status).where(SimulationRun.id == assessment.simulation_run_id)
    )
    if status == "running":
        return True
    latest_id = await db.scalar(
        select(SimulationRun.id).order_by(SimulationRun.started_at.desc()).limit(1)
    )
    return latest_id == assessment.simulation_run_id


async def _conversation(
    db: AsyncSession, *, assessment_id: uuid.UUID, user_id: uuid.UUID, tier: str
) -> list[AssessmentChatTurn]:
    rows = await db.execute(
        select(AssessmentChatTurn)
        .where(
            AssessmentChatTurn.assessment_id == assessment_id,
            AssessmentChatTurn.user_id == user_id,
            AssessmentChatTurn.context_tier == tier,
        )
        .order_by(AssessmentChatTurn.created_at, AssessmentChatTurn.id)
        # A turn already in the identity map (a test's shared session) is refreshed
        # from the row, never served stale.
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedTurn:
    """An accepted question, committed as a `streaming` turn and its ledger row."""

    turn_id: uuid.UUID
    usage_id: uuid.UUID
    assessment_id: uuid.UUID
    user_id: uuid.UUID
    tier: str
    model: str
    created_at: datetime
    record: ChatRecord
    request: dict[str, Any]
    daily_limit: int
    started: float  # time.monotonic() when the question was accepted


def _new_rows(
    *,
    assessment_id: uuid.UUID,
    user_id: uuid.UUID,
    tier: str,
    question: str,
    model: str,
    record_sha: str,
    prompt_sha: str,
    created_at: datetime,
) -> tuple[AssessmentChatTurn, AssessmentChatUsage]:
    turn = AssessmentChatTurn(
        id=uuid.uuid4(),
        assessment_id=assessment_id,
        user_id=user_id,
        context_tier=tier,
        question=question,
        answer_text="",
        status=CHAT_STATUS_STREAMING,
        model=model,
        fallback_used=False,
        record_sha256_12=record_sha,
        prompt_sha256_12=prompt_sha,
        created_at=created_at,
    )
    usage = AssessmentChatUsage(
        id=uuid.uuid4(),
        turn_id=turn.id,
        user_id=user_id,
        assessment_id=assessment_id,
        context_tier=tier,
        model=model,
        status=CHAT_STATUS_STREAMING,
        created_at=created_at,
    )
    return turn, usage


async def _has_streaming_turn(db: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """True while this user (any assessment, any tier) already has a `streaming`
    turn — the same predicate ONE_STREAMING_INDEX enforces at insert time."""
    return bool(
        await db.scalar(
            select(func.count(AssessmentChatTurn.id)).where(
                AssessmentChatTurn.user_id == user_id,
                AssessmentChatTurn.status == CHAT_STATUS_STREAMING,
            )
        )
    )


async def _refuse_over_caps(
    db: AsyncSession, *, assessment_id: uuid.UUID, user_id: uuid.UUID, tier: str
) -> list[AssessmentChatTurn]:
    """Raise for a full conversation (409) or a used-up daily question count (429);
    otherwise return the conversation, oldest first."""
    settings = get_settings()
    turns = await _conversation(db, assessment_id=assessment_id, user_id=user_id, tier=tier)
    if len(turns) >= settings.assessment_chat_max_turns:
        raise ChatError(409, "conversation_full")
    used, oldest = await _questions_in_window(db, user_id=user_id)
    if used >= settings.assessment_chat_daily_question_limit:
        resets_at = (oldest + _WINDOW).isoformat() if oldest is not None else None
        raise ChatError(429, "daily_limit", resets_at=resets_at)
    return turns


async def _take_spend_lock(db: AsyncSession) -> None:
    """Take `_SPEND_LOCK_KEY`'s advisory lock with a BOUNDED wait (RSEC-3): poll
    `pg_try_advisory_xact_lock` every `_SPEND_LOCK_POLL_SECONDS` until
    `SPEND_LOCK_WAIT_SECONDS` has elapsed, then raise `ChatError(503, "busy")` rather
    than queue forever. `pg_advisory_xact_lock`'s own unbounded wait is exactly the
    hazard this replaces: an ask stuck on it pins a pooled connection open, and the
    pool is shared with the whole web tier."""
    deadline = time.monotonic() + SPEND_LOCK_WAIT_SECONDS
    while True:
        got = await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _SPEND_LOCK_KEY})
        if got:
            return
        if time.monotonic() >= deadline:
            raise ChatError(503, "busy")
        await asyncio.sleep(_SPEND_LOCK_POLL_SECONDS)


async def prepare_turn(
    db: AsyncSession, *, assessment_id: uuid.UUID, user: Any, question_raw: object
) -> PreparedTurn:
    """§6.2 steps 2-10, cheapest first; the router has already refused impersonation,
    a disabled chat, an unknown assessment and a non-JSON body. Raises ChatError for
    every refusal and commits on success.

    RSEC-3: every earlier release of this function serialized EVERY ask — refusals
    included — behind one unbounded advisory lock, held across the record build. A
    reviewer firing many concurrent asks could queue everyone else behind it, and
    each queued request pins a pooled connection open. Now only the two
    dollar-ceiling reads and the row insert run under the lock, and the lock itself
    has a bounded wait (`_take_spend_lock`): every cheaper refusal — the sweep, a
    repeat click, a full conversation, the daily question count, an unknown
    assessment — is checked first, unlocked, so it can never queue on the lock at
    all."""
    settings = get_settings()
    question = validate_question(question_raw, max_chars=settings.assessment_chat_max_question_chars)
    model = settings.llm_assessment_chat_model
    if model not in PRICES:
        raise ChatError(503, "model_unpriced")
    system_prompt, prompt_sha = load_system_prompt()
    # Committed on its own, before anything takes the lock: an ask that took the
    # lock first could otherwise wait, through the one-in-flight partial unique
    # index, on a row THIS sweep is trying to update, and the two would deadlock.
    await sweep_stale(db)
    await db.commit()
    user_id = user.id
    # A repeat click while an answer is still streaming is refused here, unlocked,
    # so it never queues on the lock at all. ONE_STREAMING_INDEX at insert time
    # (below) remains the actual authority — a turn can still start between this
    # check and the insert — this is only a cheap head start on the common case.
    if await _has_streaming_turn(db, user_id=user_id):
        raise ChatError(409, "answer_in_progress")
    tier = tier_for(user)
    turns = await _refuse_over_caps(db, assessment_id=assessment_id, user_id=user_id, tier=tier)
    loaded = await load_chat_record(db, assessment_id, tier=tier)
    if loaded is None:
        raise ChatError(404, "not_found")
    record = loaded[0]
    request = build_request(
        model=model,
        effort=settings.assessment_chat_effort,
        system_prompt=system_prompt,
        messages=build_messages(record, replay_window(turns), question),
    )
    # Serialize every ask's check-then-insert against the two dollar ceilings
    # (SB-9/PA1-14): without this, two concurrent requests can both read the $100
    # global total as under the ceiling before either has committed its own spend,
    # so the ceiling overshoots by more than one question. Everything above this
    # line is unlocked; from here this ask holds the lock until its own commit below
    # releases it — a ChatError raised past this point still ends this function's
    # transaction, through the request session's own commit or rollback once the
    # router returns, which releases the lock the same way.
    await _take_spend_lock(db)
    # The per-user caps were read unlocked above, as a cheap head start; another ask
    # of this user's may have committed its turn since, so both are read again here,
    # under the lock, before anything is inserted (R2SEC-3).
    await _refuse_over_caps(db, assessment_id=assessment_id, user_id=user_id, tier=tier)
    if await spend_24h(db, user_id=user_id) >= Decimal(str(settings.assessment_chat_daily_user_usd_limit)):
        raise ChatError(429, "daily_spend_limit")
    if await spend_24h(db) >= Decimal(str(settings.assessment_chat_daily_total_usd_limit)):
        raise ChatError(429, "global_spend_limit")
    created_at = datetime.now(UTC)
    turn, usage = _new_rows(
        assessment_id=assessment_id,
        user_id=user_id,
        tier=tier,
        question=question,
        model=model,
        record_sha=record.sha256_12,
        prompt_sha=prompt_sha,
        created_at=created_at,
    )
    try:
        # A SAVEPOINT, so the one-in-flight IntegrityError rolls back only these two
        # rows and leaves the session usable.
        async with db.begin_nested():
            db.add_all([turn, usage])
    except IntegrityError as exc:
        if ONE_STREAMING_INDEX in str(getattr(exc, "orig", "")):
            raise ChatError(409, "answer_in_progress") from None
        raise
    await db.commit()
    return PreparedTurn(
        turn_id=turn.id,
        usage_id=usage.id,
        assessment_id=assessment_id,
        user_id=user_id,
        tier=tier,
        model=model,
        created_at=created_at,
        record=record,
        request=request,
        daily_limit=settings.assessment_chat_daily_question_limit,
        started=time.monotonic(),
    )


_LIVE_TASKS: set[asyncio.Task] = set()


def start_turn(prepared: PreparedTurn) -> asyncio.Queue:
    """Start the producer and return the queue its SSE events arrive on. The task is
    referenced from `_LIVE_TASKS` until it finishes, so it is never garbage-collected
    mid-answer, and it is NOT tied to the request: a closed tab does not waste a paid
    answer (§6.3)."""
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(run_turn(prepared, queue), name=f"assessment-chat-{prepared.turn_id}")
    _LIVE_TASKS.add(task)
    task.add_done_callback(_LIVE_TASKS.discard)
    return queue


async def drain_live_tasks(timeout: float | None = None) -> None:
    """Wait until every in-flight answer has persisted (tests, orderly shutdown).

    ``timeout=None`` (the default, and what every test relies on) waits for all of
    them with no limit. A numeric ``timeout`` — used only by `create_app`'s
    shutdown hook (SB-11) — gives up after that many seconds and lets the process
    exit anyway; a task still running past it is left for `sweep_stale`'s own
    STALE_AFTER_SECONDS window to mark ``interrupted`` on the next request.
    ``asyncio.wait`` (not `gather`) is used deliberately: it never raises a live
    task's own exception into this caller, and it returns cleanly on a timeout
    rather than cancelling anything.
    """
    tasks = list(_LIVE_TASKS)
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


def _where(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return f"{frames[-1].filename}:{frames[-1].lineno}" if frames else "unknown"


async def _answer(prepared: PreparedTurn, emit: Emit, snapshot: UsageSnapshot) -> StreamOutcome:
    """One streaming call under the deadline (§5.1), mapped to an outcome (§5.6).
    SDK exceptions are matched by class, most specific first — never by message."""
    client = get_async_anthropic_client()
    model = prepared.model
    try:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with client.beta.messages.stream(**prepared.request) as stream:
                await consume_stream(
                    stream, emit=emit, snapshot=snapshot, book=CitationBook(prepared.record)
                )
                final = await stream.get_final_message()
    except TimeoutError:
        return failure_outcome(error_code="timeout", requested_model=model, snapshot=snapshot)
    except anthropic.RateLimitError:
        return failure_outcome(
            error_code="upstream_rate_limited", requested_model=model, snapshot=snapshot,
            known_unbilled=True,
        )
    except anthropic.BadRequestError as exc:
        logger.error(
            "Assessment chat: the API rejected turn %s as a bad request (request id %s)",
            prepared.turn_id, getattr(exc, "request_id", None),
        )
        return failure_outcome(
            error_code="upstream_bad_request", requested_model=model, snapshot=snapshot,
            known_unbilled=True,
        )
    except anthropic.APIStatusError as exc:
        # A mid-stream error EVENT (both SDKs, verified in anthropic/_streaming.py)
        # raises this same class over the stream's own HTTP 200 response, with the
        # SSE error's body attached — so status_code alone cannot tell "never
        # reached the API" (a real HTTP error) from "the API answered, then failed
        # partway through" (billing unknown). Classify by the body's declared error
        # type first; 529 is the only other case this code recognized before that
        # mid-stream shape existed.
        status_code = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        error_type = error.get("type") if isinstance(error, dict) else None
        if error_type == "overloaded_error" or status_code == 529:
            code = "upstream_overloaded"
        elif error_type == "rate_limit_error":
            code = "upstream_rate_limited"
        else:
            code = "upstream_error"
        return failure_outcome(
            error_code=code, requested_model=model, snapshot=snapshot,
            known_unbilled=status_code != 200,
        )
    except anthropic.APIConnectionError:
        return failure_outcome(error_code="upstream_error", requested_model=model, snapshot=snapshot)
    # RS-1: the regex-based link rewrite inside outcome_from_final can run long on a
    # pathological answer; off the event loop so a slow rewrite can never block every
    # other request this single uvicorn worker is serving. Thread-safe because it
    # only reads `prepared.record` and logs — it writes nothing shared.
    return await asyncio.to_thread(
        outcome_from_final, final, record=prepared.record, requested_model=model
    )


def _log_completion(
    prepared: PreparedTurn, outcome: StreamOutcome, latency_ms: int, sums: dict[str, int | None]
) -> None:
    # Ids, tier, status, models, tokens and latency — never content (§10.3).
    logger.info(
        "Assessment chat turn %s: user=%s assessment=%s tier=%s status=%s stop=%s model=%s "
        "served_by=%s fallback=%s input=%s output=%s cache_read=%s cache_write=%s latency_ms=%d",
        prepared.turn_id, prepared.user_id, prepared.assessment_id, prepared.tier,
        outcome.status, outcome.stop_reason, prepared.model, outcome.served_by_model,
        outcome.fallback_used, sums["input_tokens"], sums["output_tokens"],
        sums["cache_read_input_tokens"], sums["cache_creation_input_tokens"], latency_ms,
    )


async def _persist(
    prepared: PreparedTurn, outcome: StreamOutcome
) -> tuple[str, dict[str, Any] | None]:
    """Write one outcome (§6.3). The ledger row is ALWAYS updated — the tokens were
    billed either way. The turn is updated only while it is still `streaming`, so an
    answer never overwrites a turn that was swept to `interrupted`, and a turn that
    CASCADEd away with its assessment stays gone.

    Returns ("ok", done-payload), ("gone", None) when the turn no longer accepts the
    answer, or ("error", None) when the database refused the write."""
    latency_ms = int((time.monotonic() - prepared.started) * 1000)
    sums = billed_sums(outcome.usage_by_model)
    payload: dict[str, Any] | None = None
    try:
        async with _own_session() as db:
            try:
                await db.execute(
                    update(AssessmentChatUsage)
                    .where(AssessmentChatUsage.id == prepared.usage_id)
                    .values(
                        status=outcome.status,
                        served_by_model=outcome.served_by_model,
                        usage_by_model=outcome.usage_by_model,
                        completed_at=func.clock_timestamp(),
                        **sums,
                    )
                    .execution_options(synchronize_session=False)
                )
                updated = (
                    await db.execute(
                        update(AssessmentChatTurn)
                        .where(
                            AssessmentChatTurn.id == prepared.turn_id,
                            AssessmentChatTurn.status == CHAT_STATUS_STREAMING,
                        )
                        .values(
                            status=outcome.status,
                            answer_text=outcome.answer_text,
                            answer_segments=outcome.segments or None,
                            citations=outcome.citations or None,
                            allowed_links=outcome.allowed_links or None,
                            stop_reason=outcome.stop_reason,
                            refusal_category=outcome.refusal_category,
                            error_code=outcome.error_code,
                            served_by_model=outcome.served_by_model,
                            fallback_used=outcome.fallback_used,
                            latency_ms=latency_ms,
                            completed_at=func.clock_timestamp(),
                        )
                        .returning(AssessmentChatTurn.id)
                        .execution_options(synchronize_session=False)
                    )
                ).scalars().first()
                if updated is not None:
                    row = (
                        await db.execute(
                            select(AssessmentChatTurn)
                            .where(AssessmentChatTurn.id == updated)
                            .execution_options(populate_existing=True)
                        )
                    ).scalar_one()
                    conversation = await _conversation(
                        db, assessment_id=prepared.assessment_id, user_id=prepared.user_id,
                        tier=prepared.tier,
                    )
                    in_window = row.id in {t.id for t in replay_window(conversation)}
                    payload = {
                        "turn": turn_payload(
                            row, current_sha=prepared.record.sha256_12, in_window=in_window
                        ),
                        "questions_used_24h": await questions_used_24h(db, user_id=prepared.user_id),
                        "daily_limit": prepared.daily_limit,
                    }
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    except Exception as exc:
        # Not narrowed to SQLAlchemyError (SB-1/SW-5): a session that cannot even be
        # OPENED (e.g. the pool or the network is down) raises outside the inner
        # try/rollback, and the producer must still end the turn rather than hang
        # the SSE stream. Class name only, never the exception's str() — a
        # DBAPIError's message carries its bound parameters (F11).
        logger.error(
            "Assessment chat: could not persist turn %s (usage %s): %s",
            prepared.turn_id, prepared.usage_id, type(exc).__name__,
        )
        return "error", None
    _log_completion(prepared, outcome, latency_ms, sums)
    if payload is None:
        logger.warning(
            "Assessment chat: turn %s was swept or deleted while it was being answered; "
            "the answer is dropped and its usage is recorded",
            prepared.turn_id,
        )
        return "gone", None
    return "ok", payload


async def _mark_storage_failed(prepared: PreparedTurn) -> None:
    """Best-effort (SB-1/SW-5): when `_persist` could not write the answer at all,
    flip the still-`streaming` turn to `failed`/`storage_error` directly, on its own
    session, so the user's very next question is accepted at once rather than
    waiting up to STALE_AFTER_SECONDS for `sweep_stale`. The ledger row is left
    alone — it has no usage recorded, so it keeps counting the reserve either way.
    Any failure here is only logged, by class name, never content."""
    try:
        async with _own_session() as db:
            await db.execute(
                update(AssessmentChatTurn)
                .where(
                    AssessmentChatTurn.id == prepared.turn_id,
                    AssessmentChatTurn.status == CHAT_STATUS_STREAMING,
                )
                .values(
                    status=CHAT_STATUS_FAILED,
                    error_code="storage_error",
                    completed_at=func.clock_timestamp(),
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as exc:
        logger.error(
            "Assessment chat: could not mark turn %s failed after a storage error: %s",
            prepared.turn_id, type(exc).__name__,
        )


async def run_turn(prepared: PreparedTurn, queue: asyncio.Queue) -> None:
    """The producer (§6.3): always persists, always ends the queue with ``None``.
    ``done`` for complete/truncated/refused, ``error {code}`` for a failed answer,
    ``error {"code": "storage_error"}`` when the answer could not be saved at all —
    which also marks the turn `failed` directly (`_mark_storage_failed`), so a retry
    need not wait for the sweep."""

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    snapshot = UsageSnapshot()
    try:
        try:
            await emit(
                "turn",
                {
                    "turn_id": str(prepared.turn_id),
                    "created_at": prepared.created_at.isoformat(),
                    "tier": prepared.tier,
                },
            )
            outcome = await _answer(prepared, emit, snapshot)
        except asyncio.CancelledError:
            # The process is going down mid-answer: record what is known, then let
            # the cancellation proceed — the `finally` below still ends the queue.
            outcome = failure_outcome(
                error_code=None, requested_model=prepared.model, snapshot=snapshot,
                status=CHAT_STATUS_INTERRUPTED,
            )
            await asyncio.shield(_persist(prepared, outcome))
            raise
        except Exception as exc:  # the producer must never die without persisting
            logger.error(
                "Assessment chat: unexpected %s while answering turn %s (at %s)",
                type(exc).__name__, prepared.turn_id, _where(exc),
            )
            outcome = failure_outcome(
                error_code="upstream_error", requested_model=prepared.model, snapshot=snapshot
            )
        result, payload = await asyncio.shield(_persist(prepared, outcome))
        if result == "error":
            await _mark_storage_failed(prepared)
        if result == "ok" and payload is not None and outcome.status != CHAT_STATUS_FAILED:
            await emit("done", payload)
        elif result == "ok":
            await emit("error", {"code": outcome.error_code or "upstream_error"})
        else:
            await emit("error", {"code": "storage_error"})
    finally:
        # Guaranteed on every path — cancellation, an emit that itself raises, or
        # the normal end — so the SSE response always completes rather than hanging.
        queue.put_nowait(None)


# ---------------------------------------------------------------------------
# History and Clear
# ---------------------------------------------------------------------------


async def list_history(
    db: AsyncSession, *, assessment_id: uuid.UUID, user: Any
) -> dict[str, Any] | None:
    """The GET shape (§6.5), or None for an unknown assessment. Sweeps first; builds
    the current record so each turn can say whether its record has changed."""
    settings = get_settings()
    user_id = user.id
    tier = tier_for(user)
    await sweep_stale(db)
    loaded = await load_chat_record(db, assessment_id, tier=tier)
    if loaded is None:
        return None
    record, assessment = loaded
    turns = await _conversation(db, assessment_id=assessment_id, user_id=user_id, tier=tier)
    window = {turn.id for turn in replay_window(turns)}
    return {
        "tier": tier,
        "turns": [
            turn_payload(turn, current_sha=record.sha256_12, in_window=turn.id in window)
            for turn in turns
        ],
        "questions_used_24h": await questions_used_24h(db, user_id=user_id),
        "daily_limit": settings.assessment_chat_daily_question_limit,
        "max_question_chars": settings.assessment_chat_max_question_chars,
        "max_turns": settings.assessment_chat_max_turns,
        "verdict_may_change": await verdict_may_change(db, assessment),
    }


async def clear_history(db: AsyncSession, *, assessment_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Delete the caller's turns on this assessment, every tier (D16). Refuses while one
    is still streaming after the sweep, and never deletes a streaming turn even if one
    starts meanwhile. The ledger rows survive (`turn_id` SET NULL), which is why Clear
    cannot reset the caps."""
    await sweep_stale(db)
    in_flight = await db.scalar(
        select(func.count(AssessmentChatTurn.id)).where(
            AssessmentChatTurn.assessment_id == assessment_id,
            AssessmentChatTurn.user_id == user_id,
            AssessmentChatTurn.status == CHAT_STATUS_STREAMING,
        )
    )
    if in_flight:
        raise ChatError(409, "answer_in_progress")
    deleted = (
        await db.execute(
            delete(AssessmentChatTurn)
            .where(
                AssessmentChatTurn.assessment_id == assessment_id,
                AssessmentChatTurn.user_id == user_id,
                AssessmentChatTurn.status != CHAT_STATUS_STREAMING,
            )
            .returning(AssessmentChatTurn.id)
            .execution_options(synchronize_session=False)
        )
    ).scalars().all()
    await db.commit()
    return len(deleted)
