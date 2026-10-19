"""The Interviews page's one line per post: generated per finished interview by the
worker, with each post's first sentence on the page whenever no line is stored.

Generation (``generate_due``) runs in the worker while it has no job: one interview at a
time, newest run first, for interviews that are done (``done_thread_ids``). It sends the
interview's posts, one document each, with ``prompts/interview-trajectory.md``, and asks
for one line per post as structured JSON, each tied to a post by its short id (``P1``,
``P2``, …). A line survives only when that id was listed and it is one plain line; the
stored ``lines`` are keyed by ``str(post.first_row_id)``, the key the page reads. Posts
are sent as plain-text documents, so an interview's own text is material, not
instruction.

A row is written BEFORE the model is called (``failed``/``in_progress``, attempts + 1,
and a placeholder usage entry the ceiling prices at this call's worst case), so a paid
attempt is counted even if the worker dies before storing its result. A failing
interview is retried at most ``MAX_ATTEMPTS`` times, ``RETRY_AFTER`` apart; an
overloaded, rate-limited or 5xx answer (never billed) is retried without using up an
attempt. A refusal is never retried. A row whose ``posts_sha256_12`` no longer matches
the interview's posts (posts landed after it was written) is regenerated with its
attempts restarted. The daily dollar ceiling (``interview_trajectories_daily_usd_limit``)
counts these rows only. Nothing here logs line text.

This module is imported by the worker, which already loads ``src.services.blackbird_rubric``
and ``src.services.rubric_revisions`` through ``src.services.assessment_chat`` (the chat
suggestions generator's import, and this module's ``FALLBACK_BETA``/``entries_cost``), so a
malformed rubric document stops the worker either way. The post helpers it shares with the
page (``src/services/interview_posts.py``) stay clear of both.
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
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import get_settings
from src.models import AgentMessage, InterviewTrajectory, SimulationRun
from src.models.assessment_chat import TOKEN_FIELDS
from src.models.interview_trajectory import (
    TRAJECTORY_STATUS_FAILED,
    TRAJECTORY_STATUS_READY,
    TRAJECTORY_STATUS_REFUSED,
)
from src.services import llm
from src.services.assessment_chat import FALLBACK_BETA, SPEND_RESERVE_USD, entries_cost
from src.services.assessment_chat_stream import served_by, strip_private_use, usage_entries
from src.services.interview_posts import (
    Post,
    done_thread_ids,
    load_posts,
    posts_hash,
    run_interview_roots,
)
from src.services.llm_pricing import PRICES, cost_for_tokens
from src.services.runs import live_run_id

logger = logging.getLogger(__name__)

#: Read per generation: `prompts/` is bind-mounted into the worker, so an edit applies
#: to the next generation (existing rows are kept).
PROMPT_PATH = Path("prompts/interview-trajectory.md")

MAX_LINE_CHARS = 160
#: Under NONSTREAMING_MAX_TOKENS (src/services/llm.py); one line per post fits well inside.
MAX_TOKENS = 8000
MIN_LINE_CHARS = 3
#: Paid attempts per interview (and post set) before the page's first sentences are final.
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)
#: The worker asks for due work at most this often while idle.
SWEEP_INTERVAL_SECONDS = 30.0
#: One call's limit, and no SDK retry (the 30-minute retry is ours): a job waits behind
#: at most this long.
CALL_TIMEOUT_SECONDS = 180.0
_WINDOW = timedelta(hours=24)

ERROR_IN_PROGRESS = "in_progress"

LINES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "post": {"type": "string"},
                    "line": {"type": "string"},
                },
                "required": ["post", "line"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["lines"],
    "additionalProperties": False,
}

_WHITESPACE_RE = re.compile(r"\s+")
#: Anything a plain one-line summary has no business carrying: a link, markup, or a
#: code span. Such a line is dropped rather than repaired.
#: Links, Markdown and tag-shaped markup. A bare ``<`` or ``>`` is science ("IC50 <10 nM",
#: ">90% inhibition") and stays.
_UNWANTED_RE = re.compile(r"https?://|www\.|<[A-Za-z/!?]|`|\]\(", re.IGNORECASE)


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


def post_ids(posts: list[Post]) -> dict[str, Post]:
    """``{"P1": post, ...}``: the posts numbered from 1 in thread order."""
    return {f"P{n}": post for n, post in enumerate(posts, 1)}


def build_request(*, model: str, system_prompt: str, posts: list[Post]) -> dict[str, Any]:
    """One structured-output request over an interview's posts. Citations are switched
    off on every document: the API refuses citations together with
    ``output_config.format``."""
    documents = [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": post.text},
            "title": f"{pid} · {post.speaker} · {post.kind}",
            "citations": {"enabled": False},
        }
        for pid, post in post_ids(posts).items()
    ]
    instruction = f"Posts P1-P{len(posts)} of one interview, in order. Write one line for each."
    return {
        "model": model,
        # Thinking and one short line per post share it; tests/unit/test_trajectory_generation.py
        # pins it under NONSTREAMING_MAX_TOKENS (the ceiling scan sees only literals).
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": LINES_SCHEMA},
        },
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "system": system_prompt,  # abeta_create marks a str system prompt for caching
        "messages": [
            {"role": "user", "content": [*documents, {"type": "text", "text": instruction}]}
        ],
    }


def clean_line(raw: object) -> str | None:
    """The line as stored, or None: one line, whitespace collapsed, private-use and
    invisible format characters (Unicode category Cf: tag characters, zero-width and
    bidi controls) removed, at least MIN_LINE_CHARS long and carrying no link, markup or
    code. A line over MAX_LINE_CHARS is clipped at the last space and ends with ``…``
    (at most MAX_LINE_CHARS characters in all)."""
    if not isinstance(raw, str):
        return None
    visible = "".join(ch for ch in strip_private_use(raw) if unicodedata.category(ch) != "Cf")
    text = _WHITESPACE_RE.sub(" ", visible).strip()
    if len(text) < MIN_LINE_CHARS or _UNWANTED_RE.search(text):
        return None
    if len(text) > MAX_LINE_CHARS:
        head = text[: MAX_LINE_CHARS - 1]
        cut = head.rfind(" ")
        text = (head[:cut] if cut > 0 else head).rstrip() + "…"
    return text


def parse_reply(message: Any, ids: dict[str, Post]) -> tuple[dict[str, str], str | None]:
    """``({post id: line}, error_code)``: the valid lines and None, or what was kept and
    why the reply does not count (`refusal`, `truncated`, `malformed_json`, `too_few`).
    A line naming an id that was not listed, repeating a post, or failing ``clean_line``
    is dropped; a post without a line falls back to its first sentence on the page."""
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason == "refusal":
        return {}, "refusal"
    if stop_reason == "max_tokens":
        return {}, "truncated"
    text = "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}, "malformed_json"
    items = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}, "malformed_json"
    kept: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = item.get("post")
        line = clean_line(item.get("line"))
        if line is None or not isinstance(pid, str):
            continue
        pid = pid.strip()
        if pid not in ids or pid in kept:
            continue
        kept[pid] = line
    if not kept:
        return {}, "too_few"
    return kept, None


@dataclass(frozen=True)
class Outcome:
    status: str
    lines: dict[str, str] | None  # {str(first_row_id): line}
    error_code: str | None
    entries: list[dict[str, Any]] | None
    served_by_model: str | None


def outcome_from_message(message: Any, ids: dict[str, Post]) -> Outcome:
    """What one reply stores. Usage is read first, so a reply this cannot use still
    records what it cost."""
    entries = usage_entries(message)
    model = served_by(message)
    if getattr(message, "stop_reason", None) == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        code = f"refusal:{category}" if category else "refusal"
        return Outcome(TRAJECTORY_STATUS_REFUSED, None, code[:40], entries, model)
    try:
        kept, error = parse_reply(message, ids)
    except Exception:  # noqa: BLE001 — a parsing bug must still store the paid attempt
        logger.exception("Interview trajectories: could not read a reply")
        kept, error = {}, "internal_error"
    if error is not None:
        return Outcome(TRAJECTORY_STATUS_FAILED, None, error, entries, model)
    lines = {str(ids[pid].first_row_id): line for pid, line in kept.items()}
    return Outcome(TRAJECTORY_STATUS_READY, lines, None, entries, model)


def _in_flight_entry(model: str, reserve_input_tokens: int = 0) -> dict[str, Any]:
    """A usage entry for an attempt whose cost is unknown (in flight, timed out, cut off):
    ``partial``, so ``row_cost`` prices it at that attempt's worst case.
    ``reserve_input_tokens`` is the request's estimated input (``estimate_input_tokens``);
    0 means "use RESERVE_INPUT_TOKENS"."""
    return {
        "model": model, "billed": True, "partial": True,
        "reserve_input_tokens": int(reserve_input_tokens),
        **dict.fromkeys(TOKEN_FIELDS, 0),
    }


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
    run_id: uuid.UUID
    thread_id: str
    attempts: int  # paid attempts so far; 0 again once the post set changed
    usage_by_model: list[dict[str, Any]] | None


#: Input-token floor for one attempt's reserve: the largest interview on record is about
#: 32.5 K characters (2026-10-19), roughly 9 K tokens, plus the prompt and the titles. A
#: larger request is reserved at its own estimate (``estimate_input_tokens``).
RESERVE_INPUT_TOKENS = 16_000


def estimate_input_tokens(request: dict[str, Any]) -> int:
    """A generous token estimate for a request: one token per three characters of its
    system prompt and message text (English runs nearer four)."""
    chars = len(str(request.get("system") or ""))
    for message in request.get("messages") or []:
        for block in message.get("content") or []:
            source = block.get("source") or {}
            chars += len(str(block.get("text") or source.get("data") or "")) + len(
                str(block.get("title") or "")
            )
    return -(-chars // 3)


def attempt_reserve(model: str, input_tokens: int = 0) -> Decimal:
    """What one attempt can cost at most: ``max(input_tokens, RESERVE_INPUT_TOKENS)`` in
    and ``MAX_TOKENS`` out at the model's price (about $0.28 on claude-opus-5 at the floor),
    or the chat's reserve for an unpriced model. The chat's $2.50 reserve is sized for its
    own calls; charged here, four timed-out attempts would hold the whole default ceiling
    for a day. A server-side fallback to a pricier model is not priced in."""
    cost = cost_for_tokens(
        model, input_tokens=max(int(input_tokens), RESERVE_INPUT_TOKENS),
        output_tokens=MAX_TOKENS, cache_read=0, cache_creation=0,
    )
    return SPEND_RESERVE_USD if cost is None else cost


def row_cost(usage_by_model: object, model: str) -> Decimal:
    """One row's dollars for the ceiling. A row accumulates every attempt, so each
    ``partial`` entry (an attempt whose cost is unknown) counts its own reserve and every
    other entry its priced cost; an unpriced entry, or a row whose usage was never
    recorded (NULL), counts one reserve. An empty list is only unbilled attempts (429 or
    5xx answers), which cost nothing."""
    if not isinstance(usage_by_model, list):
        return attempt_reserve(model)
    total = Decimal(0)
    for entry in usage_by_model:
        if not isinstance(entry, dict):
            total += attempt_reserve(model)
        elif entry.get("partial"):
            total += attempt_reserve(
                str(entry.get("model") or model), int(entry.get("reserve_input_tokens") or 0)
            )
        else:
            cost = entries_cost([entry])
            total += attempt_reserve(model) if cost is None else cost
    return total


async def spend_24h(db: AsyncSession) -> Decimal:
    """Dollars the trajectory rows touched in the rolling 24 h cost (``row_cost``)."""
    rows = (
        await db.execute(
            select(InterviewTrajectory.usage_by_model, InterviewTrajectory.model).where(
                InterviewTrajectory.updated_at >= func.clock_timestamp() - _WINDOW
            )
        )
    ).all()
    return sum((row_cost(usage, model) for usage, model in rows), Decimal(0))


#: (run, thread) -> (the stored hash last found equal to the interview's posts, the
#: thread's post-row signature then). Saves re-reading every finished interview's posts on
#: each sweep: the posts are re-read only when the stored hash or the signature — the
#: thread's non-panel row count and newest ``created_at``, one grouped query per run —
#: has changed, so a post flushed after the interview's decision (the engine's shutdown
#: flush can land one) regenerates the lines without a worker restart.
_VERIFIED: dict[tuple[uuid.UUID, str], tuple[str, tuple[int, Any]]] = {}


async def _post_signatures(db: AsyncSession, run_id: uuid.UUID) -> dict[str, tuple[int, Any]]:
    """``{thread_id: (non-panel row count, newest created_at)}`` for one run."""
    thread = func.coalesce(AgentMessage.thread_ts, AgentMessage.message_ts)
    rows = await db.execute(
        select(thread, func.count(), func.max(AgentMessage.created_at))
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.phase.in_(("new_post", "thread_reply")),
        )
        .group_by(thread)
    )
    return {key: (int(count), newest) for key, count, newest in rows.all()}
#: (run, thread) keys whose posts could not be read in this process; skipped until a
#: restart (no attempt was paid for, and a fix ships with one).
_UNBUILDABLE: set[tuple[uuid.UUID, str]] = set()
_WARNED: set[str] = set()


def _warn_once(problem: str) -> None:
    """One WARNING per problem per process: the sweep asks every 30 s."""
    if problem not in _WARNED:
        _WARNED.add(problem)
        logger.warning("Interview trajectories: %s; nothing is generated", problem)


async def next_candidate(
    db: AsyncSession, *, skip: frozenset[tuple[uuid.UUID, str]] = frozenset()
) -> Candidate | None:
    """The newest done interview that needs lines: no row yet; a `failed` row with
    attempts left whose last attempt is RETRY_AFTER old; or any row whose
    ``posts_sha256_12`` differs from the interview's posts now (attempts restart).
    Newest run first, newest interview first within it. ``skip`` holds keys whose posts
    could not be read in this process."""
    t = InterviewTrajectory
    live_id = await live_run_id(db)
    runs = (
        await db.execute(select(SimulationRun).order_by(SimulationRun.started_at.desc()))
    ).scalars().all()
    for run in runs:
        roots = await run_interview_roots(db, run.id)
        if not roots:
            continue
        done = await done_thread_ids(db, run, live_id)
        signatures = await _post_signatures(db, run.id)
        rows = {
            row.thread_id: row
            for row in (
                await db.execute(
                    select(
                        t.thread_id, t.status, t.attempts, t.usage_by_model,
                        t.posts_sha256_12,
                        (t.updated_at <= func.clock_timestamp() - RETRY_AFTER).label("retry_due"),
                    ).where(t.simulation_run_id == run.id)
                )
            ).all()
        }
        for root in roots:
            thread_id = root.thread_id
            if thread_id is None or thread_id not in done or (run.id, thread_id) in skip:
                continue
            row = rows.get(thread_id)
            if row is None:
                return Candidate(run.id, thread_id, 0, None)
            usage = row.usage_by_model if isinstance(row.usage_by_model, list) else None
            signature = signatures.get(thread_id, (0, None))
            if _VERIFIED.get((run.id, thread_id)) != (row.posts_sha256_12, signature):
                try:
                    posts = await load_posts(db, run.id, thread_id, full_text=False)
                except SQLAlchemyError:
                    raise
                except Exception as exc:  # the same containment as generate_due's build
                    logger.warning(
                        "Interview trajectories: run=%s thread=%s could not be read (%s); "
                        "skipped until the worker restarts", run.id, thread_id,
                        type(exc).__name__,
                    )
                    _UNBUILDABLE.add((run.id, thread_id))
                    continue
                if posts_hash(posts) != row.posts_sha256_12:
                    return Candidate(run.id, thread_id, 0, usage)
                _VERIFIED[(run.id, thread_id)] = (row.posts_sha256_12, signature)
            if (
                row.status == TRAJECTORY_STATUS_FAILED
                and int(row.attempts) < MAX_ATTEMPTS
                and row.retry_due
            ):
                return Candidate(run.id, thread_id, int(row.attempts), usage)
    return None


async def _upsert(db: AsyncSession, candidate: Candidate, **values: Any) -> None:
    # clock_timestamp(), not now(): the retry wait and the 24 h window compare with it.
    values["updated_at"] = func.clock_timestamp()
    stmt = pg_insert(InterviewTrajectory).values(
        id=uuid.uuid4(),
        simulation_run_id=candidate.run_id,
        thread_id=candidate.thread_id,
        **values,
    )
    await db.execute(
        stmt.on_conflict_do_update(constraint="uq_interview_trajectories_run_thread", set_=values)
    )


async def generate_due(session_factory: async_sessionmaker) -> bool:
    """Generate at most one due interview's lines; True when a model call was made (so
    the worker polls for a job before sleeping). Every refusal to spend — the switch, an
    unpriced model, a missing prompt, the daily ceiling, nothing due — returns False."""
    settings = get_settings()
    if not settings.interview_trajectories_enabled:
        return False
    model = settings.llm_interview_trajectories_model
    if model not in PRICES:
        _warn_once(f"model {model!r} is not priced in src/services/llm_pricing.py")
        return False
    prompt = load_prompt()
    if prompt is None:
        _warn_once(f"{PROMPT_PATH} is missing or empty")
        return False
    system_prompt, prompt_sha = prompt
    limit = Decimal(str(settings.interview_trajectories_daily_usd_limit))

    async with session_factory() as db:
        if await spend_24h(db) >= limit:
            return False
        candidate = await next_candidate(db, skip=frozenset(_UNBUILDABLE))
        if candidate is None:
            return False
        key = (candidate.run_id, candidate.thread_id)
        try:
            posts = await load_posts(db, candidate.run_id, candidate.thread_id, full_text=True)
            ids = post_ids(posts)
            request = build_request(model=model, system_prompt=system_prompt, posts=posts)
        except SQLAlchemyError:
            raise
        except Exception as exc:  # one malformed interview must not stall every other one
            logger.warning(
                "Interview trajectories: run=%s thread=%s could not be built (%s); skipped "
                "until the worker restarts", candidate.run_id, candidate.thread_id,
                type(exc).__name__,
            )
            _UNBUILDABLE.add(key)
            return False
        if not posts:
            _UNBUILDABLE.add(key)
            return False
        current_hash = posts_hash(posts)
        prior = list(candidate.usage_by_model or [])
        # Claimed before the call: the attempt counts, at the reserve, even if the worker
        # dies before storing its result.
        await _upsert(
            db, candidate,
            status=TRAJECTORY_STATUS_FAILED, error_code=ERROR_IN_PROGRESS,
            lines=None, attempts=candidate.attempts + 1, model=model,
            served_by_model=None, prompt_sha256_12=prompt_sha,
            posts_sha256_12=current_hash,
            usage_by_model=[*prior, _in_flight_entry(model, estimate_input_tokens(request))],
            latency_ms=None,
        )
        await db.commit()

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
                attempts = candidate.attempts  # not this interview's fault
        else:
            # Timed out or cut off: cost unknown, reserved at this request's worst case.
            entries = [_in_flight_entry(model, estimate_input_tokens(request))]
        outcome = Outcome(TRAJECTORY_STATUS_FAILED, None, _api_error_code(exc), entries, None)
    else:
        outcome = outcome_from_message(message, ids)
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = [*prior, *(outcome.entries or [])]
    try:
        async with session_factory() as db:
            await _upsert(
                db, candidate,
                status=outcome.status, error_code=outcome.error_code,
                lines=outcome.lines, attempts=attempts, model=model,
                served_by_model=outcome.served_by_model, prompt_sha256_12=prompt_sha,
                posts_sha256_12=current_hash, usage_by_model=usage, latency_ms=latency_ms,
            )
            await db.commit()
    except SQLAlchemyError as exc:
        # Logged by type only: the statement's bound parameters carry the lines, which
        # summarise confidential posts. The claimed row stays failed/in_progress with its
        # reserve and attempt, and is retried within MAX_ATTEMPTS — even when the call itself
        # failed transiently, which only a second, database failure can cause.
        logger.error(
            "Interview trajectories: run=%s thread=%s result not stored (%s)",
            candidate.run_id, candidate.thread_id, type(exc).__name__,
        )
        return True
    # Ids, status, models, tokens and latency — never line text.
    billed = [e for e in outcome.entries or [] if e.get("billed", True) and not e.get("partial")]
    logger.info(
        "Interview trajectories: run=%s thread=%s attempt=%d status=%s error=%s "
        "served_by=%s kept=%s posts=%d input=%s output=%s latency_ms=%d",
        candidate.run_id, candidate.thread_id, attempts,
        outcome.status, outcome.error_code, outcome.served_by_model,
        len(outcome.lines or {}), len(posts),
        sum(int(e.get("input_tokens") or 0) + int(e.get("cache_read_input_tokens") or 0)
            + int(e.get("cache_creation_input_tokens") or 0) for e in billed),
        sum(int(e.get("output_tokens") or 0) for e in billed),
        latency_ms,
    )
    return True
