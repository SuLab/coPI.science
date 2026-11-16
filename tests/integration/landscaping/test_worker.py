"""The landscaping worker process against Postgres (spec §12.2, §7.11, §9.5, §15; D59, D65,
D68, D69, D70).

The stage modules are tested on their own; here every collaborator that would call a model,
an external source or the Batches API is replaced by a recorder (``calls``), so each test reads
which steps the worker took. Epoch, phase and readiness logic runs for real. Every session of a
test shares one connection whose outer transaction rolls back, so ``now()`` is the same for every
row a test writes. Every name below is invented.
"""
import contextlib
import functools
import json
import re
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
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
    worker,
)
from src.landscaping.batches import BatchOutcome
from src.landscaping.eval import bakeoff, regression, reject_audit
from src.landscaping.schemas import GATING_KEYS
from src.landscaping.stages import cut as cut_stage
from src.landscaping.stages import deep as deep_stage
from src.landscaping.stages import extract as extract_stage
from src.landscaping.stages import final as final_stage
from src.landscaping.stages import opportunities
from src.landscaping.stages import rerank as rerank_stage
from src.landscaping.stages import screen as screen_stage
from src.landscaping.stages import triage as triage_stage
from src.models import AppSetting
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeCandidateGrouping,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRegressionRun,
    LandscapeRegressionSet,
    LandscapeReport,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeScreen,
    LandscapeSource,
    LandscapeTriage,
)
from src.services import pubmed
from src.services.advisory_locks import LANDSCAPE_WORKER_LOCK_KEY, SessionAdvisoryLock

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
ALL_STAGES = (
    "screen", "extract", "triage", "cut", "deep", "rerank", "similarity", "label", "final",
)
INVENTED_LAB = "Lab of Invented Fen Pharmacology"

REAL_SCHEDULE_DUE = intake.schedule_due
REAL_RUN_CUT = cut_stage.run_cut
REAL_BUILD_OPPORTUNITIES = opportunities.build_opportunities

#: Batch submissions, the reject audit included: none may happen while paused, disabled or
#: over the daily cap.
SUBMISSIONS = frozenset({
    "submit_screen_batch", "submit_extract_batch", "submit_triage_batch", "submit_label_batch",
    "submit_final_batch", "submit_reject_audit",
})
#: Synchronous LLM work.
SYNC_LLM = frozenset({
    "run_deep", "update_rankings", "judge_similarity", "run_fallbacks", "run_regression",
    "run_bakeoff", "run_s4_repeatability",
})
#: S5-S9 and review-batch work, none of which runs in phase backfill.
DEEP_WORK = frozenset({
    "run_cut", "build_opportunities", "submit_label_batch", "submit_final_batch", "run_deep",
    "update_rankings", "judge_similarity", "issue_weekly_batch", "draw_probes", "expert_sample",
})

STUBS = (
    (batches, "reconcile_orphans", 0),
    (batches, "mark_ingested", None),
    (batches, "run_fallbacks", 0),
    (intake, "schedule_due", []),
    (intake, "run_slice", None),
    (intake, "link_grants", 0),
    (intake, "link_datasets", 0),
    (intake, "ingest_positive_controls", 0),
    (ideas, "apply_lab_unifications", 0),
    (ideas, "resolve_aliases", 0),
    (ideas, "assemble_evidence", None),
    (screen_stage, "submit_screen_batch", 0),
    (extract_stage, "submit_extract_batch", 0),
    (triage_stage, "submit_triage_batch", 0),
    (opportunities, "submit_label_batch", 0),
    (final_stage, "submit_final_batch", 0),
    (opportunities, "build_opportunities", None),
    (opportunities, "judge_similarity", {}),
    (opportunities, "activate_groupings", None),
    (cut_stage, "run_cut", None),
    (cut_stage, "finish_cut", None),
    (deep_stage, "run_deep", None),
    (rerank_stage, "update_rankings", None),
    (rerank_stage, "finish_rebuild", None),
    (review_batches, "issue_weekly_batch", None),
    (review_batches, "draw_probes", []),
    (review_batches, "expert_sample", []),
    (reports, "pipeline_metrics", {}),
    (reports, "drift_report", {}),
    (reports, "positive_control_trace", {}),
    (reports, "pilot_report", None),
    (reject_audit, "submit_reject_audit", 0),
    (regression, "run_regression", True),
    (regression, "refresh_after_activation", 0),
    (bakeoff, "run_bakeoff", None),
    (bakeoff, "run_s4_repeatability", {}),
    (rescue, "carry_rescues", 0),
    (liveness, "simulation_live", False),
    (ledger, "over_daily_cap", False),
)


class Calls:
    """Every stubbed call, in order, as ``(name, args, kwargs)``; ``results`` overrides a
    stub's return value by name; ``chunks`` is what the faked ``batches.poll`` yields."""

    def __init__(self) -> None:
        self.log: list[tuple[str, tuple, dict]] = []
        self.results: dict[str, object] = {}
        self.chunks: list[tuple[uuid.UUID, list[BatchOutcome], bool]] = []

    def names(self) -> list[str]:
        return [name for name, _, _ in self.log]

    def args(self, name: str) -> list[tuple[tuple, dict]]:
        return [(args, kwargs) for n, args, kwargs in self.log if n == name]

    def epochs_given(self, name: str) -> set:
        """The epoch each call of a submitter was given, positionally or as ``epoch_id``."""
        return {
            kw.get("epoch_id", args[1] if len(args) > 1 else None)
            for args, kw in self.args(name)
        }

    def stub(self, name: str, default: object):
        async def fake(*args, **kwargs):
            self.log.append((name, args, kwargs))
            return self.results.get(name, default)

        return fake


