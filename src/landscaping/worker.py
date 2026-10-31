"""The landscaping worker process, ``python -m src.landscaping.worker`` (spec §12.2, L18, D23).

One process owns the pipeline's work state, all of it in ``landscape_*`` tables; it never reads
or writes the ``jobs`` table (spec §12.1).

**At start** (``main``, ``start``): it takes ``LANDSCAPE_WORKER_LOCK_KEY`` on its own connection
and exits when another instance holds it, before touching anything; refuses to run without
migration 0077; loads and validates the rubric, the BlackbirdBot extracts, the landscaping prompt
set and the criteria file; floors the NCBI request interval (D58); inserts missing source rows;
makes sure a candidate epoch stands for the current judging inputs (spec §7.11); then starts
batch reconciliation before returning stale ``running`` rows to ``pending``.

**Each loop** (``run_once``):

- Always, even while disabled or paused: batch reconciliation, then polling ended batches and
  ingesting them chunk by chunk, each batch marked ingested after its last chunk (§9.5).
- Only while ``landscape_enabled`` is true and ``app_settings.landscape_paused`` is not, in §12.2
  order: intake (and the D69 flip to ``continuous``); evidence assembly and S2-S4 submissions;
  synchronous work (S6, S7, similarity judgments, fallback chains) while no simulation run is
  live; the cut, S8, labels and S9, then the weekly batch and reports; epochs; the reject audit;
  probes; the expert sample; the bake-off and the pilot's measurements.

**Phases** (``app_settings.landscape_phase``; unset means ``pilot``):

- ``pilot`` (D68): the pilot slices and positive controls, the bake-off once they are in, then
  S2-S9 on the pilot documents only once the stage model settings equal the bake-off's picks
  (no review batch), then the S4 repeatability run and the pilot report.
- ``backfill``: S2-S4 only; S5-S9 wait. Once every enabled source's backfill slices have ended
  and no S2-S4 work is owed, the worker sets the phase to ``continuous`` and stamps the current
  candidate's ``drained_at`` (D69).
- ``continuous``: the whole funnel, review batches, probes and the expert sample.

No candidate activates outside ``continuous`` (D70). While ``ledger.over_daily_cap`` is true, no
new batch is submitted and no synchronous LLM call is made; intake, cuts, S8 grouping, the batch
issue and reports go on (§9.5). ``stages.cut.run_cut`` is called regardless, so a cut whose tie
class needs the boundary tournament still submits it.

**Transactions.** Every unit of work commits on its own (§12.2). A failing step is logged and the
loop goes on with the next one, so a failure never rolls back finished work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import get_settings
from src.database import make_engine
from src.landscaping import (
    batches,
    comparator,
    epochs,
    ideas,
    intake,
    ledger,
    liveness,
    reports,
    rescue,
    review_batches,
)
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.criteria import load_criteria
from src.landscaping.eval import bakeoff, regression, reject_audit
from src.landscaping.prompt_set import load_blackbird_extracts, load_prompt_set
from src.landscaping.schema_check import schema_ready
from src.landscaping.stages import cut as cut_stage
from src.landscaping.stages import deep as deep_stage
from src.landscaping.stages import extract as extract_stage
from src.landscaping.stages import final as final_stage
from src.landscaping.stages import opportunities as s8
from src.landscaping.stages import rerank as rerank_stage
from src.landscaping.stages import screen as screen_stage
from src.landscaping.stages import triage as triage_stage
from src.models import AppSetting
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeBatch,
    LandscapeBatchItem,
    LandscapeCandidateGrouping,
    LandscapeComparison,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeIdea,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRejectAudit,
    LandscapeReport,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeScreen,
    LandscapeSource,
    LandscapeTriage,
)
from src.services import pubmed
from src.services.advisory_locks import LANDSCAPE_WORKER_LOCK_KEY, SessionAdvisoryLock
from src.services.blackbird_rubric import load_rubric

logger = logging.getLogger(__name__)

#: Seconds between loops; ended batches are polled every ``landscape_batch_poll_seconds``.
LOOP_SECONDS = 30
#: Ideas given an evidence-assembly pass per epoch and loop, so one loop stays bounded.
ASSEMBLY_PER_LOOP = 200
#: Phases in which S5-S9 run (D69).
_DEEP_PHASES = ("pilot", "continuous")
_ALL_STAGES = (
    "screen", "extract", "triage", "cut", "deep", "rerank", "similarity", "label", "final",
)
_IN_FLIGHT = ("pending", "submitting", "submitted", "running")
_PASSED = ("done", "rescued")
_TRUE = ("true", "1", "yes", "on")
#: Stage-row tables a synchronous stage may leave ``running`` (§12.2 On restart).
_RUNNING_TABLES: tuple[type, ...] = (
    LandscapeScreen, LandscapeExtraction, LandscapeTriage, LandscapeDeep, LandscapeComparison,
    LandscapeCandidateGrouping, LandscapeOpportunityVersion, LandscapeAssessment,
    LandscapeRejectAudit,
)
#: Each batched stage's row tables, counted for the ``landscape_batch_min_items`` gate.
_STAGE_ROWS: dict[str, tuple[type, ...]] = {
    "screen": (LandscapeScreen,),
    "extract": (LandscapeExtraction,),
    "triage": (LandscapeTriage,),
    "label": (LandscapeOpportunityVersion, LandscapeCandidateGrouping),
    "final": (LandscapeAssessment,),
}
#: Each batched stage's submitter. Looked up at call time, so a stage module's function can be
#: replaced in tests.
_SUBMITTERS: dict[str, Callable[[Any, uuid.UUID], Awaitable[int]]] = {
    "screen": lambda f, e: screen_stage.submit_screen_batch(f, e),
    "extract": lambda f, e: extract_stage.submit_extract_batch(f, e),
    "triage": lambda f, e: triage_stage.submit_triage_batch(f, e),
    "label": lambda f, e: s8.submit_label_batch(f, epoch_id=e),
    "final": lambda f, e: final_stage.submit_final_batch(f, epoch_id=e),
}

#: Routes each ended batch's outcomes by the batch's ``stage``. A ``rerank`` batch's completed
#: tournament then lands the cut (basis ``triage``) or the S7 rebuild (basis ``deep``).
INGESTERS: dict[str, Callable[[Any, list[BatchOutcome]], Awaitable[None]]] = {
    "screen": screen_stage.ingest_screen_outcomes,
    "extract": extract_stage.ingest_extract_outcomes,
    "triage": triage_stage.ingest_triage_outcomes,
    "rerank": comparator.ingest_tournament_outcomes,
    "label": s8.ingest_label_outcomes,
    "final": final_stage.ingest_final_outcomes,
    "audit": reject_audit.ingest_audit_outcomes,
}

_shutdown = False


def _handle_signal(*_args: Any) -> None:
    global _shutdown
    logger.info("Received shutdown signal, finishing the current unit of work...")
    _shutdown = True


class WorkerStartRefused(RuntimeError):
    """The worker must not run: another instance holds the lock, migration 0077 is missing, or
    a judging input failed validation."""


@dataclass
class WorkerState:
    """In-process loop state; a restart starts afresh."""

    last_poll: datetime | None = None
    #: (epoch id, stage) -> when that stage last submitted, for the max-wait gate.
    last_submit: dict[tuple[uuid.UUID, str], datetime] = field(default_factory=dict)
    #: Daily task -> the UTC day it last ran.
    daily: dict[str, date] = field(default_factory=dict)
    #: (epoch id, cut id) pairs S8 has grouped.
    grouped: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)
    #: Bake-off triggers already tried, so a failing bake-off is not re-run every loop.
    tried: set[str] = field(default_factory=set)
    #: Rebuild batches this process has landed from the loop (spec §7.8). A rebuild whose last
    #: answers came through the fallback chain has no batch end to land it, so the S7 step
    #: lands it once; landing is idempotent, so one landed at a batch end may land again.
    landed: set[uuid.UUID] = field(default_factory=set)

    def poll_due(self, now: datetime, seconds: float) -> bool:
        if self.last_poll is not None and (now - self.last_poll).total_seconds() < seconds:
            return False
        self.last_poll = now
        return True

    def once_today(self, task: str, today: date) -> bool:
        if self.daily.get(task) == today:
            return False
        self.daily[task] = today
        return True


@dataclass(frozen=True)
class _Loop:
    now: datetime
    phase: str
    #: New LLM work is allowed: the day's spend is under ``landscape_daily_usd_cap``.
    llm: bool
    #: S2-S9 may run: always outside ``pilot``; in ``pilot`` once the bake-off's picks are applied.
    funnel: bool


async def _step(name: str, work: Awaitable[Any], default: Any = None) -> Any:
    """Await one step; a failure is logged and the loop goes on (§12.2)."""
    try:
        return await work
    except Exception:  # noqa: BLE001 - one failing step must not stop the others
        logger.exception("landscaping step %r failed; the loop goes on", name)
        return default


# --- app_settings -----------------------------------------------------------------------------


async def _app_setting(db: AsyncSession, key: str) -> str | None:
    value = await db.scalar(select(AppSetting.value).where(AppSetting.key == key))
    return value.strip().strip('"') if value is not None else None


async def _set_app_setting(db: AsyncSession, key: str, value: str) -> None:
    await db.execute(
        pg_insert(AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=[AppSetting.key], set_={"value": value, "updated_at": func.now()}
        )
    )


# --- start ------------------------------------------------------------------------------------


def validate_inputs() -> None:
    """Load the rubric, the BlackbirdBot extracts, the prompt set and the criteria file, each
    with its own validation; ``WorkerStartRefused`` on the first error (§12.2)."""
    try:
        load_rubric()
        load_blackbird_extracts()
        load_prompt_set()
        load_criteria()
    except Exception as exc:  # noqa: BLE001 - every loader error refuses the start
        raise WorkerStartRefused(f"a judging input failed validation: {exc}") from exc


async def reset_stale(db_factory, *, now: datetime) -> int:
    """Return to ``pending`` every landscaping stage row left ``running`` for longer than
    ``landscape_running_stale_minutes`` (§12.2 On restart); commits and returns the count."""
    cutoff = now - timedelta(minutes=get_settings().landscape_running_stale_minutes)
    reset = 0
    async with db_factory() as db:
        for model in _RUNNING_TABLES:
            result = await db.execute(
                update(model)
                .where(model.status == "running", model.updated_at < cutoff)
                .values(status="pending", updated_at=func.now())
            )
            reset += result.rowcount or 0
        await db.commit()
    if reset:
        logger.info("returned %d stale running row(s) to pending", reset)
    return reset


async def start(db_factory, *, now: datetime | None = None) -> None:
    """The start sequence after the worker lock (§12.2); ``WorkerStartRefused`` when the schema
    or a judging input is missing or invalid, before anything is written."""
    settings = get_settings()
    async with db_factory() as db:
        if not await schema_ready(db):
            raise WorkerStartRefused("migration 0077 is not applied: no landscaping tables")
    validate_inputs()
    pubmed.set_min_interval(1 / settings.landscape_ncbi_max_rps)
    async with db_factory() as db:
        await intake.ensure_sources(db)
        await epochs.ensure_candidate(db)
    # Reconciliation starts first and never blocks (D31); nothing is resubmitted before the
    # first loop has read the kill switch, the pause flag and the cap.
    await batches.reconcile_orphans(db_factory, may_submit=False)
    await reset_stale(db_factory, now=now or datetime.now(UTC))


async def main() -> None:
    """Run the worker until SIGTERM or SIGINT. ``WorkerStartRefused`` when another instance
    holds the worker lock (nothing is touched), or when ``start`` refuses."""
    settings = get_settings()
    lock = SessionAdvisoryLock(settings.database_url, LANDSCAPE_WORKER_LOCK_KEY)
    if not await lock.acquire():
        await lock.release()
        raise WorkerStartRefused("another landscaping worker holds the worker lock")
    engine = make_engine("landscape", settings.database_url)
    db_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await start(db_factory)
        logger.info("landscaping worker started")
        state = WorkerState()
        while not _shutdown:
            await lock.check()
            try:
                await run_once(db_factory, state, now=datetime.now(UTC))
            except Exception:  # noqa: BLE001 - the loop survives anything one pass raises
                logger.exception("landscaping loop failed; retrying next loop")
            await _pause(LOOP_SECONDS)
    finally:
        logger.info("landscaping worker shutting down")
        await engine.dispose()
        await lock.release()


async def _pause(seconds: int) -> None:
    for _ in range(seconds):
        if _shutdown:
            return
        await asyncio.sleep(1)


# --- one loop ---------------------------------------------------------------------------------


async def run_once(db_factory, state: WorkerState, *, now: datetime) -> None:
    """One pass of the loop (module docstring); ``now`` is timezone-aware."""
    settings = get_settings()
    async with db_factory() as db:
        paused = (await _app_setting(db, "landscape_paused") or "").lower() in _TRUE
        phase = await _app_setting(db, "landscape_phase") or "pilot"
        over_cap = await ledger.over_daily_cap(db)
    running = settings.landscape_enabled and not paused
    await _step("reconcile", batches.reconcile_orphans(
        db_factory, may_submit=running and not over_cap,
    ))
    if state.poll_due(now, settings.landscape_batch_poll_seconds):
        await _step("poll", ingest_ended(db_factory))
    if not running:
        return
    phase = await _step("intake", _intake_step(db_factory, phase, now), default=phase)
    loop = _Loop(
        now=now, phase=phase, llm=not over_cap, funnel=await _funnel_open(db_factory, phase),
    )
    work = await _working_epochs(db_factory)
    await _step("submissions", _submission_step(db_factory, state, loop, work))
    await _step("synchronous work", _sync_step(db_factory, state, loop, work))
    await _step("cut and S8-S9", _cut_step(db_factory, state, loop, work))
    if loop.phase == "continuous":
        await _step("weekly batch", review_batches.issue_weekly_batch(
            db_factory, today=now.date(),
        ))
    await _step("reports", _reports(db_factory, state, now))
    await _step("epochs", _epoch_step(db_factory, loop))
    await _step("reject audit", _audit_step(db_factory, state, loop, work))
    if loop.phase == "continuous":
        await _step("probes", _probe_step(db_factory, now))
        await _step("expert sample", _expert_step(db_factory, state, now))
    await _step("evaluation", _evaluation_step(db_factory, state, loop))


async def _working_epochs(db_factory) -> list[uuid.UUID]:
    """The epochs that do work: the active one, then at most one candidate whose re-judge has
    begun (it passed regression or was overridden, spec §7.11 steps 1-2)."""
    async with db_factory() as db:
        active = await epochs.active_epoch(db)
        candidate = await _rejudging(db)
    return [epoch.id for epoch in (active, candidate) if epoch is not None]


async def _rejudging(db: AsyncSession) -> LandscapeEpoch | None:
    return await db.scalar(
        select(LandscapeEpoch)
        .where(LandscapeEpoch.status == "candidate", LandscapeEpoch.rejudge_started_at.is_not(None))
        .order_by(LandscapeEpoch.created_at.desc())
        .limit(1)
    )


async def _latest_report(db: AsyncSession, kind: str) -> LandscapeReport | None:
    return await db.scalar(
        select(LandscapeReport)
        .where(LandscapeReport.kind == kind)
        .order_by(LandscapeReport.created_at.desc())
        .limit(1)
    )


def picks_applied(report: LandscapeReport | None) -> bool:
    """Whether every stage model the bake-off picked equals the current setting (D68).

    The picks are the report payload's ``recommended_settings``: each stage's environment
    variable (``LANDSCAPE_SCREEN_MODEL``) mapped to the model picked, as
    ``eval.bakeoff.run_bakeoff`` stores them. A report without picks counts as not applied,
    so the pilot funnel waits rather than run on unchecked models."""
    payload = (report.payload or {}) if report is not None else {}
    picks = payload.get("recommended_settings")
    if not isinstance(picks, dict) or not picks:
        return False
    settings = get_settings()
    return all(getattr(settings, str(name).lower(), None) == model for name, model in picks.items())


async def _funnel_open(db_factory, phase: str) -> bool:
    if phase != "pilot":
        return True
    async with db_factory() as db:
        return picks_applied(await _latest_report(db, "bakeoff"))


# --- polling and ingestion --------------------------------------------------------------------


async def ingest_ended(db_factory) -> int:
    """Poll ended batches and route each chunk's outcomes through ``INGESTERS``; a batch is
    marked ingested after its last chunk, then a completed rerank tournament is landed. A batch
    whose chunk fails is left unmarked and read again on the next poll (the compare-and-set
    makes the replay harmless). Returns the number of batches marked ingested."""
    ingested = 0
    broken: set[uuid.UUID] = set()
    async for batch_id, outcomes, is_last in batches.poll(db_factory):
        if batch_id in broken:
            continue
        try:
            if outcomes:
                await _route(db_factory, outcomes)
        except Exception:  # noqa: BLE001 - the batch is retried on the next poll
            logger.exception("ingesting batch %s failed; it is read again next poll", batch_id)
            broken.add(batch_id)
            continue
        if is_last:
            await batches.mark_ingested(db_factory, batch_id)
            ingested += 1
            await _step(f"tournament {batch_id}", _after_tournament(db_factory, batch_id))
    return ingested


async def _route(db_factory, outcomes: list[BatchOutcome]) -> None:
    stage = outcomes[0].stage
    ingest = INGESTERS.get(stage)
    if ingest is None:
        raise LookupError(f"no ingester for batch stage {stage!r}")
    await ingest(db_factory, outcomes)


async def _tournament_keys(db: AsyncSession, batch_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """``(epoch id, basis)`` of a rerank batch whose tournament has every answer it will get;
    [] for any other batch or one still owed answers."""
    stage = await db.scalar(select(LandscapeBatch.stage).where(LandscapeBatch.id == batch_id))
    if stage != comparator.STAGE or not await comparator.tournament_complete(db, batch_id):
        return []
    C, BI = LandscapeComparison, LandscapeBatchItem
    rows = await db.execute(
        select(C.epoch_id, C.basis)
        .join(BI, BI.row_id == C.id)
        .where(BI.batch_id == batch_id)
        .distinct()
    )
    return [(epoch_id, basis) for epoch_id, basis in rows.all()]


async def _after_tournament(db_factory, batch_id: uuid.UUID) -> None:
    async with db_factory() as db:
        keys = await _tournament_keys(db, batch_id)
    for epoch_id, basis in keys:
        if basis == "triage":
            await cut_stage.finish_cut(db_factory, epoch_id=epoch_id, tournament_batch_id=batch_id)
        elif basis == rerank_stage.BASIS:
            await rerank_stage.finish_rebuild(db_factory, epoch_id=epoch_id, batch_id=batch_id)


# --- step 1: intake and the D69 flip ----------------------------------------------------------


async def _intake_step(db_factory, phase: str, now: datetime) -> str:
    """Due fetches, links, lab unifications and alias resolution; in ``backfill``, the D69
    flip once the backfill has drained. Returns the phase in force afterwards."""
    async with db_factory() as db:
        due = await intake.schedule_due(db, now=now)
    for source, slice_key in due:
        if _shutdown:
            break
        await _step(f"fetch {source} {slice_key}", _fetch(db_factory, source, slice_key))
    await _step("grant links", _in_session(db_factory, intake.link_grants))
    await _step("dataset links", _in_session(db_factory, intake.link_datasets))
    await _step("lab unifications", _in_session(db_factory, ideas.apply_lab_unifications))
    async with db_factory() as db:
        live = (
            await db.scalars(select(LandscapeEpoch.id).where(LandscapeEpoch.status != "retired"))
        ).all()
    for epoch_id in live:
        await _step(f"aliases {epoch_id}", _resolve_aliases(db_factory, epoch_id))
    if phase == "backfill" and await _backfill_drained(db_factory, now):
        await _flip_to_continuous(db_factory)
        return "continuous"
    return phase


async def _fetch(db_factory, source: str, slice_key: str) -> None:
    await intake.run_slice(db_factory, source, slice_key, intake.slice_fetcher(source, slice_key))


async def _in_session(db_factory, work: Callable[[AsyncSession], Awaitable[Any]]) -> None:
    async with db_factory() as db:
        await work(db)
        await db.commit()


async def _resolve_aliases(db_factory, epoch_id: uuid.UUID) -> None:
    async with db_factory() as db:
        await ideas.resolve_aliases(db, epoch_id=epoch_id)
        await db.commit()


async def _backfill_drained(db_factory, now: datetime) -> bool:
    """D69: every enabled source's backfill slices have a terminal fetch (``complete``, or
    ``incomplete`` after its retries), and the epoch doing the work owes no S2-S4 work."""
    async with db_factory() as db:
        if not await _backfill_fetched(db, now):
            return False
        epoch = await _unactivated(db) or await epochs.active_epoch(db)
        if epoch is None:
            return False
        return not await _s2_s4_pending(db, epoch.id)


async def _unactivated(db: AsyncSession) -> LandscapeEpoch | None:
    return await db.scalar(
        select(LandscapeEpoch)
        .where(LandscapeEpoch.status.in_(("candidate", "held")))
        .order_by(LandscapeEpoch.created_at.desc())
        .limit(1)
    )


async def _backfill_fetched(db: AsyncSession, now: datetime) -> bool:
    enabled = set(
        (await db.scalars(select(LandscapeSource.source).where(LandscapeSource.enabled.is_(True))))
        .all()
    )
    window_days = get_settings().landscape_window_days
    wanted = {
        (source, key)
        for source, key in intake.backfill_slices(now.date(), window_days)
        if source in enabled
    }
    if not wanted:
        return True
    F = LandscapeFetch
    rows = await db.execute(
        select(F.source, F.slice_key).where(
            F.status.in_(("complete", "incomplete")),
            F.source.in_({source for source, _ in wanted}),
            F.slice_key.in_({key for _, key in wanted}),
        )
    )
    ended = {(source, key) for source, key in rows.all()}
    return wanted <= ended


async def _s2_s4_pending(db: AsyncSession, epoch_id: uuid.UUID) -> bool:
    """Whether S2, S3, S4 or evidence-assembly work is owed in the epoch: a row in flight, or
    an item its stage has not enqueued yet (an eligible document with no S2 row, a screened-in
    (or rescued, L44) document with no S3 row, an idea version with no S4 row)."""
    D, S, E = LandscapeDocument, LandscapeScreen, LandscapeExtraction
    T, Idea = LandscapeTriage, LandscapeIdea
    screened = select(S.id).where(S.document_id == D.id, S.epoch_id == epoch_id).exists()
    extracted = select(E.id).where(
        E.document_id == S.document_id, E.epoch_id == epoch_id, E.basis == "summary"
    ).exists()
    triaged = select(T.id).where(
        T.idea_id == Idea.id, T.epoch_id == epoch_id, T.idea_version == Idea.version_hash
    ).exists()
    checks = (
        select(S.id).where(S.epoch_id == epoch_id, S.status.in_(_IN_FLIGHT)).exists(),
        select(E.id).where(
            E.epoch_id == epoch_id, E.basis == "summary", E.status.in_(_IN_FLIGHT)
        ).exists(),
        select(T.id).join(Idea, Idea.id == T.idea_id).where(
            T.epoch_id == epoch_id, T.status.in_(_IN_FLIGHT), T.idea_version == Idea.version_hash,
        ).exists(),
        select(D.id).where(
            D.s1_status == "eligible", D.merged_into.is_(None), ~screened
        ).exists(),
        select(S.id).join(D, D.id == S.document_id).where(
            S.epoch_id == epoch_id,
            or_(and_(S.status == "done", S.decision.in_(("yes", "unsure"))),
                S.status == "rescued"),
            D.merged_into.is_(None), ~extracted,
        ).exists(),
        select(Idea.id).where(
            Idea.epoch_id == epoch_id, Idea.evidence_assembled_at.is_(None)
        ).exists(),
        select(Idea.id).where(
            Idea.epoch_id == epoch_id, Idea.version_hash.is_not(None), ~triaged
        ).exists(),
    )
    for check in checks:
        if await db.scalar(select(check)):
            return True
    return False


async def _flip_to_continuous(db_factory) -> None:
    """D69: phase ``continuous``, and the current candidate's ``drained_at`` set to now,
    overwriting any earlier value (spec §7.11 step 4), in one transaction."""
    async with db_factory() as db:
        await _set_app_setting(db, "landscape_phase", "continuous")
        await db.execute(
            update(LandscapeEpoch)
            .where(LandscapeEpoch.status.in_(("candidate", "held")))
            .values(drained_at=func.now(), updated_at=func.now())
        )
        await db.commit()
    logger.info("backfill drained: phase is now continuous (D69)")


# --- step 2: evidence and batch submissions ---------------------------------------------------


async def _submission_step(db_factory, state: WorkerState, loop: _Loop, work: list) -> None:
    if not loop.funnel:
        return
    for epoch_id in work:
        await _step(f"evidence {epoch_id}", _assemble(db_factory, epoch_id))
        for stage in ("screen", "extract", "triage"):
            await _step(f"submit {stage}", _maybe_submit(db_factory, state, loop, stage, epoch_id))


async def _assemble(db_factory, epoch_id: uuid.UUID) -> None:
    async with db_factory() as db:
        idea_ids = (
            await db.scalars(
                select(LandscapeIdea.id)
                .where(
                    LandscapeIdea.epoch_id == epoch_id,
                    LandscapeIdea.evidence_assembled_at.is_(None),
                )
                .order_by(LandscapeIdea.created_at, LandscapeIdea.id)
                .limit(ASSEMBLY_PER_LOOP)
            )
        ).all()
    for idea_id in idea_ids:
        if _shutdown:
            return
        await _step(
            f"assemble {idea_id}",
            ideas.assemble_evidence(db_factory, epoch_id=epoch_id, idea_id=idea_id),
        )


async def _submit_due(
    db_factory, state: WorkerState, stage: str, epoch_id: uuid.UUID, now: datetime
) -> bool:
    """A stage submits when it holds ``landscape_batch_min_items`` pending rows, or when
    ``landscape_batch_max_wait_minutes`` have passed since it last submitted (§12.2 step 2).
    The submitters create most stage rows themselves, so not-yet-created rows are not counted:
    the wait clause is what bounds how long such an item waits."""
    settings = get_settings()
    last = state.last_submit.get((epoch_id, stage))
    if last is None or now - last >= timedelta(minutes=settings.landscape_batch_max_wait_minutes):
        return True
    pending = 0
    async with db_factory() as db:
        for model in _STAGE_ROWS[stage]:
            pending += await db.scalar(
                select(func.count()).select_from(model).where(
                    model.epoch_id == epoch_id, model.status == "pending",
                    model.fallback_from.is_(None),
                )
            ) or 0
    return pending >= settings.landscape_batch_min_items


async def _maybe_submit(
    db_factory, state: WorkerState, loop: _Loop, stage: str, epoch_id: uuid.UUID
) -> int:
    if not loop.llm or not await _submit_due(db_factory, state, stage, epoch_id, loop.now):
        return 0
    state.last_submit[(epoch_id, stage)] = loop.now
    return await _SUBMITTERS[stage](db_factory, epoch_id)


# --- step 3: synchronous work -----------------------------------------------------------------


def _fallback_adapters(phase: str) -> tuple[FallbackAdapter, ...]:
    early = (
        *screen_stage.FALLBACKS, *extract_stage.FALLBACKS, *triage_stage.FALLBACKS,
        *reject_audit.FALLBACKS,
    )
    if phase not in _DEEP_PHASES:
        return early
    return (*early, *comparator.FALLBACKS, *s8.FALLBACKS, *final_stage.FALLBACKS)


async def _sync_step(db_factory, state: WorkerState, loop: _Loop, work: list) -> None:
    """S6, S7 insertions, similarity judgments and the fallback chains, at most
    ``landscape_sync_concurrency`` at a time; nothing while a simulation run is live (spec
    §12.2 step 4, D33) or over the daily cap."""
    if not loop.llm or not loop.funnel:
        return
    async with db_factory() as db:
        if await liveness.simulation_live(db):
            logger.info("a simulation run is live: synchronous landscaping work waits")
            return
    tasks: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        (f"fallbacks {adapter.stage}", _fallback_thunk(db_factory, adapter))
        for adapter in _fallback_adapters(loop.phase)
    ]
    if loop.phase in _DEEP_PHASES:
        for epoch_id in work:
            tasks += await _deep_tasks(db_factory, epoch_id)
            tasks += await _rerank_tasks(db_factory, state, epoch_id)
            tasks += await _similarity_tasks(db_factory, epoch_id)
    semaphore = asyncio.Semaphore(max(1, get_settings().landscape_sync_concurrency))

    async def bounded(name: str, thunk: Callable[[], Awaitable[Any]]) -> None:
        async with semaphore:
            if not _shutdown:
                await _step(name, thunk())

    await asyncio.gather(*(bounded(name, thunk) for name, thunk in tasks))


def _fallback_thunk(db_factory, adapter: FallbackAdapter) -> Callable[[], Awaitable[int]]:
    return lambda: batches.run_fallbacks(db_factory, adapter)


async def _deep_tasks(db_factory, epoch_id: uuid.UUID) -> list:
    """S6 for each queued row at its idea's current version, and for the probe ideas without
    an ended S6 record: every live probe in the active epoch, every issued probe in a candidate
    (D62)."""
    Idea, Dp = LandscapeIdea, LandscapeDeep
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        if epoch is None:
            return []
        queued = set(
            (
                await db.scalars(
                    select(Dp.idea_id).join(Idea, Idea.id == Dp.idea_id).where(
                        Dp.epoch_id == epoch_id, Dp.status == "pending",
                        Dp.idea_version == Idea.version_hash,
                    )
                )
            ).all()
        )
        queued |= await _probe_ideas(db, epoch)
    return [
        (f"S6 {idea_id}", _deep_thunk(db_factory, idea_id, epoch_id))
        for idea_id in sorted(queued)
    ]


def _deep_thunk(db_factory, idea_id: uuid.UUID, epoch_id: uuid.UUID):
    return lambda: deep_stage.run_deep(db_factory, idea_id, epoch_id=epoch_id)


async def _probe_ideas(db: AsyncSession, epoch: LandscapeEpoch) -> set[uuid.UUID]:
    """The epoch's ideas behind probes that have no ended S6 record at their current version.
    A probe's idea is found by its member ``idea_key``."""
    V, Opp = LandscapeOpportunityVersion, LandscapeOpportunity
    query = select(V.member_idea_keys).where(V.is_probe.is_(True))
    if epoch.status == "active":
        query = query.join(Opp, Opp.id == V.opportunity_id).where(Opp.retired_at.is_(None))
    else:
        issued = select(LandscapeReviewBatchItem.opportunity_id).where(
            LandscapeReviewBatchItem.is_probe.is_(True)
        )
        query = query.where(V.opportunity_id.in_(issued))
    keys = {key for members in (await db.scalars(query)).all() for key in members or []}
    if not keys:
        return set()
    Idea, Dp = LandscapeIdea, LandscapeDeep
    ended = select(Dp.id).where(
        Dp.idea_id == Idea.id, Dp.epoch_id == epoch.id, Dp.idea_version == Idea.version_hash,
        Dp.status != "pending",
    ).exists()
    return set(
        (await db.scalars(select(Idea.id).where(
            Idea.epoch_id == epoch.id, Idea.idea_key.in_(keys), ~ended,
        ))).all()
    )


