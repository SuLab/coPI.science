"""S7, the ranked list, against Postgres (spec §7.8; D13, X3).

The synchronous insertion comparator is an oracle over invented strengths that counts its
questions; the Message Batches API is a fake ``create``, and rebuild answers are fed to
``ingest_tournament_outcomes``. A real synchronous call fails the test. Most sessions share one
connection whose outer transaction rolls back; the serialization test commits through the
engine and deletes what it made. Ideas and summaries are invented.
"""
import asyncio
import contextlib
import itertools
import json
import math
import random
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import get_settings
from src.landscaping import batches, comparator, epochs, liveness
from src.landscaping.batches import BatchOutcome
from src.landscaping.comparator import ingest_tournament_outcomes
from src.landscaping.stages import rerank
from src.landscaping.stages.rerank import card_order, finish_rebuild, update_rankings
from src.models.landscape import (
    LandscapeBatch,
    LandscapeBatchItem,
    LandscapeComparison,
    LandscapeCut,
    LandscapeDeep,
    LandscapeEpoch,
    LandscapeIdea,
    LandscapeRanking,
)

pytestmark = pytest.mark.integration

MODEL = "claude-opus-5"
_minutes = itertools.count(1)


class _FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict]] = []

    def create(self, *, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id=f"msgbatch_rerank_{len(self.created):04d}")


class _Inputs:
    def system(self, stage: str) -> str:
        return f"Invented {stage} system prompt."

    def model(self, stage: str) -> str:
        return MODEL


class _Oracle:
    """The insertion comparator: the stronger idea ranks above; every question is logged as
    (inserted idea, list idea)."""

    def __init__(self) -> None:
        self.strength: dict[uuid.UUID, float] = {}
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.delay = 0.0

    async def compare(self, db_factory, a, b, *, epoch_id, basis):
        assert basis == "deep"
        self.calls.append((a, b))
        if self.delay:
            await asyncio.sleep(self.delay)
        return -1 if self.strength[a] > self.strength[b] else 1

    def asked(self, idea) -> int:
        return sum(1 for a, _ in self.calls if a == idea)


@pytest.fixture(autouse=True)
def fake(monkeypatch, pg_url):
    client = _FakeBatches()
    monkeypatch.setattr(batches, "_RECONCILER", batches._Reconciler())
    monkeypatch.setattr(batches, "_batches_client", lambda: client)

    async def _inputs(db, epoch_id):
        return _Inputs()

    async def _no_sync(*args, **kwargs):
        raise AssertionError("S7 made a real synchronous call")

    async def _not_live(db):
        return False

    monkeypatch.setattr(epochs, "epoch_inputs", _inputs)
    monkeypatch.setattr(comparator, "call_sync", _no_sync)
    monkeypatch.setattr(liveness, "simulation_live", _not_live)
    oracle = _Oracle()
    monkeypatch.setattr(comparator, "compare", oracle.compare)
    # The rerank lock is a session advisory lock on its own connection to the test database.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    get_settings.cache_clear()
    client.oracle = oracle
    yield client
    get_settings.cache_clear()


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


async def _epoch(db_factory) -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(status="candidate")
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _ideas(db_factory, epoch_id, n: int, oracle: _Oracle, rng: random.Random) -> list:
    """``n`` invented ideas with a done S6 record each, and a hidden strength."""
    async with db_factory() as db:
        ideas = [
            LandscapeIdea(
                id=uuid.uuid4(), epoch_id=epoch_id, idea_type="biology", version_hash="v1",
                idea_key=f"invented-rerank-idea-{uuid.uuid4().hex}",
            )
            for _ in range(n)
        ]
        db.add_all(ideas)
        await db.flush()
        db.add_all([
            LandscapeDeep(
                id=uuid.uuid4(), idea_id=idea.id, idea_version="v1", epoch_id=epoch_id,
                status="done", provisional_weighted_score=3.0,
                summary=f"Invented S6 summary {idea.idea_key}.",
            )
            for idea in ideas
        ])
        await db.commit()
    for idea in ideas:
        oracle.strength[idea.id] = rng.random()
    return [idea.id for idea in ideas]


async def _cut(db_factory, epoch_id, ids) -> uuid.UUID:
    """A complete cut whose top K is ``ids``, newer than every cut made before it."""
    async with db_factory() as db:
        cut = LandscapeCut(
            epoch_id=epoch_id, status="complete", n=len(ids), k=len(ids),
            ordered_idea_ids=[str(i) for i in ids], folds={"folded": {}},
            completed_at=datetime(2026, 1, 5, tzinfo=UTC) + timedelta(minutes=next(_minutes)),
        )
        db.add(cut)
        await db.commit()
        return cut.id


