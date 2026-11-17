"""Message Batches for the batched stages, with the intent protocol (spec §7.1, D31, X4).

The protocol, in order:

1. ``submit`` commits a ``landscape_batches`` row (``submitting``, database-generated ``seq``),
   one ``landscape_batch_items`` row per request, and moves the stage rows to ``submitting``
   with their ``batch_item_id``, all BEFORE ``batches.create``. Each request's ``custom_id``
   is ``b{seq}-{item_seq}``, which fits the API's ``^[a-zA-Z0-9_-]{1,64}$``.
2. After ``create`` returns, the API batch id is stored and the rows move to ``submitted``.
3. A crash between 1 and 2, or a ``create`` that raised (it may still have made a batch), is
   settled by ``reconcile_orphans``: it lists the API's batches once per scan, waits until
   every listed batch has ended, then matches their ``b{seq}-`` prefixes to the rows still
   ``submitting``. A match is adopted; a row with no match is resubmitted. Until then its
   stage rows stay ``submitting``, so nothing is billed twice.
4. ``poll`` streams an ended batch's results in chunks; ingestion is a compare-and-set
   (``apply_result``), so a replayed chunk changes nothing, and ledger rows are
   insert-ignore on ``(batch_id, item_seq)``.

This module owns the protocol and the generic non-success transitions (``transition``,
``run_fallbacks``); each stage module owns its ``succeeded`` handling.

**Sync mode** (``landscape_batch_mode = "sync"``) is for an organization whose Batches endpoint
is unavailable (a HIPAA-configured one, measured 2026-11-16). The batch row, its items and the
stage rows are written as above, but ``submit`` makes no API call: it stores
``local:<row id>`` as the API id and keeps the requests in memory. ``poll`` then runs each
unanswered item as its own Messages call, a chunk at a time, on the batch's model only (a
refusal still goes to ``run_fallbacks``), each stage's batches in a lane of their own beside
the other stages' (D75). Each answer is ledgered at the synchronous rate with
the batch id and ``item_seq`` (insert-ignore), and its outcome goes through the same ingestion.
Answers stay in memory until ``mark_ingested``, so a chunk whose ingestion failed is replayed,
not paid for again. A restart loses the requests: the batch's rows still ``submitted`` go back
to ``pending`` for their stage to batch again. No API batch exists in this mode, so
reconciliation lists nothing and releases every unconfirmed row to ``pending``. Switch modes
only while no API batch is in flight.

The SDK client is synchronous, so every API call runs on a worker thread. Only
``create``/``retrieve``/``results``/``list`` are used: they exist in both the test venv's and
the images' ``anthropic``.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anthropic
from sqlalchemy import String, Table, any_, bindparam, func, insert, select, update
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import Base
from src.landscaping.ledger import record_call, usage_from
from src.landscaping.llm_client import CallResult, call_sync, require_priced
from src.models.landscape import LandscapeBatch, LandscapeBatchItem
from src.services import llm
from src.services.job_queue import job_error_text

logger = logging.getLogger(__name__)

OUTCOME_KINDS = ("succeeded", "refusal", "truncated", "errored", "expired", "canceled")

#: A batch row's items become ``failed:create`` only after this many failed creates, and only
#: once the last re-armed scan has found no batch for it (spec §7.1 step 4).
_MAX_CREATE_FAILURES = 3
#: Listing starts this long before the oldest unconfirmed row, to absorb clock skew between
#: the database and the API.
_LIST_SKEW = timedelta(minutes=10)
_LIST_PAGE_SIZE = 100
_CUSTOM_ID = re.compile(r"b(\d+)-(\d+)")
#: The ``api_batch_id`` prefix of a batch run in-process (sync mode).
LOCAL_PREFIX = "local:"
#: Items per sync-mode chunk: each is called, ledgered and yielded for ingestion before the
#: next starts, so a stop between chunks loses no answer.
_LOCAL_CHUNK = 50


def _sync_mode() -> bool:
    return get_settings().landscape_batch_mode == "sync"


def _is_local(batch: LandscapeBatch) -> bool:
    return (batch.api_batch_id or "").startswith(LOCAL_PREFIX)


def _batches_client() -> Any:
    """The SDK's ``messages.batches`` resource on the shared synchronous client."""
    return llm.get_anthropic_client().messages.batches


def _custom_id(seq: int, item_seq: int) -> str:
    return f"b{seq}-{item_seq}"