@pytest.fixture
def calls(monkeypatch):
    recorded = Calls()
    for module, name, default in STUBS:
        monkeypatch.setattr(module, name, recorded.stub(name, default))

    async def fake_poll(db_factory, *, chunk_size=1000):
        recorded.log.append(("poll", (), {}))
        for chunk in recorded.chunks:
            yield chunk

    monkeypatch.setattr(batches, "poll", fake_poll)
    return recorded


@pytest.fixture
def settings(monkeypatch):
    current = get_settings()
    for name, value in {
        "landscape_enabled": True,
        "landscape_sync_concurrency": 1,
        "landscape_batch_poll_seconds": 0,
        "landscape_batch_min_items": 500,
        "landscape_batch_max_wait_minutes": 60,
        "landscape_daily_usd_cap": None,
    }.items():
        monkeypatch.setattr(current, name, value)
    return current


@pytest.fixture(autouse=True)
def restore_ncbi_interval():
    yield
    pubmed.set_min_interval(None)


@pytest_asyncio.fixture
async def db_factory(engine):
    async with engine.connect() as conn:
        trans = await conn.begin()

        @contextlib.asynccontextmanager
        async def factory():
            session = AsyncSession(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield session
            finally:
                await session.close()

        try:
            yield factory
        finally:
            if trans.is_active:
                await trans.rollback()


# --- builders -------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _snapshot() -> dict:
    return epochs.build_snapshot()


async def _noop(*_args, **_kwargs):
    return None


async def _epoch(db_factory, status: str, **values) -> uuid.UUID:
    fields = {
        "components": {}, "snapshot": _snapshot(), "changed_stages": list(ALL_STAGES), **values,
    }
    async with db_factory() as db:
        epoch = LandscapeEpoch(status=status, **fields)
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _epoch_row(db_factory, epoch_id: uuid.UUID) -> LandscapeEpoch:
    async with db_factory() as db:
        return await db.get(LandscapeEpoch, epoch_id, populate_existing=True)


async def _set(db_factory, key: str, value: str) -> None:
    async with db_factory() as db:
        await db.execute(
            pg_insert(AppSetting).values(key=key, value=value)
            .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
        )
        await db.commit()


async def _phase(db_factory) -> str | None:
    async with db_factory() as db:
        return await worker._app_setting(db, "landscape_phase")


async def _report(db_factory, kind: str, payload: dict | None = None) -> uuid.UUID:
    async with db_factory() as db:
        report = LandscapeReport(kind=kind, payload=payload)
        db.add(report)
        await db.commit()
        return report.id


async def _document(db_factory) -> uuid.UUID:
    """An eligible document with no S2 row: owed S2 work, which holds the D69 flip."""
    async with db_factory() as db:
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            title="An invented fen study", s1_status="eligible",
        )
        db.add(doc)
        await db.commit()
        return doc.id


def _key(symbol: str) -> str:
    return f"biology;{symbol};inhibit;MONDO:{uuid.uuid4().int % 10_000_000:07d}"


async def _idea(
    db_factory, epoch_id: uuid.UUID, *, symbol: str = "INVF1", deep_status: str | None = "done",
) -> LandscapeIdea:
    """One idea with its whole S1-S4 trail ended (document, S2 yes, S3 done, evidence assembled,
    S4 done at its version) and, unless ``deep_status`` is None, one S6 record."""
    async with db_factory() as db:
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            title=f"{symbol} loss slows an invented fen carcinoma",
            summary_text=f"{symbol} knockdown slowed tumour growth in mice.",
            published_on=date(2026, 3, 1), document_type="article",
            qualifying_institution=INVENTED_LAB, lab_key="name:fen|q", s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        screen = LandscapeScreen(
            document_id=doc.id, epoch_id=epoch_id, status="done", decision="yes",
        )
        extraction = LandscapeExtraction(
            document_id=doc.id, epoch_id=epoch_id, basis="summary", status="done",
            raw={"ideas": [], "linked_publications": []},
        )
        idea = LandscapeIdea(
            epoch_id=epoch_id, idea_key=_key(symbol), idea_type="biology",
            targets=[{"hgnc_id": "HGNC:90002", "symbol": symbol, "label": symbol}],
            direction="inhibit",
            disease={"mondo_id": "MONDO:9999998", "label": "invented fen carcinoma"},
            stage="target_validation", evidence_grade="in_vivo", jhu_group_count=1,
            version_hash=uuid.uuid4().hex, evidence_summary={}, evidence_status={},
            evidence_assembled_at=NOW - timedelta(days=3),
        )
        db.add_all([screen, extraction, idea])
        await db.flush()
        db.add(LandscapeIdeaEvidence(
            idea_id=idea.id, extraction_id=extraction.id, candidate_index=0,
        ))
        db.add(LandscapeTriage(
            idea_id=idea.id, idea_version=idea.version_hash, epoch_id=epoch_id, status="done",
            triage_score=3.5, evidence_count=1, evidence_grade="in_vivo",
            stage="target_validation",
        ))
        if deep_status is not None:
            db.add(LandscapeDeep(
                idea_id=idea.id, idea_version=idea.version_hash, epoch_id=epoch_id,
                status=deep_status, summary=f"{symbol} is an invented credible target.",
                gating={k: "met" for k in GATING_KEYS},
            ))
        await db.commit()
        return idea