async def _seed_list(db_factory, epoch_id, ordered) -> uuid.UUID:
    """``ordered`` as a landed rebuild: ranking rows on each idea's S6 record, and one
    answered batched comparison between each adjacent pair. Returns the batch row id."""
    async with db_factory() as db:
        deep = dict((await db.execute(
            select(LandscapeDeep.idea_id, LandscapeDeep.id)
            .where(LandscapeDeep.idea_id.in_(ordered))
        )).all())
        batch = LandscapeBatch(
            stage="rerank", model=MODEL, row_table="landscape_comparisons", status="ingested"
        )
        db.add(batch)
        await db.flush()
        items = [
            LandscapeBatchItem(id=uuid.uuid4(), batch_id=batch.id, item_seq=n, row_id=uuid.uuid4())
            for n in range(len(ordered) - 1)
        ]
        db.add_all(items)
        await db.flush()
        for item, (upper, lower) in zip(items, itertools.pairwise(ordered), strict=True):
            a, b = sorted((upper, lower))
            db.add(LandscapeComparison(
                id=item.row_id, idea_a=a, version_a="v1", idea_b=b, version_b="v1",
                epoch_id=epoch_id, basis="deep", ask_order=1, status="done",
                winner_idea_id=upper, batch_item_id=item.id,
            ))
        db.add_all([
            LandscapeRanking(id=deep[idea], epoch_id=epoch_id, idea_id=idea, position=n)
            for n, idea in enumerate(ordered, 1)
        ])
        await db.commit()
        return batch.id


async def _ranking(db_factory, epoch_id) -> list[uuid.UUID]:
    async with db_factory() as db:
        rows = (await db.execute(
            select(LandscapeRanking.idea_id, LandscapeRanking.position)
            .where(LandscapeRanking.epoch_id == epoch_id)
            .order_by(LandscapeRanking.position)
        )).all()
    assert [p for _, p in rows] == list(range(1, len(rows) + 1))
    return [idea for idea, _ in rows]


def _strongest_first(oracle: _Oracle, ids) -> list[uuid.UUID]:
    return sorted(ids, key=lambda i: -oracle.strength[i])


async def _latest_batch(db_factory) -> uuid.UUID:
    async with db_factory() as db:
        return await db.scalar(
            select(LandscapeBatch.id).where(LandscapeBatch.stage == "rerank")
            .order_by(LandscapeBatch.seq.desc()).limit(1)
        )


async def _answer(db_factory, batch_id, oracle: _Oracle) -> None:
    """A succeeded result for every item of ``batch_id``: the stronger idea wins both orders."""
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
        letter = "A" if oracle.strength[first] > oracle.strength[second] else "B"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("k", [75, 400])
async def test_insertion_uses_log_comparisons(db_factory, fake, k):
    oracle, rng = fake.oracle, random.Random(k)
    epoch_id = await _epoch(db_factory)
    base = _strongest_first(oracle, await _ideas(db_factory, epoch_id, k, oracle, rng))
    await _seed_list(db_factory, epoch_id, base)
    new = await _ideas(db_factory, epoch_id, 3, oracle, rng)

    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, base + new)
    )

    bound = math.ceil(math.log2(k + len(new)))
    assert all(1 <= oracle.asked(idea) <= bound for idea in new)
    assert len(oracle.calls) == sum(oracle.asked(idea) for idea in new)
    assert fake.created == []
    assert await _ranking(db_factory, epoch_id) == _strongest_first(oracle, base + new)


@pytest.mark.asyncio
async def test_rebuild_when_empty_or_over_quarter_new(db_factory, fake):
    oracle, rng = fake.oracle, random.Random(1)
    empty_epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, empty_epoch, 4, oracle, rng)
    await update_rankings(
        db_factory, epoch_id=empty_epoch, cut_id=await _cut(db_factory, empty_epoch, ideas)
    )
    assert len(fake.created) == 1
    assert len(fake.created[0]) == 12  # every pair of 4, in both orders
    assert oracle.calls == []

    epoch_id = await _epoch(db_factory)
    base = _strongest_first(oracle, await _ideas(db_factory, epoch_id, 8, oracle, rng))
    await _seed_list(db_factory, epoch_id, base)
    first = await _ideas(db_factory, epoch_id, 2, oracle, rng)
    await update_rankings(  # 2 of 10 are new: inserted
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, base + first)
    )
    assert len(fake.created) == 1
    asked = len(oracle.calls)
    assert asked > 0

    second = await _ideas(db_factory, epoch_id, 3, oracle, rng)
    await update_rankings(  # the 2 inserted since the rebuild and 3 more: 5 of 13
        db_factory, epoch_id=epoch_id,
        cut_id=await _cut(db_factory, epoch_id, base + first + second),
    )
    assert len(fake.created) == 2
    assert len(oracle.calls) == asked


@pytest.mark.asyncio
async def test_insertions_wait_while_rebuild_in_flight(db_factory, fake):
    oracle, rng = fake.oracle, random.Random(2)
    epoch_id = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch_id, 4, oracle, rng)
    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, ideas)
    )
    assert len(fake.created) == 1

    entrant = await _ideas(db_factory, epoch_id, 1, oracle, rng)
    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, ideas + entrant)
    )

    assert oracle.calls == []
    assert await _ranking(db_factory, epoch_id) == []


