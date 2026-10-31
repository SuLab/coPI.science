"""S5, the cut, against Postgres (spec §7.6; D11, D34, D37, D52, X2).

The Message Batches API is a fake ``create``; tournament answers are fed to
``ingest_tournament_outcomes`` as ``BatchOutcome`` objects. Synchronous calls fail the test:
the boundary tie-break must never make one. Every session shares one connection whose outer
transaction rolls back. Ideas, documents and dossiers are invented.
"""
import contextlib
import itertools
import json
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import batches, comparator, epochs
from src.landscaping.batches import BatchOutcome
from src.landscaping.comparator import ingest_tournament_outcomes, tournament_complete
from src.landscaping.stages import cut as cut_stage
from src.landscaping.stages.cut import finish_cut, run_cut
from src.models.landscape import (
    LandscapeBatchItem,
    LandscapeComparison,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeTriage,
)

pytestmark = pytest.mark.integration

MODEL = "claude-opus-5"
ANCESTRY = {("MONDO:0000001", "MONDO:0000002")}
_counter = itertools.count()


class _FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict]] = []

    def create(self, *, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id=f"msgbatch_cut_{len(self.created):04d}")


class _Inputs:
    def system(self, stage: str) -> str:
        return f"Invented {stage} system prompt."

    def model(self, stage: str) -> str:
        return MODEL


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    client = _FakeBatches()
    client.sync_calls = []
    monkeypatch.setattr(batches, "_RECONCILER", batches._Reconciler())
    monkeypatch.setattr(batches, "_batches_client", lambda: client)

    # The cut's epochs store no snapshot: the tie-break's prompt and model are patched in, so
    # the requests are independent of the prompt files on disk.
    async def _inputs(db, epoch_id):
        return _Inputs()

    monkeypatch.setattr(epochs, "epoch_inputs", _inputs)
    monkeypatch.setattr(cut_stage, "is_ancestor", lambda a, b: (a, b) in ANCESTRY)

    async def _no_sync(*args, **kwargs):
        client.sync_calls.append(args)
        raise AssertionError("the cut made a synchronous call")

    monkeypatch.setattr(comparator, "call_sync", _no_sync)
    return client


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


async def _epoch(db_factory, **kw) -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(status="candidate", **kw)
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _idea(
    db_factory, epoch_id, *, score: float, version: str = "v1", status: str | None = "done",
    targets: tuple[str, ...] | None = None, disease: str = "MONDO:0009999",
    direction: str = "inhibit",
) -> uuid.UUID:
    n = next(_counter)
    async with db_factory() as db:
        idea = LandscapeIdea(
            epoch_id=epoch_id, idea_key=f"invented-cut-idea-{n}-{uuid.uuid4().hex[:8]}",
            idea_type="biology", version_hash=version, direction=direction,
            targets=[{"hgnc_id": t} for t in (targets or (f"HGNC:{90000 + n}",))],
            disease={"mondo_id": disease}, jhu_group_count=1,
        )
        db.add(idea)
        await db.flush()
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-cut-{n}-{uuid.uuid4().hex}",
            s1_status="eligible", published_on=date(2026, 1, 5),
        )
        db.add(doc)
        await db.flush()
        extraction = LandscapeExtraction(
            document_id=doc.id, epoch_id=epoch_id, basis="summary", status="done"
        )
        db.add(extraction)
        await db.flush()
        db.add(LandscapeIdeaEvidence(idea_id=idea.id, extraction_id=extraction.id,
                                     candidate_index=0))
        if status is not None:
            db.add(_triage(idea.id, epoch_id, version, status, score))
        await db.commit()
        return idea.id


def _triage(idea_id, epoch_id, version: str, status: str, score: float) -> LandscapeTriage:
    return LandscapeTriage(
        idea_id=idea_id, idea_version=version, epoch_id=epoch_id, status=status,
        triage_score=score, evidence_count=5,
        raw={"dossier": f"Invented blinded dossier {idea_id} {version}."},
    )


async def _cuts(db_factory, epoch_id) -> list[LandscapeCut]:
    async with db_factory() as db:
        return list((await db.scalars(
            select(LandscapeCut).where(LandscapeCut.epoch_id == epoch_id)
            .order_by(LandscapeCut.created_at)
        )).all())


async def _cut(db_factory, cut_id) -> LandscapeCut:
    async with db_factory() as db:
        return await db.get(LandscapeCut, cut_id)