async def _cut(db_factory, epoch_id: uuid.UUID, ideas_: list, *, at: datetime) -> uuid.UUID:
    async with db_factory() as db:
        cut = LandscapeCut(
            epoch_id=epoch_id, status="complete", n=len(ideas_), k=len(ideas_),
            ordered_idea_ids=[str(idea.id) for idea in ideas_], folds={"folded": {}},
            created_at=at, completed_at=at,
        )
        db.add(cut)
        await db.commit()
        return cut.id


async def _grouping(db_factory, epoch_id: uuid.UUID, cut_id: uuid.UUID, keys: list) -> uuid.UUID:
    async with db_factory() as db:
        grouping = LandscapeCandidateGrouping(
            epoch_id=epoch_id, cut_id=cut_id, grouping_key=uuid.uuid4().hex,
            member_idea_keys=keys, label="Invented fen grouping", status="done", current=True,
        )
        db.add(grouping)
        await db.commit()
        return grouping.id


async def _assessment(db_factory, epoch_id: uuid.UUID, *, status: str, **keys) -> uuid.UUID:
    async with db_factory() as db:
        row = LandscapeAssessment(epoch_id=epoch_id, status=status, **keys)
        db.add(row)
        await db.commit()
        return row.id


def _outcome(batch_id: uuid.UUID, stage: str) -> BatchOutcome:
    return BatchOutcome(
        batch_row_id=batch_id, batch_item_id=uuid.uuid4(), item_seq=1, row_id=uuid.uuid4(),
        stage=stage, kind="succeeded", model="claude-opus-5", message=None, usage={},
    )


# --- start ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_instance_exits(pg_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    get_settings.cache_clear()
    touched: list[str] = []

    async def schema_ready(db):
        touched.append("schema_ready")
        return True

    monkeypatch.setattr(worker, "schema_ready", schema_ready)
    holder = SessionAdvisoryLock(pg_url, LANDSCAPE_WORKER_LOCK_KEY)
    assert await holder.acquire()
    try:
        with pytest.raises(worker.WorkerStartRefused, match="lock"):
            await worker.main()
    finally:
        await holder.release()
        get_settings.cache_clear()
    assert touched == []


@pytest.mark.asyncio
async def test_refuses_without_0077(db_factory, settings, calls, monkeypatch):
    async def schema_ready(db):
        return False

    monkeypatch.setattr(worker, "schema_ready", schema_ready)
    async with db_factory() as db:
        before = await db.scalar(select(func.count()).select_from(LandscapeSource))
    with pytest.raises(worker.WorkerStartRefused, match="0077"):
        await worker.start(db_factory)
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(LandscapeSource)) == before
    assert "reconcile_orphans" not in calls.names()


@pytest.mark.asyncio
async def test_ncbi_interval_set_at_start(db_factory, settings, calls, monkeypatch):
    seen: list = []
    real = pubmed.set_min_interval

    def record(seconds):
        seen.append(seconds)
        real(seconds)

    monkeypatch.setattr(pubmed, "set_min_interval", record)
    monkeypatch.setattr(epochs, "ensure_candidate", _noop)
    await worker.start(db_factory)
    assert seen == [pytest.approx(1 / settings.landscape_ncbi_max_rps)]


@pytest.mark.asyncio
async def test_restart_reconciles_then_resets_stale(db_factory, settings, calls, monkeypatch):
    order: list = []
    real_reset = worker.reset_stale

    async def reconcile(db_factory_, *, may_submit):
        order.append(("reconcile", may_submit))
        return 0

    async def reset(db_factory_, *, now):
        order.append("reset")
        return await real_reset(db_factory_, now=now)

    monkeypatch.setattr(batches, "reconcile_orphans", reconcile)
    monkeypatch.setattr(worker, "reset_stale", reset)
    monkeypatch.setattr(epochs, "ensure_candidate", _noop)
    epoch_id = await _epoch(db_factory, "active")
    stale_idea = await _idea(db_factory, epoch_id, symbol="INVF2", deep_status=None)
    live_idea = await _idea(db_factory, epoch_id, symbol="INVF3", deep_status=None)
    now = datetime.now(UTC)
    async with db_factory() as db:
        stale = LandscapeDeep(
            idea_id=stale_idea.id, idea_version=stale_idea.version_hash, epoch_id=epoch_id,
            status="running", updated_at=now - timedelta(hours=2),
        )
        live = LandscapeDeep(
            idea_id=live_idea.id, idea_version=live_idea.version_hash, epoch_id=epoch_id,
            status="running", updated_at=now - timedelta(minutes=1),
        )
        db.add_all([stale, live])
        await db.commit()
        stale_id, live_id = stale.id, live.id

    await worker.start(db_factory, now=now)

    assert order == [("reconcile", False), "reset"]
    async with db_factory() as db:
        assert (await db.get(LandscapeDeep, stale_id, populate_existing=True)).status == "pending"
        assert (await db.get(LandscapeDeep, live_id, populate_existing=True)).status == "running"