@pytest.mark.asyncio
async def test_no_second_rebuild_while_in_flight(db_factory, fake):
    oracle, rng = fake.oracle, random.Random(3)
    epoch_id = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch_id, 4, oracle, rng)
    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, ideas)
    )

    many = await _ideas(db_factory, epoch_id, 8, oracle, rng)
    for _ in range(2):
        await update_rankings(
            db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, ideas + many)
        )

    assert len(fake.created) == 1
    assert oracle.calls == []


@pytest.mark.asyncio
async def test_rebuild_landing_removes_leavers_and_inserts_entrants(db_factory, fake):
    oracle, rng = fake.oracle, random.Random(4)
    epoch_id = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch_id, 5, oracle, rng)
    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, ideas)
    )
    batch_id = await _latest_batch(db_factory)

    leaver, staying = ideas[0], ideas[1:]
    (entrant,) = await _ideas(db_factory, epoch_id, 1, oracle, rng)
    await update_rankings(  # in flight: waits
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, staying + [entrant])
    )
    assert await _ranking(db_factory, epoch_id) == []

    await _answer(db_factory, batch_id, oracle)
    await finish_rebuild(db_factory, epoch_id=epoch_id, batch_id=batch_id)

    ranking = await _ranking(db_factory, epoch_id)
    assert leaver not in ranking
    assert ranking == _strongest_first(oracle, staying + [entrant])
    assert oracle.calls and all(a == entrant for a, _ in oracle.calls)
    assert oracle.asked(entrant) <= math.ceil(math.log2(len(staying) + 1))

    asked = len(oracle.calls)
    await finish_rebuild(db_factory, epoch_id=epoch_id, batch_id=batch_id)
    assert await _ranking(db_factory, epoch_id) == ranking
    assert len(oracle.calls) == asked


@pytest.mark.asyncio
async def test_insertions_serialized(engine, fake, monkeypatch):
    oracle, rng = fake.oracle, random.Random(5)
    oracle.delay = 0.01
    monkeypatch.setattr(rerank, "LOCK_RETRY_SECONDS", 0.05)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    epoch_id = await _epoch(factory)
    batch_id = None
    try:
        base = _strongest_first(oracle, await _ideas(factory, epoch_id, 12, oracle, rng))
        batch_id = await _seed_list(factory, epoch_id, base)
        new = await _ideas(factory, epoch_id, 2, oracle, rng)
        cut_id = await _cut(factory, epoch_id, base + new)

        await asyncio.gather(
            update_rankings(factory, epoch_id=epoch_id, cut_id=cut_id),
            update_rankings(factory, epoch_id=epoch_id, cut_id=cut_id),
        )

        ranking = await _ranking(factory, epoch_id)
        assert ranking == _strongest_first(oracle, base + new)
        # One binary search at a time: each inserted idea's questions are contiguous, and no
        # idea was searched for twice (the second call read the list the first had written).
        searched = [a for a, _ in oracle.calls]
        runs = [idea for idea, _ in itertools.groupby(searched)]
        assert sorted(runs) == sorted(new)
        asked = {frozenset(call) for call in oracle.calls}
        stored = {frozenset(pair) for pair in itertools.pairwise(base)}
        for pair in itertools.pairwise(ranking):
            assert frozenset(pair) in asked | stored
    finally:
        async with factory() as db:
            await db.execute(delete(LandscapeEpoch).where(LandscapeEpoch.id == epoch_id))
            if batch_id is not None:
                await db.execute(delete(LandscapeBatch).where(LandscapeBatch.id == batch_id))
            await db.commit()


@pytest.mark.asyncio
async def test_unchanged_recut_no_calls(db_factory, fake):
    oracle, rng = fake.oracle, random.Random(6)
    epoch_id = await _epoch(db_factory)
    base = _strongest_first(oracle, await _ideas(db_factory, epoch_id, 8, oracle, rng))
    await _seed_list(db_factory, epoch_id, base)
    new = await _ideas(db_factory, epoch_id, 1, oracle, rng)
    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, base + new)
    )
    before = await _ranking(db_factory, epoch_id)
    asked = len(oracle.calls)

    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, base + new)
    )

    assert len(oracle.calls) == asked
    assert fake.created == []
    assert await _ranking(db_factory, epoch_id) == before


@pytest.mark.asyncio
async def test_removed_idea_no_calls(db_factory, fake):
    oracle, rng = fake.oracle, random.Random(7)
    epoch_id = await _epoch(db_factory)
    base = _strongest_first(oracle, await _ideas(db_factory, epoch_id, 8, oracle, rng))
    await _seed_list(db_factory, epoch_id, base)
    remaining = base[:3] + base[4:]

    await update_rankings(
        db_factory, epoch_id=epoch_id, cut_id=await _cut(db_factory, epoch_id, remaining)
    )

    assert oracle.calls == []
    assert fake.created == []
    assert await _ranking(db_factory, epoch_id) == remaining


def test_card_order_uses_s7_position_else_s5_rank():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    cards = {"first": [a], "second": [b, c], "unranked": [d]}
    order = card_order(cards, s7_positions={a: 3, c: 1}, s5_ranks={a: 9, b: 2})
    assert order == ["second", "first", "unranked"]