async def _deep(db_factory, epoch_id) -> list[tuple[uuid.UUID, str]]:
    async with db_factory() as db:
        return [tuple(r) for r in (await db.execute(
            select(LandscapeDeep.idea_id, LandscapeDeep.idea_version)
            .where(LandscapeDeep.epoch_id == epoch_id)
        )).all()]


def _top(cut: LandscapeCut) -> list[str]:
    return cut.ordered_idea_ids[:cut.k]


async def _answer(db_factory, batch_id, prefer: list[uuid.UUID]) -> None:
    """A succeeded result for every item of ``batch_id``: the idea earlier in ``prefer``
    wins, in both orders."""
    async with db_factory() as db:
        items = (await db.execute(
            select(LandscapeBatchItem.id, LandscapeBatchItem.item_seq, LandscapeBatchItem.row_id)
            .where(LandscapeBatchItem.batch_id == batch_id)
        )).all()
        rows = {r.id: r for r in (await db.scalars(
            select(LandscapeComparison)
            .where(LandscapeComparison.id.in_([i.row_id for i in items]))
        )).all()}
    outcomes = []
    for item_id, item_seq, row_id in items:
        row = rows[row_id]
        first, second = (
            (row.idea_a, row.idea_b) if row.ask_order == 1 else (row.idea_b, row.idea_a)
        )
        letter = "A" if prefer.index(first) < prefer.index(second) else "B"
        message = SimpleNamespace(
            model=MODEL, stop_reason="end_turn",
            content=[SimpleNamespace(
                type="text", text=json.dumps({"winner": letter, "reason": "Invented."})
            )],
        )
        outcomes.append(BatchOutcome(
            batch_row_id=batch_id, batch_item_id=item_id, item_seq=item_seq, row_id=row_id,
            stage="rerank", kind="succeeded", model=MODEL, message=message, usage={},
        ))
    await ingest_tournament_outcomes(db_factory, outcomes)


async def _tied_class(db_factory, epoch_id) -> tuple[list[uuid.UUID], LandscapeCut]:
    """Three ideas equal on keys 1-5, cut into a pending boundary tournament."""
    ideas = [await _idea(db_factory, epoch_id, score=4.0) for _ in range(3)]
    assert await run_cut(db_factory, epoch_id) is None
    (pending,) = await _cuts(db_factory, epoch_id)
    return ideas, pending


@pytest.mark.asyncio
async def test_pool_uses_previous_version_while_new_waits(db_factory):
    epoch_id = await _epoch(db_factory)
    waiting = await _idea(db_factory, epoch_id, score=4.8, version="v2", status=None)
    async with db_factory() as db:
        db.add(_triage(waiting, epoch_id, "v1", "done", 4.8))
        db.add(_triage(waiting, epoch_id, "v2", "pending", 0.0))
        await db.commit()
    other = await _idea(db_factory, epoch_id, score=3.0)
    never_triaged = await _idea(db_factory, epoch_id, score=0.0, status="pending")

    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))

    assert cut.ordered_idea_ids == [str(waiting), str(other)]
    assert str(never_triaged) not in cut.ordered_idea_ids


@pytest.mark.asyncio
async def test_no_refill_after_s6_exclusion(db_factory):
    epoch_id = await _epoch(db_factory)
    best = await _idea(db_factory, epoch_id, score=4.5)
    second = await _idea(db_factory, epoch_id, score=3.0)
    first_cut = await run_cut(db_factory, epoch_id)
    assert await _deep(db_factory, epoch_id) == [(best, "v1")]

    async with db_factory() as db:
        await db.execute(update(LandscapeDeep).where(LandscapeDeep.epoch_id == epoch_id)
            .values(status="excluded:deep_gate"))
        await db.commit()

    cut = await _cut(db_factory, first_cut)
    assert _top(cut) == [str(best)]
    assert (second, "v1") not in await _deep(db_factory, epoch_id)


@pytest.mark.asyncio
async def test_deep_gate_excluded_leaves_next_pool(db_factory):
    epoch_id = await _epoch(db_factory)
    best = await _idea(db_factory, epoch_id, score=4.5)
    second = await _idea(db_factory, epoch_id, score=3.0)
    await run_cut(db_factory, epoch_id)
    async with db_factory() as db:
        await db.execute(update(LandscapeDeep).where(LandscapeDeep.epoch_id == epoch_id)
            .values(status="excluded:deep_gate"))
        await db.commit()

    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))

    assert str(best) not in cut.ordered_idea_ids
    assert _top(cut) == [str(second)]
    assert (second, "v1") in await _deep(db_factory, epoch_id)