# --- the kill switch, the pause flag and the cap --------------------------------------------


@pytest.mark.asyncio
async def test_paused_submits_nothing_but_ingests(db_factory, settings, calls, monkeypatch):
    await _epoch(db_factory, "active")
    await _set(db_factory, "landscape_phase", "continuous")
    await _set(db_factory, "landscape_paused", "true")
    batch_id = uuid.uuid4()
    outcome = _outcome(batch_id, "screen")
    calls.chunks = [(batch_id, [outcome], True)]
    seen: list = []

    async def ingest(db_factory_, outcomes):
        seen.extend(outcomes)

    monkeypatch.setitem(worker.INGESTERS, "screen", ingest)
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    assert seen == [outcome]
    assert calls.args("mark_ingested") == [((db_factory, batch_id), {})]
    assert calls.args("reconcile_orphans") == [((db_factory,), {"may_submit": False})]
    assert not set(calls.names()) & (SUBMISSIONS | SYNC_LLM)
    assert "schedule_due" not in calls.names()


@pytest.mark.asyncio
async def test_disabled_polls_only(db_factory, settings, calls, monkeypatch):
    monkeypatch.setattr(settings, "landscape_enabled", False)
    await _epoch(db_factory, "active")
    await _set(db_factory, "landscape_phase", "continuous")
    batch_id = uuid.uuid4()
    calls.chunks = [(batch_id, [_outcome(batch_id, "final")], True)]
    monkeypatch.setitem(worker.INGESTERS, "final", _noop)
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    assert set(calls.names()) == {"over_daily_cap", "reconcile_orphans", "poll", "mark_ingested"}
    assert calls.args("reconcile_orphans") == [((db_factory,), {"may_submit": False})]


@pytest.mark.asyncio
async def test_daily_cap_defers_llm_work_only(db_factory, settings, calls):
    calls.results["over_daily_cap"] = True
    calls.results["schedule_due"] = [("openalex", "daily:2026-10-30")]
    await _epoch(db_factory, "active")
    await _set(db_factory, "landscape_phase", "continuous")
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    names = set(calls.names())
    assert {"run_slice", "run_cut", "issue_weekly_batch"} <= names
    assert all(kwargs == {"may_submit": False} for _, kwargs in calls.args("run_cut"))
    assert not names & (SUBMISSIONS | SYNC_LLM)
    assert calls.args("reconcile_orphans") == [((db_factory,), {"may_submit": False})]


@pytest.mark.asyncio
async def test_daily_cap_reached_mid_loop_skips_remaining_sync_tasks(
    db_factory, settings, calls, monkeypatch,
):
    stub = calls.stub("run_fallbacks", 0)

    async def fallbacks_then_cap(*args, **kwargs):
        calls.results["over_daily_cap"] = True
        return await stub(*args, **kwargs)

    monkeypatch.setattr(batches, "run_fallbacks", fallbacks_then_cap)
    await _set(db_factory, "landscape_phase", "continuous")
    epoch_id = await _epoch(db_factory, "active")
    await _idea(db_factory, epoch_id, deep_status="pending")
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    assert len(worker._fallback_adapters("continuous")) > 1
    assert calls.names().count("run_fallbacks") == 1
    assert not set(calls.names()) & {"run_deep", "update_rankings", "judge_similarity"}


@pytest.mark.asyncio
async def test_daily_cap_reached_between_stages_skips_next_submission(
    db_factory, settings, calls, monkeypatch,
):
    stub = calls.stub("submit_screen_batch", 0)

    async def screen_then_cap(*args, **kwargs):
        calls.results["over_daily_cap"] = True
        return await stub(*args, **kwargs)

    monkeypatch.setattr(screen_stage, "submit_screen_batch", screen_then_cap)
    await _set(db_factory, "landscape_phase", "continuous")
    epoch_id = await _epoch(db_factory, "active")
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    assert calls.epochs_given("submit_screen_batch") == {epoch_id}
    assert not set(calls.names()) & {"submit_extract_batch", "submit_triage_batch"}