def _parse_custom_id(custom_id: str) -> tuple[int, int] | None:
    """``(seq, item_seq)`` of a ``b{seq}-{item_seq}`` id, or None for any other id."""
    match = _CUSTOM_ID.fullmatch(custom_id or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _touch(table: Table) -> dict:
    return {"updated_at": func.now()} if "updated_at" in table.c else {}


def _uuid_array(name: str, ids: list[uuid.UUID]) -> Any:
    """``ANY(:name)`` over one array parameter, so 100,000 ids do not exceed asyncpg's
    32,767-parameter limit as an expanded ``IN`` would."""
    return any_(bindparam(name, ids, type_=ARRAY(UUID(as_uuid=True))))


# --- in-process reconciliation state -------------------------------------------------------


@dataclass
class _Scan:
    """One reconciliation scan over a fixed list of API batches (spec §7.1 step 4)."""

    #: The ``landscape_batches`` rows it settles.
    rows: set[uuid.UUID]
    #: API batch ids, listed once on the scan's first advance and then fixed.
    listed: list[str] | None = None
    ended: set[str] = field(default_factory=set)
    #: ``seq`` → API batch id, read once every listed batch has ended.
    matches: dict[int, str] | None = None


class _Reconciler:
    """The worker's reconciliation state. It lives in memory: a restart starts again with the
    startup scan over every row still ``submitting``."""

    def __init__(self) -> None:
        self.started = False
        self.scans: list[_Scan] = []
        #: Requests of rows whose create failed in this process, kept for their re-create.
        self.requests: dict[uuid.UUID, list[dict]] = {}
        #: Sync mode: each local batch's requests by ``item_seq``, and the answers it has had,
        #: ``item_seq`` → ``(kind, message)``, kept until the batch is marked ingested.
        self.local_requests: dict[uuid.UUID, dict[int, dict]] = {}
        self.local_answers: dict[uuid.UUID, dict[int, tuple[str, Any | None]]] = {}

    def rearm(self, batch_row_id: uuid.UUID) -> None:
        """Give the row a scan of its own, listed on its next advance. Earlier scans drop it:
        the new list, fixed later, covers every batch theirs would."""
        for scan in self.scans:
            scan.rows.discard(batch_row_id)
        self.scans = [scan for scan in self.scans if scan.rows]
        self.scans.append(_Scan(rows={batch_row_id}))


_RECONCILER = _Reconciler()


# --- submit ---------------------------------------------------------------------------------


async def submit(
    db_factory, *, stage: str, row_model: type, items: list[tuple[uuid.UUID, dict]]
) -> uuid.UUID:
    """Submit ``items`` (``(stage_row_id, request)`` pairs) as one Message Batch.

    Every request must name the same priced model (``require_priced`` runs before any write).
    Only rows that are ``pending`` with no ``fallback_from`` are taken: a row waiting for the
    fallback chain is never batched (D42); ``ValueError`` when none qualifies. The intent rows
    commit before ``create``. A ``create`` that raises is not re-raised: the rows stay
    ``submitting``, ``create_failures`` grows and reconciliation is re-armed for the row.
    In sync mode nothing is sent: the row is adopted under a ``local:`` id for ``poll`` to run.
    Returns the ``landscape_batches`` row id.
    """
    if not items:
        raise ValueError("submit needs at least one item")
    models = {request["model"] for _, request in items}
    for model in sorted(models):
        require_priced(model)
    if len(models) != 1:
        raise ValueError(f"one model per batch, got {sorted(models)}")
    model = models.pop()
    table = row_model.__table__
    async with db_factory() as db:
        eligible = set(
            (
                await db.scalars(
                    select(table.c.id)
                    .where(
                        table.c.id == _uuid_array("row_ids", [row_id for row_id, _ in items]),
                        table.c.status == "pending",
                        table.c.fallback_from.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
        )
        kept = [(row_id, request) for row_id, request in items if row_id in eligible]
        if not kept:
            raise ValueError("no item is a pending row without fallback_from")
        if len(kept) < len(items):
            logger.warning(
                "batch %s: %d item(s) not pending, left out", stage, len(items) - len(kept)
            )
        batch = LandscapeBatch(
            stage=stage, model=model, row_table=table.name, request_count=len(kept)
        )
        db.add(batch)
        await db.flush()
        batch_id, seq = batch.id, batch.seq
        await db.execute(
            insert(LandscapeBatchItem),
            [
                {"id": uuid.uuid4(), "batch_id": batch_id, "item_seq": n, "row_id": row_id}
                for n, (row_id, _) in enumerate(kept, start=1)
            ],
        )
        items_t = LandscapeBatchItem.__table__
        await db.execute(
            update(table)
            .where(table.c.id == items_t.c.row_id, items_t.c.batch_id == batch_id)
            .values(status="submitting", batch_item_id=items_t.c.id, **_touch(table))
        )
        await db.commit()
    if _sync_mode():
        _RECONCILER.local_requests[batch_id] = {
            n: request for n, (_, request) in enumerate(kept, start=1)
        }
        await _store_created(db_factory, batch_id, f"{LOCAL_PREFIX}{batch_id}")
        return batch_id
    requests = [
        {"custom_id": _custom_id(seq, n), "params": request}
        for n, (_, request) in enumerate(kept, start=1)
    ]
    _RECONCILER.requests[batch_id] = requests
    await _create(db_factory, batch_id, requests)
    return batch_id


async def _create(db_factory, batch_row_id: uuid.UUID, requests: list[dict]) -> bool:
    """``batches.create`` for an intent row; True when the API id was stored."""
    client = _batches_client()
    try:
        created = await asyncio.to_thread(client.create, requests=requests)
    except Exception as exc:  # any failure may still have made a batch
        logger.warning("batches.create failed for batch row %s: %s", batch_row_id, exc)
        async with db_factory() as db:
            await db.execute(
                update(LandscapeBatch)
                .where(LandscapeBatch.id == batch_row_id)
                .values(
                    create_failures=LandscapeBatch.create_failures + 1, updated_at=func.now()
                )
            )
            await db.commit()
        _RECONCILER.rearm(batch_row_id)
        return False
    # Outside the try: a failure from here on is not a failed create. The batch exists, and
    # reconciliation adopts it after a restart.
    await _store_created(db_factory, batch_row_id, created.id)
    _RECONCILER.requests.pop(batch_row_id, None)
    return True


async def _store_created(db_factory, batch_row_id: uuid.UUID, api_batch_id: str) -> None:
    async with db_factory() as db:
        batch = await db.get(LandscapeBatch, batch_row_id, with_for_update=True)
        await _adopt(db, batch, api_batch_id)
        await db.commit()


async def _adopt(db: AsyncSession, batch: LandscapeBatch, api_batch_id: str) -> None:
    """Store the API id and move the batch's ``submitting`` stage rows to ``submitted``."""
    batch.api_batch_id = api_batch_id
    batch.status = "submitted"
    batch.submitted_at = func.now()
    batch.updated_at = func.now()
    await _move_stage_rows(db, batch, "submitted")


async def _move_stage_rows(
    db: AsyncSession, batch: LandscapeBatch, status: str, *, error: str | None = None,
    from_status: str = "submitting",
) -> None:
    table = Base.metadata.tables[batch.row_table]
    items_t = LandscapeBatchItem.__table__
    values: dict[str, Any] = {"status": status, **_touch(table)}
    if error is not None:
        values["error"] = error
    await db.execute(
        update(table)
        .where(
            table.c.batch_item_id == items_t.c.id,
            items_t.c.batch_id == batch.id,
            table.c.status == from_status,
        )
        .values(**values)
    )


async def _close(db: AsyncSession, batch: LandscapeBatch, stage_status: str) -> None:
    """Settle an unconfirmed row with no API batch: its stage rows take ``stage_status`` and
    the row is marked ``ingested``, so nothing reads it again."""
    error = None
    if stage_status.startswith("failed:"):
        error = job_error_text(f"Message Batch create failed {batch.create_failures} times")
    await _move_stage_rows(db, batch, stage_status, error=error)
    batch.status = "ingested"
    batch.ingested_at = func.now()
    batch.updated_at = func.now()


# --- reconciliation -------------------------------------------------------------------------


async def reconcile_orphans(db_factory, *, may_submit: bool) -> int:
    """Settle batch rows left ``submitting`` without an API id; never blocks (spec §7.1 step 4).

    The first call arms the startup scan over every such row; a failed ``create`` arms a scan
    of its own. A scan lists, once, the API batches created since its oldest row (less
    ``_LIST_SKEW``) whose ids are not stored; that list is then fixed. While any listed batch
    still runs, the scan waits and its rows stay held. Once all have ended, a row whose
    ``b{seq}-`` prefix appears is adopted; a row with no match fails (``failed:create``) after
    ``_MAX_CREATE_FAILURES`` creates, and is otherwise resubmitted, only when ``may_submit``
    (never while disabled, paused or over the daily cap, §9.5): re-created from its kept
    requests, or, after a restart has lost them, released to ``pending`` for its stage to
    batch again. A scan that raises is retried next call. Returns the number adopted.

    In sync mode no API batch can exist, so nothing is listed: every unconfirmed row is
    released to ``pending`` at once, and 0 is returned.
    """
    state = _RECONCILER
    if _sync_mode():
        await _release_unconfirmed(db_factory)
        state.scans = []
        return 0
    if not state.started:
        await _arm_startup_scan(db_factory, state)
    adopted = 0
    for scan in list(state.scans):
        try:
            adopted += await _advance(db_factory, scan, may_submit=may_submit)
        except Exception:  # the scan stays armed and runs next loop
            logger.exception("batch reconciliation scan failed; retrying next loop")
    state.scans = [scan for scan in state.scans if scan.rows]
    return adopted


async def _release_unconfirmed(db_factory) -> None:
    """Sync mode: settle every ``submitting`` row with no API id by releasing its stage rows
    to ``pending``."""
    async with db_factory() as db:
        rows = (
            await db.scalars(
                select(LandscapeBatch)
                .where(LandscapeBatch.status == "submitting", LandscapeBatch.api_batch_id.is_(None))
                .with_for_update()
            )
        ).all()
        for batch in rows:
            await _close(db, batch, "pending")
            _RECONCILER.requests.pop(batch.id, None)
            _RECONCILER.local_requests.pop(batch.id, None)
            logger.info("batch row %s released to pending (sync mode)", batch.id)
        await db.commit()


async def _arm_startup_scan(db_factory, state: _Reconciler) -> None:
    async with db_factory() as db:
        rows = set(
            (
                await db.scalars(
                    select(LandscapeBatch.id).where(
                        LandscapeBatch.status == "submitting",
                        LandscapeBatch.api_batch_id.is_(None),
                    )
                )
            ).all()
        )
    # A row whose create already failed in this process has a later scan of its own.
    for scan in state.scans:
        rows -= scan.rows
    if rows:
        state.scans.insert(0, _Scan(rows=rows))
    state.started = True


async def _advance(db_factory, scan: _Scan, *, may_submit: bool) -> int:
    client = _batches_client()
    if scan.listed is None:
        scan.listed = await _list_candidates(db_factory, client, scan.rows)
    if scan.matches is None:
        if not await _all_ended(client, scan):
            return 0
        scan.matches = await _match_prefixes(client, scan.listed)
    adopted = 0
    for row_id in sorted(scan.rows):
        outcome = await _resolve_row(db_factory, row_id, scan.matches, may_submit=may_submit)
        if outcome == "adopted":
            adopted += 1
        if outcome != "held":
            scan.rows.discard(row_id)
    return adopted


async def _list_candidates(db_factory, client: Any, rows: set[uuid.UUID]) -> list[str]:
    async with db_factory() as db:
        since = await db.scalar(
            select(func.min(LandscapeBatch.created_at)).where(
                LandscapeBatch.id == _uuid_array("batch_ids", list(rows))
            )
        )
    if since is None:
        return []
    listed = await asyncio.to_thread(_list_since, client, since - _LIST_SKEW)
    if not listed:
        return []
    async with db_factory() as db:
        stored = set(
            (
                await db.scalars(
                    select(LandscapeBatch.api_batch_id).where(
                        LandscapeBatch.api_batch_id
                        == any_(bindparam("api_ids", listed, type_=ARRAY(String)))
                    )
                )
            ).all()
        )
    return [api_id for api_id in listed if api_id not in stored]


def _list_since(client: Any, since: datetime) -> list[str]:
    """API batch ids created at or after ``since``. The list endpoint returns the newest
    first and the SDK page auto-paginates, so iteration stops at the first older batch."""
    ids = []
    for remote in client.list(limit=_LIST_PAGE_SIZE):
        if remote.created_at < since:
            break
        ids.append(remote.id)
    return ids


async def _all_ended(client: Any, scan: _Scan) -> bool:
    for api_id in scan.listed or []:
        if api_id in scan.ended:
            continue
        remote = await asyncio.to_thread(client.retrieve, api_id)
        if remote.processing_status != "ended":
            return False
        scan.ended.add(api_id)
    return True


async def _match_prefixes(client: Any, listed: list[str]) -> dict[int, str]:
    matches: dict[int, str] = {}
    for api_id in listed:
        seq = await asyncio.to_thread(_first_seq, client, api_id)
        if seq is not None:
            matches.setdefault(seq, api_id)
    return matches


def _first_seq(client: Any, api_batch_id: str) -> int | None:
    """The ``seq`` of a batch's first result. Every request of one of our batches carries
    the same ``b{seq}-`` prefix, so one line identifies it; another prefix gives None."""
    try:
        results = client.results(api_batch_id)
    except anthropic.APIError:
        raise
    except anthropic.AnthropicError:
        # The SDK raises this bare class when the batch has no results URL (archived).
        return None
    try:
        for line in results:
            parsed = _parse_custom_id(line.custom_id)
            return parsed[0] if parsed else None
        return None
    finally:
        close = getattr(results, "close", None)
        if close is not None:
            close()


async def _resolve_row(
    db_factory, row_id: uuid.UUID, matches: dict[int, str], *, may_submit: bool
) -> str:
    """Settle one unconfirmed row after its scan: adopted, failed, released, resubmitted,
    held (not ``may_submit``), or gone (settled elsewhere)."""
    async with db_factory() as db:
        batch = await db.get(LandscapeBatch, row_id, with_for_update=True)
        if batch is None or batch.status != "submitting" or batch.api_batch_id is not None:
            return "gone"
        api_id = matches.get(batch.seq)
        if api_id is not None:
            await _adopt(db, batch, api_id)
            await db.commit()
            _RECONCILER.requests.pop(row_id, None)
            return "adopted"
        if batch.create_failures >= _MAX_CREATE_FAILURES:
            await _close(db, batch, "failed:create")
            await db.commit()
            _RECONCILER.requests.pop(row_id, None)
            logger.error("batch row %s: items failed:create", row_id)
            return "failed"
        if not may_submit:
            return "held"
        requests = _RECONCILER.requests.get(row_id)
        if requests is None:
            await _close(db, batch, "pending")
            await db.commit()
            return "released"
    await _create(db_factory, row_id, requests)
    return "resubmitted"


# --- polling and ingestion ------------------------------------------------------------------


@dataclass
class BatchOutcome:
    """One result of an ended batch. ``model`` is the batch's requested model; ``message`` is
    None for errored, expired and canceled results."""

    batch_row_id: uuid.UUID
    batch_item_id: uuid.UUID
    item_seq: int
    row_id: uuid.UUID
    stage: str
    kind: str
    model: str
    message: Any | None
    usage: dict


def _message_kind(message: Any) -> str:
    """``refusal`` or ``truncated`` for a reply that stopped on ``refusal`` or ``max_tokens``,
    else ``succeeded``."""
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        return "refusal"
    if stop == "max_tokens":
        return "truncated"
    return "succeeded"


def _outcome_kind(result: Any) -> tuple[str, Any | None]:
    """``(kind, message)`` for one result. A succeeded result that stopped on ``refusal`` or
    ``max_tokens`` is ``refusal`` or ``truncated``; an unknown result type counts as
    ``errored``."""
    kind = getattr(result, "type", None)
    if kind == "succeeded":
        return _message_kind(result.message), result.message
    if kind in ("errored", "expired", "canceled"):
        return kind, None
    logger.warning("unknown batch result type %r counted as errored", kind)
    return "errored", None


async def poll(
    db_factory, *, chunk_size: int = 1000,
    may_run: Callable[[], Awaitable[bool]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[tuple[uuid.UUID, list[BatchOutcome], bool]]:
    """Yield ``(batch_row_id, outcomes, is_last_chunk)`` for each ended, not-yet-ingested
    batch, in chunks of at most ``chunk_size`` results.

    A ``submitted`` batch is retrieved first and skipped while the API still runs it. Each
    chunk's ledger rows (``batch=True``, insert-ignore) and the items' ``result_kind`` commit
    before the chunk is yielded. Results arrive in any order and are keyed by ``custom_id``.
    The caller applies each chunk (``apply_result``) and calls ``mark_ingested`` after the
    last one (§12.2); an unmarked batch is read again on the next poll, which the
    compare-and-set makes harmless. A batch with no results yields one empty last chunk.

    A local batch (sync mode) is run by ``_run_local``. Its calls are made only while
    ``await may_run()`` is true, re-read before every chunk (None never calls; the worker
    passes its enabled, unpaused, under-cap, no-live-simulation check), and no new call starts
    once ``should_stop()`` is true. Answers already had are replayed either way.

    Local batches run side by side, one lane per stage (D75): each lane takes its stage's
    batches in ``seq`` order, so a long screening batch does not hold extraction or triage
    back. Every other batch is read in one lane, in ``seq`` order, one batch at a time. Chunks
    still reach the caller one at a time, and a lane goes on past its chunk only once the
    caller asks for the next one, so each lane sees its chunk ingested before it decides
    whether to go on (``_run_local``).
    """
    client = _batches_client()
    async with db_factory() as db:
        ready = (
            await db.scalars(
                select(LandscapeBatch)
                .where(
                    LandscapeBatch.status.in_(("submitted", "ended")),
                    LandscapeBatch.api_batch_id.is_not(None),
                )
                .order_by(LandscapeBatch.seq)
            )
        ).all()
    lanes: dict[str, list[LandscapeBatch]] = {}
    for batch in ready:
        lanes.setdefault(f"local:{batch.stage}" if _is_local(batch) else "api", []).append(batch)

    async def lane(batches: list[LandscapeBatch]):
        for batch in batches:
            if _is_local(batch):
                async for chunk in _run_local(
                    db_factory, batch, may_run=may_run, should_stop=should_stop
                ):
                    yield chunk
                continue
            if batch.status == "submitted" and not await _mark_ended(db_factory, client, batch):
                continue
            async for chunk in _ingest(db_factory, client, batch, chunk_size):
                yield chunk

    if len(lanes) == 1:
        [only] = lanes.values()
        async for chunk in lane(only):
            yield chunk
        return
    async for chunk in _side_by_side([lane(batches) for batches in lanes.values()]):
        yield chunk


async def _side_by_side(streams: list[AsyncIterator[Any]]) -> AsyncIterator[Any]:
    """Run ``streams`` concurrently and yield their items one at a time, in arrival order. A
    stream moves past an item only once the consumer asks for the next one, so the consumer
    handles each item before its stream goes on. A stream that raises stops alone; the first
    error is raised once every other stream has ended. Streams still running when the
    consumer stops early are cancelled."""
    queue: asyncio.Queue = asyncio.Queue()

    async def pump(stream: AsyncIterator[Any]) -> None:
        try:
            async for item in stream:
                handled = asyncio.Event()
                await queue.put(("item", item, handled))
                await handled.wait()
        except Exception as exc:  # noqa: BLE001 - handed to the consumer, which raises it
            await queue.put(("error", exc, None))
        else:
            await queue.put(("done", None, None))

    tasks = [asyncio.create_task(pump(stream)) for stream in streams]
    running, error = len(tasks), None
    try:
        while running:
            kind, item, handled = await queue.get()
            if kind == "item":
                try:
                    yield item
                finally:
                    handled.set()
                continue
            running -= 1
            if kind == "error" and error is None:
                error = item
            elif kind == "error":
                logger.error("a second poll lane failed too: %r", item)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if error is not None:
        raise error


#: Failures that say "not now" rather than "this request is bad": the item stays unanswered for
#: the next poll instead of taking an ``errored`` outcome (the Batches API absorbs these).
_TRANSIENT = (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)


async def _run_local(
    db_factory, batch: LandscapeBatch, *, may_run: Callable[[], Awaitable[bool]] | None,
    should_stop: Callable[[], bool] | None,
) -> AsyncIterator[tuple[uuid.UUID, list[BatchOutcome], bool]]:
    """Yield a local batch's outcomes in chunks of ``_LOCAL_CHUNK`` items, calling the API for
    each item not answered yet in this process (module docstring, sync mode).

    Before a chunk with unanswered items, ``may_run`` is awaited; false ends the batch's turn.
    A chunk yields only its answered items, and is the last only when every item of the batch
    is answered; the batch is marked ``ended`` just before, so ``mark_ingested`` takes it. The
    turn also ends when a chunk was cut short (a stop, or a transient API failure), or when
    none of a yielded chunk's rows left ``submitted`` (the caller did not ingest it), so no
    answer is paid for that nobody reads. With its requests lost to a restart, a batch's rows
    still ``submitted`` are released to ``pending`` and it is marked ``ingested``.
    """
    requests = _RECONCILER.local_requests.get(batch.id)
    if requests is None:
        await _release_local(db_factory, batch.id)
        return
    async with db_factory() as db:
        items = (
            await db.execute(
                select(LandscapeBatchItem.item_seq, LandscapeBatchItem.id, LandscapeBatchItem.row_id)
                .where(LandscapeBatchItem.batch_id == batch.id)
                .order_by(LandscapeBatchItem.item_seq)
            )
        ).all()
    if not items:
        await _mark_local_ended(db_factory, batch.id)
        yield batch.id, [], True
        return
    answers = _RECONCILER.local_answers.setdefault(batch.id, {})
    for start in range(0, len(items), _LOCAL_CHUNK):
        chunk = items[start:start + _LOCAL_CHUNK]
        todo = [item for item in chunk if item.item_seq not in answers]
        if todo:
            if may_run is None or not await may_run():
                return
            await _call_local(db_factory, batch, todo, requests, answers, should_stop)
        answered = [item for item in chunk if item.item_seq in answers]
        complete = len(answered) == len(chunk)
        is_last = complete and start + _LOCAL_CHUNK >= len(items)
        # A chunk answered on an earlier turn whose every row has left `submitted` has nothing
        # left to ingest: it is not replayed (D75). A row an ingestion failure left
        # `submitted` keeps its chunk in the replay; the last chunk is always yielded.
        if not todo and not is_last and not await _still_submitted(db_factory, batch, chunk):
            continue
        if is_last:
            await _mark_local_ended(db_factory, batch.id)
        if answered:
            yield batch.id, [
                BatchOutcome(
                    batch_row_id=batch.id, batch_item_id=item.id, item_seq=item.item_seq,
                    row_id=item.row_id, stage=batch.stage, kind=answers[item.item_seq][0],
                    model=batch.model, message=answers[item.item_seq][1],
                    usage=usage_from(answers[item.item_seq][1]),
                )
                for item in answered
            ], is_last
            if not is_last and not await _any_ingested(db_factory, batch, answered):
                return
        if not complete:
            return


async def _still_submitted(db_factory, batch: LandscapeBatch, items: list) -> int:
    """How many of ``items``' stage rows are still ``submitted`` (not ingested yet)."""
    table = Base.metadata.tables[batch.row_table]
    async with db_factory() as db:
        return await db.scalar(
            select(func.count()).select_from(table).where(
                table.c.batch_item_id == _uuid_array("item_ids", [item.id for item in items]),
                table.c.status == "submitted",
            )
        ) or 0


async def _any_ingested(db_factory, batch: LandscapeBatch, items: list) -> bool:
    """Whether any of ``items``' stage rows has left ``submitted`` (been ingested)."""
    return await _still_submitted(db_factory, batch, items) < len(items)


async def _call_local(
    db_factory, batch: LandscapeBatch, todo: list, requests: dict[int, dict],
    answers: dict[int, tuple[str, Any | None]], should_stop: Callable[[], bool] | None,
) -> None:
    """One synchronous call per item of ``todo``, ``landscape_sync_concurrency`` at a time, on
    the batch's model only. Each answer goes into ``answers`` as soon as it arrives, then its
    ledger row (synchronous rate, insert-ignore on ``(batch_id, item_seq)``) and its item's
    ``result_kind`` commit, one call at a time. A call that raises is ``errored``, except a
    transient failure (``_TRANSIENT``): that item stays unanswered, writes nothing, and no
    further call starts. No call starts once ``should_stop()`` is true."""
    client = llm.get_anthropic_client()
    gate = asyncio.Semaphore(max(1, get_settings().landscape_sync_concurrency))
    halted = False

    async def one(item: Any) -> None:
        nonlocal halted
        async with gate:
            if halted or (should_stop is not None and should_stop()):
                return
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                message = await llm.abeta_create(client, **requests[item.item_seq])
                kind = _message_kind(message)
            except _TRANSIENT as exc:
                halted = True
                logger.warning(
                    "batch %s item %d: %s; the rest wait for the next poll",
                    batch.seq, item.item_seq, exc,
                )
                return
            except Exception as exc:  # noqa: BLE001 - any other failure is an errored outcome
                logger.warning("batch %s item %d: call failed: %s", batch.seq, item.item_seq, exc)
                message, kind = None, "errored"
            latency_ms = int((loop.time() - started) * 1000)
            answers[item.item_seq] = (kind, message)
            # Inside the gate, so at most `landscape_sync_concurrency` sessions are open.
            async with db_factory() as db:
                await record_call(
                    db, stage=batch.stage, model=batch.model, batch_id=batch.id,
                    item_seq=item.item_seq, custom_id=_custom_id(batch.seq, item.item_seq),
                    usage=usage_from(message) if message is not None else {},
                    refusal_category=(
                        llm.refusal_category(message) if message is not None else None
                    ),
                    served_by=getattr(message, "model", None) if message is not None else None,
                    batch=False, latency_ms=latency_ms,
                    item_ref=f"{batch.row_table}:{item.row_id}",
                )
                await db.execute(
                    update(LandscapeBatchItem)
                    .where(LandscapeBatchItem.id == item.id)
                    .values(result_kind=kind)
                )
                await db.commit()

    results = await asyncio.gather(*(one(item) for item in todo), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def _mark_local_ended(db_factory, batch_row_id: uuid.UUID) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeBatch)
            .where(LandscapeBatch.id == batch_row_id, LandscapeBatch.status == "submitted")
            .values(status="ended", ended_at=func.now(), updated_at=func.now())
        )
        await db.commit()


async def _release_local(db_factory, batch_row_id: uuid.UUID) -> None:
    """A local batch whose requests a restart lost: its rows still ``submitted`` go back to
    ``pending`` (their stage batches them again) and the batch is marked ``ingested``."""
    async with db_factory() as db:
        batch = await db.get(LandscapeBatch, batch_row_id, with_for_update=True)
        if batch is None or batch.status == "ingested":
            return
        await _move_stage_rows(db, batch, "pending", from_status="submitted")
        batch.status = "ingested"
        batch.ingested_at = func.now()
        batch.updated_at = func.now()
        await db.commit()
    _RECONCILER.local_answers.pop(batch_row_id, None)
    logger.warning("local batch %s: requests lost to a restart; rows released", batch_row_id)


async def _mark_ended(db_factory, client: Any, batch: LandscapeBatch) -> bool:
    try:
        remote = await asyncio.to_thread(client.retrieve, batch.api_batch_id)
    except Exception as exc:  # one unreadable batch must not stop the rest
        logger.warning("retrieve failed for batch %s: %s", batch.api_batch_id, exc)
        return False
    if remote.processing_status != "ended":
        return False
    async with db_factory() as db:
        await db.execute(
            update(LandscapeBatch)
            .where(LandscapeBatch.id == batch.id, LandscapeBatch.status == "submitted")
            .values(
                status="ended",
                ended_at=getattr(remote, "ended_at", None) or func.now(),
                updated_at=func.now(),
            )
        )
        await db.commit()
    return True


def _take(iterator: Any, size: int) -> list:
    return list(itertools.islice(iterator, size))


async def _ingest(
    db_factory, client: Any, batch: LandscapeBatch, chunk_size: int
) -> AsyncIterator[tuple[uuid.UUID, list[BatchOutcome], bool]]:
    async with db_factory() as db:
        items = {
            item.item_seq: (item.id, item.row_id)
            for item in (
                await db.scalars(
                    select(LandscapeBatchItem).where(LandscapeBatchItem.batch_id == batch.id)
                )
            ).all()
        }
    results = await asyncio.to_thread(client.results, batch.api_batch_id)
    try:
        iterator = iter(results)
        chunk = await asyncio.to_thread(_take, iterator, chunk_size)
        while True:
            following = (
                await asyncio.to_thread(_take, iterator, chunk_size)
                if len(chunk) == chunk_size
                else []
            )
            outcomes = await _record_chunk(db_factory, batch, items, chunk)
            is_last = not following
            yield batch.id, outcomes, is_last
            if is_last:
                return
            chunk = following
    finally:
        close = getattr(results, "close", None)
        if close is not None:
            close()


async def _record_chunk(
    db_factory, batch: LandscapeBatch, items: dict[int, tuple[uuid.UUID, uuid.UUID]], chunk: list
) -> list[BatchOutcome]:
    outcomes: list[BatchOutcome] = []
    async with db_factory() as db:
        for line in chunk:
            parsed = _parse_custom_id(line.custom_id)
            if parsed is None or parsed[0] != batch.seq or parsed[1] not in items:
                logger.warning("batch %s: unexpected custom_id %r", batch.seq, line.custom_id)
                continue
            item_seq = parsed[1]
            item_id, row_id = items[item_seq]
            kind, message = _outcome_kind(line.result)
            usage = usage_from(message)
            await record_call(
                db, stage=batch.stage, model=batch.model, batch_id=batch.id,
                item_seq=item_seq, custom_id=line.custom_id, usage=usage,
                refusal_category=llm.refusal_category(message) if message is not None else None,
                served_by=getattr(message, "model", None) or batch.model, batch=True,
                latency_ms=None, item_ref=f"{batch.row_table}:{row_id}",
            )
            outcomes.append(BatchOutcome(
                batch_row_id=batch.id, batch_item_id=item_id, item_seq=item_seq, row_id=row_id,
                stage=batch.stage, kind=kind, model=batch.model, message=message, usage=usage,
            ))
        for kind in {o.kind for o in outcomes}:
            ids = [o.batch_item_id for o in outcomes if o.kind == kind]
            await db.execute(
                update(LandscapeBatchItem)
                .where(
                    LandscapeBatchItem.id == _uuid_array("item_ids", ids),
                    LandscapeBatchItem.result_kind.is_(None),
                )
                .values(result_kind=kind)
            )
        await db.commit()
    return outcomes


async def mark_ingested(db_factory, batch_row_id: uuid.UUID) -> None:
    """Mark an ended batch ``ingested``: ``poll`` never reads it again."""
    async with db_factory() as db:
        await db.execute(
            update(LandscapeBatch)
            .where(LandscapeBatch.id == batch_row_id, LandscapeBatch.status == "ended")
            .values(status="ingested", ingested_at=func.now(), updated_at=func.now())
        )
        await db.commit()
    _RECONCILER.local_requests.pop(batch_row_id, None)
    _RECONCILER.local_answers.pop(batch_row_id, None)


async def apply_result(
    db: AsyncSession, row_model: type, *, row_id: uuid.UUID, batch_item_id: uuid.UUID,
    values: dict,
) -> bool:
    """Compare-and-set one stage row from a batch result (spec §7.1 step 5, F4).

    ``UPDATE … SET values WHERE id = row_id AND status = 'submitted' AND batch_item_id =
    batch_item_id``, in the caller's transaction (the caller commits). True when one row
    changed. A replayed chunk, or a late result for a row since resubmitted under another
    batch item, returns False; rows derived from the result (ideas, evidence, links) are
    written in the same transaction, and only when this returns True (N12).
    """
    table = row_model.__table__
    result = await db.execute(
        update(table)
        .where(
            table.c.id == row_id,
            table.c.status == "submitted",
            table.c.batch_item_id == batch_item_id,
        )
        .values(**{**_touch(table), **values})
    )
    return result.rowcount == 1


def transition(kind: str, *, attempts: int, truncated_once: bool, model: str) -> dict:
    """Stage-row values for a non-success outcome (spec §7.1, D32, D42).

    - ``errored`` / ``expired``: back to ``pending`` with ``attempts + 1`` while ``attempts``
      is 0 (``attempts`` counts both kinds), then ``failed:errored`` / ``failed:expired``.
    - ``canceled``: back to ``pending``, every time.
    - ``truncated``: ``pending`` with ``truncated_once`` (the stage builds the next request
      with ``retry=True``), then ``failed:truncated``, which is terminal.
    - ``refusal``: ``pending`` with ``fallback_from=model``, for ``run_fallbacks``.

    ``succeeded`` is the stage's to handle: ``ValueError``.
    """
    if kind in ("errored", "expired"):
        if attempts < 1:
            return {"status": "pending", "attempts": attempts + 1}
        return {"status": f"failed:{kind}"}
    if kind == "canceled":
        return {"status": "pending"}
    if kind == "truncated":
        if not truncated_once:
            return {"status": "pending", "truncated_once": True}
        return {"status": "failed:truncated"}
    if kind == "refusal":
        return {"status": "pending", "fallback_from": model}
    raise ValueError(f"no generic transition for outcome kind {kind!r}")


# --- the fallback chain for refused batch items ---------------------------------------------


@dataclass(frozen=True)
class FallbackAdapter:
    """How one batched stage re-runs a refused row synchronously (D42).

    ``await build(db, row)`` rebuilds the stage's request for the row from the database (its
    document or stored dossier, and its epoch's own inputs, ``epochs.epoch_inputs``), in a
    read session that closes before the call; ``on_success(db, row, result)`` writes the
    stage's result rows in ``run_fallbacks``' transaction. Each batched stage module exports
    ``FALLBACKS: tuple[FallbackAdapter, ...]``.

    ``ledger_stage`` names the ledger stage of the re-runs when it differs from ``stage``: the
    reject audit's requests are built by the audited stage's builder but ledgered as ``audit``
    (D66). None ledgers them under ``stage``.
    """

    stage: str
    row_model: type
    build: Callable[[AsyncSession, Any], Awaitable[dict]]
    on_success: Callable[[AsyncSession, Any, CallResult], Awaitable[None]]
    ledger_stage: str | None = None


async def run_fallbacks(db_factory, adapter: FallbackAdapter) -> int:
    """Re-run every ``pending`` row with ``fallback_from`` set through ``call_sync``, skipping
    the model that refused, so it is never billed again (D42).

    Each row's request comes from ``adapter.build`` in a session of its own, closed before the
    synchronous call, so no transaction stays open across it. A success sets the row ``done``
    with ``served_by_model``, then calls ``adapter.on_success``, in one transaction;
    ``on_success`` may override the status. An exhausted chain gives ``refused`` (with the last
    refusal category), a second truncation ``failed:truncated``. Any other failure keeps the
    row ``pending`` once (``attempts + 1``), then gives ``failed:errored``. A row changed since
    it was read is left alone. Returns the number completed.
    """
    model = adapter.row_model
    async with db_factory() as db:
        rows = (
            await db.scalars(
                select(model).where(model.status == "pending", model.fallback_from.is_not(None))
            )
        ).all()
    done = 0
    for row in rows:
        done += await _run_one_fallback(db_factory, adapter, row)
    return done


async def _run_one_fallback(db_factory, adapter: FallbackAdapter, row: Any) -> int:
    refused_by = row.fallback_from
    async with db_factory() as db:
        request = await adapter.build(db, row)
    result = await call_sync(
        adapter.stage, request, db_factory=db_factory,
        item_ref=f"{adapter.row_model.__tablename__}:{row.id}",
        skip_models=frozenset({refused_by}), ledger_stage=adapter.ledger_stage,
    )
    async with db_factory() as db:
        current = await db.get(adapter.row_model, row.id, with_for_update=True)
        if current is None or current.status != "pending" or current.fallback_from != refused_by:
            return 0
        succeeded = (
            result.error is None
            and result.message is not None
            and result.stop_reason not in ("refusal", "max_tokens")
        )
        if succeeded:
            current.status = "done"
            current.served_by_model = result.served_by
            await adapter.on_success(db, current, result)
        else:
            _record_fallback_failure(current, result)
        await db.commit()
    return 1 if succeeded else 0


def _record_fallback_failure(row: Any, result: CallResult) -> None:
    if result.stop_reason == "refusal":
        row.status = "refused"
        row.refusal_category = result.refusal_category
    elif result.stop_reason == "max_tokens":
        row.status = "failed:truncated"
    elif row.attempts < 1:
        row.attempts = row.attempts + 1
    else:
        row.status = "failed:errored"
        row.error = job_error_text(result.error or "no reply")