async def _rerank_tasks(db_factory, state: WorkerState, epoch_id: uuid.UUID) -> list:
    """S7 for the current cut. A rebuild whose tournament completed with no batch ending
    afterwards (its last answers came through the fallback chain) lands first, once (§7.8)."""
    async with db_factory() as db:
        cut = await _current_cut(db, epoch_id)
        if cut is None:
            return []
        rebuild = await rerank_stage.newest_rebuild_batch(db, epoch_id)
        if rebuild is not None and (
            rebuild in state.landed or not await comparator.tournament_complete(db, rebuild)
        ):
            rebuild = None
    cut_id = cut.id

    async def rerank() -> None:
        if rebuild is not None:
            await rerank_stage.finish_rebuild(db_factory, epoch_id=epoch_id, batch_id=rebuild)
            state.landed.add(rebuild)
        await rerank_stage.update_rankings(db_factory, epoch_id=epoch_id, cut_id=cut_id)

    return [(f"S7 {epoch_id}", rerank)]


async def _similarity_tasks(db_factory, epoch_id: uuid.UUID) -> list:
    """``judge_similarity`` for each pair of the current cut's S6-passed ideas that
    ``related_edges`` links by a kind other than ``similarity`` (spec §7.9, D14). Answers are
    cached per version pair, so a pair already judged makes no call."""
    async with db_factory() as db:
        cut = await _current_cut(db, epoch_id)
        if cut is None:
            return []
        passed = await _s6_passed(db, epoch_id, cut)
        facts = await s8.idea_facts(db, epoch_id=epoch_id, idea_ids=passed) if passed else []
    pairs = sorted({(a, b) for a, b, kind in s8.related_edges(facts) if kind != "similarity"})
    return [
        (f"similarity {a}/{b}", _similarity_thunk(db_factory, a, b, epoch_id)) for a, b in pairs
    ]