# --- routing ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcomes_routed_by_stage(db_factory, settings, calls, monkeypatch):
    stages = list(worker.INGESTERS)
    seen: dict[str, list] = defaultdict(list)

    def recorder(stage):
        async def ingest(db_factory_, outcomes):
            seen[stage].extend(outcomes)

        return ingest

    for stage in stages:
        monkeypatch.setitem(worker.INGESTERS, stage, recorder(stage))
    batch_ids = {stage: uuid.uuid4() for stage in stages}
    cut_epoch, rank_epoch = uuid.uuid4(), uuid.uuid4()

    async def tournament_keys(db, batch_id):
        if batch_id == batch_ids["rerank"]:
            return [(cut_epoch, "triage"), (rank_epoch, "deep")]
        return []

    monkeypatch.setattr(worker, "_tournament_keys", tournament_keys)
    outcomes = {stage: _outcome(batch_ids[stage], stage) for stage in stages}
    second = _outcome(batch_ids["screen"], "screen")
    unknown_batch = uuid.uuid4()
    calls.chunks = [
        (batch_ids["screen"], [outcomes["screen"]], False),
        (batch_ids["screen"], [second], True),
        *((batch_ids[s], [outcomes[s]], True) for s in stages if s != "screen"),
        (unknown_batch, [_outcome(unknown_batch, "invented_stage")], True),
    ]

    assert await worker.ingest_ended(db_factory) == len(stages)

    for stage in stages:
        expected = [outcomes[stage], second] if stage == "screen" else [outcomes[stage]]
        assert seen[stage] == expected
    marked = [args[1] for args, _ in calls.args("mark_ingested")]
    assert sorted(marked, key=str) == sorted(batch_ids.values(), key=str)
    assert unknown_batch not in marked
    assert calls.args("finish_cut") == [
        ((db_factory,), {"epoch_id": cut_epoch, "tournament_batch_id": batch_ids["rerank"]})
    ]
    assert "finish_rebuild" not in calls.names()


@pytest.mark.asyncio
async def test_rebuild_completed_by_fallbacks_lands_once(
    db_factory, settings, calls, monkeypatch,
):
    """A rebuild whose last answers came through the fallback chain has no batch end: the S7
    step lands it once, before updating the list (spec §7.8)."""
    await _set(db_factory, "landscape_phase", "continuous")
    epoch_id = await _epoch(db_factory, "active")
    await _cut(db_factory, epoch_id, [await _idea(db_factory, epoch_id)], at=NOW)
    rebuild = uuid.uuid4()

    async def newest(db, epoch_id_):
        return rebuild if epoch_id_ == epoch_id else None

    async def complete(db, batch_id):
        return batch_id == rebuild

    monkeypatch.setattr(rerank_stage, "newest_rebuild_batch", newest)
    monkeypatch.setattr(comparator, "tournament_complete", complete)
    state = worker.WorkerState()
    await worker.run_once(db_factory, state, now=NOW)
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))

    assert calls.args("finish_rebuild") == [
        ((db_factory,), {"epoch_id": epoch_id, "batch_id": rebuild})
    ]
    s7 = [name for name in calls.names() if name in ("finish_rebuild", "update_rankings")]
    assert s7[:2] == ["finish_rebuild", "update_rankings"]
    assert s7.count("update_rankings") == 2


@pytest.mark.asyncio
async def test_rebuild_ending_over_the_cap_lands_only_under_it(
    db_factory, settings, calls, monkeypatch,
):
    """Landing a rebuild inserts entrants with synchronous questions, so a rebuild batch that
    ends while the daily cap is reached is not landed by ingestion; the next loop under the cap
    lands it in the S7 step (spec §7.8, §9.5)."""
    await _set(db_factory, "landscape_phase", "continuous")
    epoch_id = await _epoch(db_factory, "active")
    await _cut(db_factory, epoch_id, [await _idea(db_factory, epoch_id)], at=NOW)
    rebuild = uuid.uuid4()

    async def tournament_keys(db, batch_id):
        return [(epoch_id, rerank_stage.BASIS)] if batch_id == rebuild else []

    async def newest(db, epoch_id_):
        return rebuild if epoch_id_ == epoch_id else None

    async def complete(db, batch_id):
        return batch_id == rebuild

    monkeypatch.setattr(worker, "_tournament_keys", tournament_keys)
    monkeypatch.setattr(rerank_stage, "newest_rebuild_batch", newest)
    monkeypatch.setattr(comparator, "tournament_complete", complete)
    monkeypatch.setitem(worker.INGESTERS, "rerank", _noop)
    calls.chunks = [(rebuild, [_outcome(rebuild, "rerank")], True)]
    calls.results["over_daily_cap"] = True
    state = worker.WorkerState()
    await worker.run_once(db_factory, state, now=NOW)

    assert calls.args("mark_ingested") == [((db_factory, rebuild), {})]
    assert not set(calls.names()) & {"finish_rebuild", "update_rankings"}

    calls.chunks = []
    calls.results["over_daily_cap"] = False
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))

    assert calls.args("finish_rebuild") == [
        ((db_factory,), {"epoch_id": epoch_id, "batch_id": rebuild})
    ]
    s7 = [name for name in calls.names() if name in ("finish_rebuild", "update_rankings")]
    assert s7 == ["finish_rebuild", "update_rankings"]


# --- phases -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_pilot_runs_no_backfill(db_factory, settings, calls, monkeypatch):
    monkeypatch.setattr(intake, "schedule_due", REAL_SCHEDULE_DUE)
    async with db_factory() as db:
        await intake.ensure_sources(db)
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    fetched = [(args[1], args[2]) for args, _ in calls.args("run_slice")]
    assert fetched
    assert all(key.startswith(("month:", "quarter:", "fy:", "feed:")) for _, key in fetched)
    openalex = [key for source, key in fetched if source == "openalex"]
    years = {key.split(":", 1)[1][:4] for key in openalex}
    assert len(openalex) == len(years), "the pilot takes one month per window year (D68)"
    backfill = [
        key for source, key in intake.backfill_slices(NOW.date(), settings.landscape_window_days)
        if source == "openalex"
    ]
    assert len(openalex) < len(backfill)
    assert ("jhtv", "feed:pilot") not in fetched
    assert await _phase(db_factory) is None