@pytest.mark.asyncio
async def test_fold_disease_ancestor(db_factory):
    epoch_id = await _epoch(db_factory)
    keeper = await _idea(db_factory, epoch_id, score=4.5, targets=("HGNC:7",),
                         disease="MONDO:0000002")
    child = await _idea(db_factory, epoch_id, score=4.0, targets=("HGNC:7",),
                        disease="MONDO:0000001")
    other = await _idea(db_factory, epoch_id, score=3.0)

    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))

    assert cut.ordered_idea_ids == [str(keeper), str(other)]
    assert cut.folds["folded"] == {str(keeper): [str(child)]}
    assert cut.n == 2


@pytest.mark.asyncio
async def test_fold_target_subset(db_factory):
    epoch_id = await _epoch(db_factory)
    keeper = await _idea(db_factory, epoch_id, score=4.5, targets=("HGNC:7", "HGNC:8"),
                         disease="MONDO:0000003")
    subset = await _idea(db_factory, epoch_id, score=4.0, targets=("HGNC:8",),
                         disease="MONDO:0000003")
    other_direction = await _idea(db_factory, epoch_id, score=3.0, targets=("HGNC:8",),
                                  disease="MONDO:0000003", direction="activate")

    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))

    assert cut.folds["folded"] == {str(keeper): [str(subset)]}
    assert cut.ordered_idea_ids == [str(keeper), str(other_direction)]


@pytest.mark.asyncio
async def test_k_is_ceil_one_percent_of_unfolded(db_factory):
    epoch_id = await _epoch(db_factory)
    await _idea(db_factory, epoch_id, score=4.99, targets=("HGNC:1", "HGNC:2", "HGNC:3"),
                disease="MONDO:0000004")
    for target in ("HGNC:1", "HGNC:2"):
        await _idea(db_factory, epoch_id, score=1.0, targets=(target,), disease="MONDO:0000004")
    for i in range(99):
        await _idea(db_factory, epoch_id, score=4.9 - i * 0.01)

    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))
    assert (cut.n, cut.k) == (100, 1)

    await _idea(db_factory, epoch_id, score=0.5)
    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))
    assert (cut.n, cut.k) == (101, 2)


@pytest.mark.asyncio
async def test_boundary_tie_class_resolved_by_batch_without_s6(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    ideas, pending = await _tied_class(db_factory, epoch_id)

    assert pending.status == "boundary_pending"
    assert pending.tie_class_size == 3
    assert pending.boundary_batch_id is not None
    assert len(fake.created) == 1 and len(fake.created[0]) == 6  # 3 pairs, both orders
    assert await _deep(db_factory, epoch_id) == []

    prefer = [ideas[2], ideas[0], ideas[1]]
    await _answer(db_factory, pending.boundary_batch_id, prefer)
    async with db_factory() as db:
        assert await tournament_complete(db, pending.boundary_batch_id)
    cut_id = await finish_cut(
        db_factory, epoch_id=epoch_id, tournament_batch_id=pending.boundary_batch_id
    )

    cut = await _cut(db_factory, cut_id)
    assert cut.status == "complete"
    assert cut.ordered_idea_ids == [str(i) for i in prefer]
    assert await _deep(db_factory, epoch_id) == [(ideas[2], "v1")]
    assert fake.sync_calls == []


@pytest.mark.asyncio
async def test_previous_cut_current_until_boundary_batch_ends(db_factory):
    epoch_id = await _epoch(db_factory)
    leader = await _idea(db_factory, epoch_id, score=4.5)
    await _idea(db_factory, epoch_id, score=3.0)
    first = await run_cut(db_factory, epoch_id)
    challenger = await _idea(db_factory, epoch_id, score=4.5)

    assert await run_cut(db_factory, epoch_id) is None

    async with db_factory() as db:
        current = await db.scalar(
            select(LandscapeCut.id)
            .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "complete")
            .order_by(LandscapeCut.completed_at.desc()).limit(1)
        )
        flags = dict((await db.execute(
            select(LandscapeIdea.id, LandscapeIdea.in_current_cut)
            .where(LandscapeIdea.epoch_id == epoch_id)
        )).all())
        pending = await db.scalar(
            select(LandscapeCut).where(LandscapeCut.status == "boundary_pending",
                                       LandscapeCut.epoch_id == epoch_id)
        )
    assert current == first
    assert flags[leader] is True and flags[challenger] is False

    await _answer(db_factory, pending.boundary_batch_id, [challenger, leader])
    second = await finish_cut(
        db_factory, epoch_id=epoch_id, tournament_batch_id=pending.boundary_batch_id
    )
    assert _top(await _cut(db_factory, second)) == [str(challenger)]
    async with db_factory() as db:
        assert (await db.get(LandscapeIdea, challenger, populate_existing=True)).in_current_cut
        assert not (await db.get(LandscapeIdea, leader, populate_existing=True)).in_current_cut