def _similarity_thunk(db_factory, a: uuid.UUID, b: uuid.UUID, epoch_id: uuid.UUID):
    return lambda: s8.judge_similarity(db_factory, a, b, epoch_id=epoch_id)


# --- step 4: cut, S8, labels, S9 --------------------------------------------------------------


async def _current_cut(db: AsyncSession, epoch_id: uuid.UUID) -> LandscapeCut | None:
    """The epoch's current cut: its newest complete one (``stages.cut``)."""
    C = LandscapeCut
    return await db.scalar(
        select(C)
        .where(C.epoch_id == epoch_id, C.status == "complete")
        .order_by(C.completed_at.desc().nulls_last(), C.created_at.desc())
        .limit(1)
    )


def _top(cut: LandscapeCut) -> list[uuid.UUID]:
    return [uuid.UUID(str(idea_id)) for idea_id in (cut.ordered_idea_ids or [])[:cut.k]]


async def _s6_passed(db: AsyncSession, epoch_id: uuid.UUID, cut: LandscapeCut) -> list[uuid.UUID]:
    top = _top(cut)
    if not top:
        return []
    passed = set(
        (
            await db.scalars(
                select(LandscapeDeep.idea_id).where(
                    LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.idea_id.in_(top),
                    LandscapeDeep.status.in_(_PASSED),
                )
            )
        ).all()
    )
    return [idea_id for idea_id in top if idea_id in passed]