@pytest.mark.asyncio
async def test_pilot_funnel_waits_for_applied_picks(db_factory, settings, calls, monkeypatch):
    await _set(db_factory, "landscape_phase", "pilot")
    candidate = await _epoch(db_factory, "candidate")
    picks = {"recommended_settings": {"LANDSCAPE_SCREEN_MODEL": "claude-invented-pick"}}
    await _report(db_factory, "bakeoff", picks)
    state = worker.WorkerState()

    await worker.run_once(db_factory, state, now=NOW)
    assert "run_regression" not in calls.names()
    assert not set(calls.names()) & SUBMISSIONS
    assert (await _epoch_row(db_factory, candidate)).rejudge_started_at is None

    monkeypatch.setattr(settings, "landscape_screen_model", "claude-invented-pick")
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))
    assert calls.args("run_regression") == [((db_factory, candidate), {})]
    assert (await _epoch_row(db_factory, candidate)).rejudge_started_at is not None

    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=2))
    assert candidate in calls.epochs_given("submit_screen_batch")
    assert "issue_weekly_batch" not in calls.names()


@pytest.mark.asyncio
async def test_no_review_batch_before_backfill_cut(db_factory, settings, calls):
    await _set(db_factory, "landscape_phase", "backfill")
    candidate = await _epoch(db_factory, "candidate", rejudge_started_at=NOW - timedelta(days=9))
    await _document(db_factory)
    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    names = set(calls.names())
    assert candidate in calls.epochs_given("submit_screen_batch")
    assert not names & DEEP_WORK
    assert await _phase(db_factory) == "backfill"
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(LandscapeReviewBatch)) == 0


@pytest.mark.asyncio
async def test_backfill_complete_moves_to_continuous(db_factory, settings, calls):
    await _set(db_factory, "landscape_phase", "backfill")
    async with db_factory() as db:
        await intake.ensure_sources(db)
        await db.execute(
            update(LandscapeSource).values(enabled=LandscapeSource.source == "openalex")
        )
        await db.commit()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    candidate = await _epoch(db_factory, "candidate", rejudge_started_at=old, drained_at=old)
    keys = [
        key for source, key in intake.backfill_slices(NOW.date(), settings.landscape_window_days)
        if source == "openalex"
    ]
    async with db_factory() as db:
        for n, key in enumerate(keys[:-1]):
            db.add(LandscapeFetch(
                source="openalex", slice_key=key, started_at=NOW - timedelta(days=1),
                status="incomplete" if n == 0 else "complete", attempts=3 if n == 0 else 1,
            ))
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        screen = LandscapeScreen(document_id=doc.id, epoch_id=candidate, status="submitted")
        db.add(screen)
        await db.commit()
        screen_id = screen.id
    state = worker.WorkerState()

    await worker.run_once(db_factory, state, now=NOW)
    assert await _phase(db_factory) == "backfill", "a slice has no terminal fetch yet"

    async with db_factory() as db:
        db.add(LandscapeFetch(
            source="openalex", slice_key=keys[-1], started_at=NOW - timedelta(hours=1),
            status="complete", attempts=1,
        ))
        await db.commit()
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))
    assert await _phase(db_factory) == "backfill", "S2 work for a backfill document is pending"

    async with db_factory() as db:
        await db.execute(
            update(LandscapeScreen).where(LandscapeScreen.id == screen_id)
            .values(status="excluded:screen", decision="no")
        )
        await db.commit()
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=2))
    assert await _phase(db_factory) == "continuous"
    drained = (await _epoch_row(db_factory, candidate)).drained_at
    assert drained is not None and drained != old


# --- epochs -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_activation_in_pilot_or_backfill(db_factory, settings, calls, monkeypatch):
    async def ready(db, *, epoch_id):
        return True

    monkeypatch.setattr(final_stage, "candidate_ready", ready)
    candidate = await _epoch(db_factory, "candidate", rejudge_started_at=NOW - timedelta(days=9))
    await _document(db_factory)
    state = worker.WorkerState()
    for phase in ("pilot", "backfill"):
        await _set(db_factory, "landscape_phase", phase)
        await worker.run_once(db_factory, state, now=NOW)
        assert (await _epoch_row(db_factory, candidate)).status == "candidate", phase
        assert "refresh_after_activation" not in calls.names()

    await _set(db_factory, "landscape_phase", "continuous")
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))
    assert (await _epoch_row(db_factory, candidate)).status == "active"
    assert calls.args("refresh_after_activation") == [((db_factory,), {"epoch_id": candidate})]