@pytest.mark.asyncio
async def test_cut_skipped_while_boundary_batch_pending(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    await _tied_class(db_factory, epoch_id)

    assert await run_cut(db_factory, epoch_id) is None

    cuts = await _cuts(db_factory, epoch_id)
    assert [c.status for c in cuts] == ["boundary_pending"]
    assert len(fake.created) == 1


@pytest.mark.asyncio
async def test_candidate_cut_frozen_after_post_drain_cut(db_factory):
    epoch_id = await _epoch(db_factory)
    await _idea(db_factory, epoch_id, score=4.5)
    await _idea(db_factory, epoch_id, score=3.0)
    # Cuts before the drain do not freeze the candidate.
    assert await run_cut(db_factory, epoch_id) is not None
    assert await run_cut(db_factory, epoch_id) is not None
    async with db_factory() as db:
        await db.execute(
            update(LandscapeEpoch).where(LandscapeEpoch.id == epoch_id)
            .values(drained_at=func.clock_timestamp())
        )
        await db.commit()

    assert await run_cut(db_factory, epoch_id) is not None
    assert await run_cut(db_factory, epoch_id) is None
    assert len(await _cuts(db_factory, epoch_id)) == 3


@pytest.mark.asyncio
async def test_comparator_cached_across_cuts(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    ideas, pending = await _tied_class(db_factory, epoch_id)
    prefer = [ideas[1], ideas[2], ideas[0]]
    await _answer(db_factory, pending.boundary_batch_id, prefer)
    await finish_cut(db_factory, epoch_id=epoch_id, tournament_batch_id=pending.boundary_batch_id)

    recut = await run_cut(db_factory, epoch_id)

    assert recut is not None
    assert (await _cut(db_factory, recut)).ordered_idea_ids == [str(i) for i in prefer]
    assert len(fake.created) == 1
    assert fake.sync_calls == []


@pytest.mark.asyncio
async def test_rescued_idea_in_pool(db_factory):
    epoch_id = await _epoch(db_factory)
    kept = await _idea(db_factory, epoch_id, score=4.0)
    rescued = await _idea(db_factory, epoch_id, score=2.0, status="rescued")
    excluded = await _idea(db_factory, epoch_id, score=4.9, status="excluded:gate")

    cut = await _cut(db_factory, await run_cut(db_factory, epoch_id))

    assert cut.ordered_idea_ids == [str(kept), str(rescued)]
    assert str(excluded) not in cut.ordered_idea_ids


@pytest.mark.asyncio
async def test_s6_requeue_throttled_per_batch_cycle(db_factory):
    epoch_id = await _epoch(db_factory)
    leader = await _idea(db_factory, epoch_id, score=4.5)
    await _idea(db_factory, epoch_id, score=3.0)
    await run_cut(db_factory, epoch_id)
    assert await _deep(db_factory, epoch_id) == [(leader, "v1")]

    async with db_factory() as db:
        await db.execute(
            update(LandscapeIdea).where(LandscapeIdea.id == leader).values(version_hash="v2")
        )
        db.add(_triage(leader, epoch_id, "v2", "done", 4.5))
        await db.commit()
    await run_cut(db_factory, epoch_id)
    assert (leader, "v2") not in await _deep(db_factory, epoch_id)

    # The S6 run is now older than the batch cycle: the changed version is re-queued once.
    async with db_factory() as db:
        await db.execute(
            update(LandscapeDeep).where(LandscapeDeep.epoch_id == epoch_id)
            .values(created_at=func.now() - func.make_interval(0, 0, 0, 8))
        )
        await db.commit()
    await run_cut(db_factory, epoch_id)
    assert (leader, "v2") in await _deep(db_factory, epoch_id)