async def _cut_step(db_factory, state: WorkerState, loop: _Loop, work: list) -> None:
    """S5 when due (and every loop while a boundary tournament is pending), S8 once S6 has
    ended for the current cut, then the label and S9 batches. Only in ``pilot`` (picks applied)
    and ``continuous`` (D69)."""
    if loop.phase not in _DEEP_PHASES or not loop.funnel:
        return
    for epoch_id in work:
        await _step(f"cut {epoch_id}", _cut_epoch(db_factory, loop, epoch_id))
        await _step(f"S8 {epoch_id}", _group_epoch(db_factory, state, epoch_id))
        await _step("submit label", _maybe_submit(db_factory, state, loop, "label", epoch_id))
        await _step("submit final", _maybe_submit(db_factory, state, loop, "final", epoch_id))


async def _cut_epoch(db_factory, loop: _Loop, epoch_id: uuid.UUID) -> None:
    C, T, Idea = LandscapeCut, LandscapeTriage, LandscapeIdea
    async with db_factory() as db:
        pending = await db.scalar(select(exists().where(
            C.epoch_id == epoch_id, C.status == "boundary_pending",
        )))
        last = await db.scalar(select(func.max(C.created_at)).where(C.epoch_id == epoch_id))
        s4_pending = await db.scalar(
            select(func.count()).select_from(T).join(Idea, Idea.id == T.idea_id).where(
                T.epoch_id == epoch_id, T.status.in_(_IN_FLIGHT),
                T.idea_version == Idea.version_hash,
            )
        ) or 0
    if pending or cut_stage.cut_due(now=loop.now, s4_pending=s4_pending, last_cut_at=last):
        await cut_stage.run_cut(db_factory, epoch_id, may_submit=loop.llm)