@pytest.mark.asyncio
async def test_candidate_activates_despite_unassessable_probe(db_factory, settings, calls):
    drained = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    await _set(db_factory, "landscape_phase", "continuous")
    active = await _epoch(db_factory, "active")
    async with db_factory() as db:
        opportunity = LandscapeOpportunity(version_counter=1)
        db.add(opportunity)
        await db.flush()
        db.add(LandscapeOpportunityVersion(
            opportunity_id=opportunity.id, version=1, epoch_id=active,
            member_idea_keys=[_key("INVPROBE")], label="Invented probe", is_probe=True,
            status="done",
        ))
        batch = LandscapeReviewBatch(issued_on=date(2026, 10, 2), blind=True)
        db.add(batch)
        await db.flush()
        db.add(LandscapeReviewBatchItem(
            batch_id=batch.id, opportunity_id=opportunity.id, is_probe=True, position=1,
        ))
        await db.commit()
        probe_id = opportunity.id
    candidate = await _epoch(
        db_factory, "candidate", rejudge_started_at=drained - timedelta(days=1),
        drained_at=drained, built_from_epoch_id=active,
    )
    idea = await _idea(db_factory, candidate)
    cut_id = await _cut(db_factory, candidate, [idea], at=drained + timedelta(hours=1))
    grouping = await _grouping(db_factory, candidate, cut_id, [idea.idea_key])
    await _assessment(db_factory, candidate, status="done", grouping_id=grouping)

    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    assert (await _epoch_row(db_factory, candidate)).status == "active"
    assert (await _epoch_row(db_factory, active)).status == "retired"
    async with db_factory() as db:
        status = await db.scalar(
            select(LandscapeAssessment.status).where(
                LandscapeAssessment.epoch_id == candidate,
                LandscapeAssessment.opportunity_id == probe_id,
            )
        )
    assert status == final_stage.NO_DEEP


@pytest.mark.asyncio
async def test_first_candidate_waits_for_bakeoff(db_factory, settings, calls):
    await _set(db_factory, "landscape_phase", "backfill")
    await _document(db_factory)
    candidate = await _epoch(db_factory, "candidate")
    state = worker.WorkerState()

    await worker.run_once(db_factory, state, now=NOW)
    assert "run_regression" not in calls.names()
    assert candidate not in calls.epochs_given("submit_screen_batch")
    assert (await _epoch_row(db_factory, candidate)).rejudge_started_at is None

    await _report(db_factory, "bakeoff", {"recommended_settings": {}})
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))
    assert calls.args("run_regression") == [((db_factory, candidate), {})]
    assert (await _epoch_row(db_factory, candidate)).rejudge_started_at is not None


@pytest.mark.asyncio
async def test_first_activation_waits_for_full_window_cut(db_factory, settings, calls):
    old = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
    await _set(db_factory, "landscape_phase", "backfill")
    # The first candidate was superseded when the bake-off's picks were applied in the pilot.
    first = await _epoch(db_factory, "retired")
    candidate = await _epoch(
        db_factory, "candidate", rejudge_started_at=old - timedelta(days=1), drained_at=old,
        built_from_epoch_id=first,
    )
    idea = await _idea(db_factory, candidate)
    pilot_cut = await _cut(db_factory, candidate, [idea], at=old + timedelta(hours=1))
    pilot_grouping = await _grouping(db_factory, candidate, pilot_cut, [idea.idea_key])
    await _assessment(db_factory, candidate, status="done", grouping_id=pilot_grouping)
    state = worker.WorkerState()

    await worker.run_once(db_factory, state, now=NOW)
    assert await _phase(db_factory) == "continuous"
    epoch = await _epoch_row(db_factory, candidate)
    assert epoch.status == "candidate", "the pilot cut predates the drain"
    assert epoch.drained_at != old

    post_cut = await _cut(
        db_factory, candidate, [idea], at=epoch.drained_at + timedelta(minutes=1)
    )
    post_grouping = await _grouping(db_factory, candidate, post_cut, [idea.idea_key])
    assessment = await _assessment(
        db_factory, candidate, status="pending", grouping_id=post_grouping
    )
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=1))
    assert (await _epoch_row(db_factory, candidate)).status == "candidate", "S9 is owed"

    async with db_factory() as db:
        await db.execute(
            update(LandscapeAssessment).where(LandscapeAssessment.id == assessment)
            .values(status="done")
        )
        await db.commit()
    await worker.run_once(db_factory, state, now=NOW + timedelta(minutes=2))
    assert (await _epoch_row(db_factory, candidate)).status == "active"


@pytest.mark.asyncio
async def test_unpassed_candidate_gets_no_submissions(db_factory, settings, calls):
    calls.results["run_regression"] = False
    await _set(db_factory, "landscape_phase", "continuous")
    active = await _epoch(db_factory, "active")
    candidate = await _epoch(db_factory, "candidate", built_from_epoch_id=active)
    await _report(db_factory, "bakeoff", {"recommended_settings": {}})
    state = worker.WorkerState()

    await worker.run_once(db_factory, state, now=NOW)
    await worker.run_once(db_factory, state, now=NOW + timedelta(hours=2))

    epoch = await _epoch_row(db_factory, candidate)
    assert epoch.status == "held" and epoch.rejudge_started_at is None
    given = set()
    for name in SUBMISSIONS - {"submit_reject_audit"}:
        given |= calls.epochs_given(name)
    assert active in given
    assert candidate not in given