async def _group_epoch(db_factory, state: WorkerState, epoch_id: uuid.UUID) -> None:
    """S8 over the current cut, once, after S6 has ended for every idea of its top K."""
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        cut = await _current_cut(db, epoch_id)
        if epoch is None or cut is None or (epoch_id, cut.id) in state.grouped:
            return
        cut_id = cut.id
        if await _grouped(db, epoch, cut_id):
            state.grouped.add((epoch_id, cut_id))
            return
        if not await _s6_ended(db, epoch_id, cut):
            return
    await s8.build_opportunities(db_factory, epoch_id=epoch_id, cut_id=cut_id)
    state.grouped.add((epoch_id, cut_id))


async def _grouped(db: AsyncSession, epoch: LandscapeEpoch, cut_id: uuid.UUID) -> bool:
    model = LandscapeOpportunityVersion if epoch.status == "active" else LandscapeCandidateGrouping
    return bool(await db.scalar(select(exists().where(
        model.epoch_id == epoch.id, model.cut_id == cut_id,
    ))))


async def _s6_ended(db: AsyncSession, epoch_id: uuid.UUID, cut: LandscapeCut) -> bool:
    """Every top-K idea of ``cut`` has an S6 record and none is still owed."""
    top = _top(cut)
    if not top:
        return True
    rows = (
        await db.execute(
            select(LandscapeDeep.idea_id, LandscapeDeep.status).where(
                LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.idea_id.in_(top)
            )
        )
    ).all()
    seen = {idea_id for idea_id, _ in rows}
    return set(top) <= seen and not any(status in _IN_FLIGHT for _, status in rows)


async def _reports(db_factory, state: WorkerState, now: datetime) -> None:
    """The day's metrics, drift and positive-control reports (§10.3, §5.1, §10.2)."""
    if not state.once_today("reports", now.date()):
        return
    builders = (
        ("metrics", reports.pipeline_metrics),
        ("drift", reports.drift_report),
        ("positive_controls", reports.positive_control_trace),
    )
    for kind, build in builders:
        await _step(f"report {kind}", _store_report(db_factory, kind, build))


async def _store_report(db_factory, kind: str, build: Callable[[AsyncSession], Awaitable[dict]]):
    async with db_factory() as db:
        payload = json.loads(json.dumps(await build(db), default=str))
        db.add(LandscapeReport(kind=kind, payload=payload, created_at=func.clock_timestamp()))
        await db.commit()


# --- step 5: epochs ---------------------------------------------------------------------------


async def _epoch_step(db_factory, loop: _Loop) -> None:
    """Candidate → regression → re-judge → activation (spec §7.11, D65, D70)."""
    async with db_factory() as db:
        epoch = await db.scalar(
            select(LandscapeEpoch)
            .where(LandscapeEpoch.status == "candidate")
            .order_by(LandscapeEpoch.created_at.desc())
            .limit(1)
        )
    if epoch is None:
        return
    if epoch.rejudge_started_at is None:
        await _begin_rejudge(db_factory, loop, epoch)
    else:
        await _advance_rejudge(db_factory, loop, epoch)