@pytest.mark.asyncio
async def test_passed_regression_is_not_run_again(db_factory, settings, calls):
    await _set(db_factory, "landscape_phase", "continuous")
    active = await _epoch(db_factory, "active")
    candidate = await _epoch(db_factory, "candidate", built_from_epoch_id=active)
    await _report(db_factory, "bakeoff", {"recommended_settings": {}})
    async with db_factory() as db:
        version = (await db.scalar(select(func.max(LandscapeRegressionSet.version))) or 0) + 1
        db.add(LandscapeRegressionSet(version=version))
        await db.flush()
        run = LandscapeRegressionRun(epoch_id=candidate, set_version=version, status="passed")
        db.add(run)
        await db.flush()
        await db.execute(
            update(LandscapeEpoch).where(LandscapeEpoch.id == candidate)
            .values(regression_set_version=version, regression_run_id=run.id)
        )
        await db.commit()

    await worker.run_once(db_factory, worker.WorkerState(), now=NOW)

    assert "run_regression" not in calls.names()
    assert (await _epoch_row(db_factory, candidate)).rejudge_started_at is not None


async def _active_state(db_factory, epoch_id: uuid.UUID) -> dict:
    """The active epoch's rows, each serialized whole, for a byte-for-byte comparison."""
    scoped = (
        LandscapeCut, LandscapeScreen, LandscapeExtraction, LandscapeIdea, LandscapeTriage,
        LandscapeDeep, LandscapeOpportunityVersion, LandscapeAssessment,
    )
    state: dict[str, list[str]] = {}
    async with db_factory() as db:
        for model in scoped:
            table = model.__table__
            rows = (await db.execute(select(table).where(table.c.epoch_id == epoch_id))).mappings()
            state[table.name] = sorted(
                json.dumps(dict(row), default=str, sort_keys=True) for row in rows.all()
            )
        for table in (LandscapeOpportunity.__table__, LandscapeEpoch.__table__):
            rows = (await db.execute(select(table))).mappings()
            state[table.name] = sorted(
                json.dumps(dict(row), default=str, sort_keys=True)
                for row in rows.all()
                if table.name != "landscape_epochs" or row["id"] == epoch_id
            )
    return state


@pytest.mark.asyncio
async def test_candidate_never_changes_active(db_factory, settings, calls, monkeypatch):
    """A real candidate re-judge (copy-forward, S5 and S8 for the candidate, the drain mark)
    while the active epoch serves leaves every active row byte-identical (spec §7.11, X1)."""
    calls.results["simulation_live"] = True  # no synchronous model work in this test

    async def candidate_cut(db_factory_, epoch_id, **kwargs):
        if epoch_id == candidate:
            return await REAL_RUN_CUT(db_factory_, epoch_id, **kwargs)
        return None

    async def not_ready(db, *, epoch_id):
        return False

    monkeypatch.setattr(cut_stage, "run_cut", candidate_cut)
    monkeypatch.setattr(opportunities, "build_opportunities", REAL_BUILD_OPPORTUNITIES)
    monkeypatch.setattr(final_stage, "candidate_ready", not_ready)
    await _set(db_factory, "landscape_phase", "continuous")
    active = await _epoch(db_factory, "active")
    idea = await _idea(db_factory, active)
    cut_id = await _cut(db_factory, active, [idea], at=NOW - timedelta(days=1))
    async with db_factory() as db:
        opportunity = LandscapeOpportunity(version_counter=1)
        db.add(opportunity)
        await db.flush()
        db.add(LandscapeOpportunityVersion(
            opportunity_id=opportunity.id, version=1, epoch_id=active, cut_id=cut_id,
            member_idea_keys=[idea.idea_key], label="Invented fen programme", status="done",
        ))
        await db.commit()
        opportunity_id = opportunity.id
    await _assessment(
        db_factory, active, status="done", opportunity_id=opportunity_id, opportunity_version=1,
    )
    candidate = await _epoch(
        db_factory, "candidate", built_from_epoch_id=active, changed_stages=["final"],
    )
    await _report(db_factory, "bakeoff", {"recommended_settings": {}})
    before = await _active_state(db_factory, active)

    state = worker.WorkerState()
    start = datetime.now(UTC)  # the copied cut is stamped now(): later days make S5 due again
    for day in range(3):
        await worker.run_once(db_factory, state, now=start + timedelta(days=day))

    assert await _active_state(db_factory, active) == before
    epoch = await _epoch_row(db_factory, candidate)
    assert epoch.status == "candidate" and epoch.rejudge_started_at is not None
    async with db_factory() as db:
        cuts = await db.scalar(
            select(func.count()).select_from(LandscapeCut)
            .where(LandscapeCut.epoch_id == candidate)
        )
    assert cuts >= 1


@pytest.mark.asyncio
async def test_never_touches_jobs_table(db_factory, engine, settings, calls, monkeypatch):
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    monkeypatch.setattr(epochs, "ensure_candidate", _noop)
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await worker.start(db_factory)
        await _set(db_factory, "landscape_phase", "continuous")
        active = await _epoch(db_factory, "active")
        await _idea(db_factory, active)
        state = worker.WorkerState()
        await worker.run_once(db_factory, state, now=NOW)
        await worker.run_once(db_factory, state, now=NOW + timedelta(days=1))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert statements
    assert [s for s in statements if re.search(r"\bjobs\b", s, re.IGNORECASE)] == []