async def _begin_rejudge(db_factory, loop: _Loop, epoch: LandscapeEpoch) -> None:
    """Run the candidate's regression set, or skip it for an override, then begin its re-judge:
    the unchanged stages are copied forward from the epoch it was built from (§7.11 steps 1-2).
    The first candidate's regression waits for the bake-off, which seeds the set; in ``pilot``
    nothing begins before the bake-off's picks are applied (D65, D68)."""
    if not loop.funnel:
        return
    if epoch.override_at is None:
        async with db_factory() as db:
            if await _latest_report(db, "bakeoff") is None:
                return
        if not loop.llm:
            return
        if not await regression.run_regression(db_factory, epoch.id):
            await _hold_failed(db_factory, epoch.id)
            return
    changed = set(epoch.changed_stages or _ALL_STAGES)
    unchanged = tuple(stage for stage in _ALL_STAGES if stage not in changed)
    async with db_factory() as db:
        await epochs.copy_forward(
            db, from_epoch=epoch.built_from_epoch_id, to_epoch=epoch.id, stages=unchanged,
        )
    logger.info("epoch %s: re-judge begun, %d stage(s) copied forward", epoch.id, len(unchanged))


async def _hold_failed(db_factory, epoch_id: uuid.UUID) -> None:
    async with db_factory() as db:
        current = await db.get(LandscapeEpoch, epoch_id, populate_existing=True)
        if current is not None and current.status == "candidate":
            await epochs.hold(db, epoch_id, "the candidate failed its regression set (spec §10.4)")


async def _advance_rejudge(db_factory, loop: _Loop, epoch: LandscapeEpoch) -> None:
    """Carry rescues, mark the drain, and activate a ready candidate, only in ``continuous``
    (D70), with ``activate_groupings`` as the hook; then freeze the new baseline (D65)."""
    async with db_factory() as db:
        if epoch.built_from_epoch_id is not None:
            await rescue.carry_rescues(
                db, from_epoch=epoch.built_from_epoch_id, to_epoch=epoch.id,
            )
        await epochs.mark_drained(db, epoch_id=epoch.id)
        if loop.phase != "continuous":
            return
        if not await final_stage.candidate_ready(db, epoch_id=epoch.id):
            return
        await epochs.activate(db, epoch.id, on_activate=s8.activate_groupings)
    logger.info("epoch %s activated", epoch.id)
    await regression.refresh_after_activation(db_factory, epoch_id=epoch.id)


# --- steps 6-9: audit, probes, experts, bake-off and pilot ------------------------------------


async def _audit_step(db_factory, state: WorkerState, loop: _Loop, work: list) -> None:
    """The reject audit, once per intake cycle (a UTC day), on the active epoch, else the
    re-judging candidate (spec §10.5, D66)."""
    if loop.phase not in ("backfill", "continuous") or not loop.llm or not work:
        return
    if state.once_today("audit", loop.now.date()):
        await reject_audit.submit_reject_audit(db_factory, epoch_id=work[0])


async def _probe_step(db_factory, now: datetime) -> None:
    """Draw (or replace) the probes for the next issue, one batch cycle ahead (§10.6, D62);
    ``draw_probes`` draws only when that batch is due blind."""
    async with db_factory() as db:
        last = await db.scalar(select(func.max(LandscapeReviewBatch.issued_on)))
    issue_on = last + review_batches.ISSUE_INTERVAL if last is not None else now.date()
    await review_batches.draw_probes(db_factory, for_issue_on=issue_on)


async def _expert_step(db_factory, state: WorkerState, now: datetime) -> None:
    """The quarter's expert sample (L55, D63); ``expert_sample`` skips experts already drawn
    this quarter, so a daily call draws for a newly marked expert only."""
    if state.once_today("experts", now.date()):
        quarter = f"{now.year}-Q{(now.month - 1) // 3 + 1}"
        await review_batches.expert_sample(db_factory, quarter=quarter)


async def _evaluation_step(db_factory, state: WorkerState, loop: _Loop) -> None:
    """Positive controls (pilot), a bake-off when the pilot intake is in or a request is newer
    than the latest bake-off, and the pilot's S4 repeatability and report (D68, §10.1-§10.2)."""
    if loop.phase == "pilot" and state.once_today("controls", loop.now.date()):
        await _step("positive controls", _in_session(db_factory, intake.ingest_positive_controls))
    if not loop.llm:
        return
    trigger = await _bakeoff_trigger(db_factory, loop)
    if trigger is not None:
        if trigger not in state.tried:
            state.tried.add(trigger)
            await bakeoff.run_bakeoff(db_factory)
            await _mark_requests_handled(db_factory)
        return
    if loop.phase == "pilot" and await _pilot_measurements_due(db_factory):
        repeatability = await bakeoff.run_s4_repeatability(db_factory)
        await reports.pilot_report(db_factory, s4_repeatability=repeatability)


async def _bakeoff_trigger(db_factory, loop: _Loop) -> str | None:
    """A key for the bake-off now due, or None: the first one once the pilot intake is in,
    and one for each ``bakeoff_request`` report newer than the latest ``bakeoff``."""
    async with db_factory() as db:
        latest = await _latest_report(db, "bakeoff")
        request = await _latest_report(db, "bakeoff_request")
        if request is not None and (latest is None or request.created_at > latest.created_at):
            return f"request:{request.id}"
        if loop.phase == "pilot" and latest is None and await _pilot_intake_in(db, loop.now):
            return "pilot"
    return None


async def _pilot_intake_in(db: AsyncSession, now: datetime) -> bool:
    """Every pilot slice has ended: none would be due even once a recently failed slice's retry
    wait (``intake.FAILED_RETRY_AFTER``) has passed, and no fetch is running."""
    due = await intake.schedule_due(db, now=now + intake.FAILED_RETRY_AFTER)
    running = await db.scalar(select(exists().where(LandscapeFetch.status == "running")))
    return not due and not running


async def _mark_requests_handled(db_factory) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeReport)
            .where(LandscapeReport.kind == "bakeoff_request", LandscapeReport.handled_at.is_(None))
            .values(handled_at=func.now())
        )
        await db.commit()


async def _pilot_measurements_due(db_factory) -> bool:
    """The pilot funnel has run (a cut grouped by S8, nothing owed in any stage) and no pilot
    report is newer than the latest bake-off."""
    async with db_factory() as db:
        latest = await _latest_report(db, "bakeoff")
        if latest is None:
            return False
        pilot = await _latest_report(db, "pilot")
        if pilot is not None and pilot.created_at > latest.created_at:
            return False
        epoch = await _rejudging(db)
        if epoch is None:
            return False
        cut = await _current_cut(db, epoch.id)
        if cut is None or not await _grouped(db, epoch, cut.id):
            return False
        return not await _funnel_busy(db, epoch.id)


async def _funnel_busy(db: AsyncSession, epoch_id: uuid.UUID) -> bool:
    if await _s2_s4_pending(db, epoch_id):
        return True
    for model in (LandscapeDeep, LandscapeComparison, LandscapeCandidateGrouping,
                  LandscapeAssessment):
        if await db.scalar(select(exists().where(
            and_(model.epoch_id == epoch_id, model.status.in_(_IN_FLIGHT))
        ))):
            return True
    return False


# --- entry point ------------------------------------------------------------------------------


def run() -> None:
    """``python -m src.landscaping.worker``: SIGTERM/SIGINT finish the current unit of work and
    exit, as ``src/worker/main.py`` does; a refused start exits with status 1."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        asyncio.run(main())
    except WorkerStartRefused as exc:
        logger.error("landscaping worker not started: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    run()
