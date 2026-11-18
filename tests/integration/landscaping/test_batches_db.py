"""The Message Batches intent protocol against Postgres (spec §7.1 steps 1-5, D31, D42, F4).

The API is a fake ``messages.batches`` resource; a "restart" is a fresh in-process
reconciler, which is all a worker restart loses. Every session shares one connection whose
outer transaction rolls back, so the commits the protocol makes are savepoint releases.
"""
import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import batches
from src.landscaping.llm_client import CallResult
from src.models.landscape import (
    LandscapeBatch,
    LandscapeBatchItem,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeLlmCall,
    LandscapeScreen,
)
from src.services.llm_pricing import cost_for_request

pytestmark = pytest.mark.integration

MODEL = "claude-sonnet-5"


class _Killed(Exception):
    """Stands in for the process dying at a chosen point."""


def _message(stop_reason: str = "end_turn", *, input_tokens: int = 100, output_tokens: int = 20):
    return SimpleNamespace(
        model=MODEL, stop_reason=stop_reason, stop_details=None, content=[],
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


def _line(custom_id: str, kind: str = "succeeded", stop_reason: str = "end_turn", **usage):
    if kind == "succeeded":
        result = SimpleNamespace(type=kind, message=_message(stop_reason, **usage))
    else:
        result = SimpleNamespace(type=kind)
    return SimpleNamespace(custom_id=custom_id, result=result)


class FakeBatches:
    """The slice of ``client.messages.batches`` the module uses: create, retrieve, list
    (newest first) and results (a JSONL line iterator)."""

    def __init__(self) -> None:
        self.remote: dict[str, SimpleNamespace] = {}
        self.requests: dict[str, list[dict]] = {}
        self.lines: dict[str, list] = {}
        self.create_calls = 0
        self.results_calls = 0
        #: Raised before any batch is made.
        self.create_error: Exception | None = None
        #: Raised after the batch is made, as a dropped connection would.
        self.raise_after_create: Exception | None = None

    def _new(self, *, created_at: datetime | None = None) -> str:
        api_id = f"msgbatch_{len(self.remote) + 1:04d}"
        self.remote[api_id] = SimpleNamespace(
            id=api_id, created_at=created_at or datetime.now(UTC),
            processing_status="in_progress", ended_at=None,
        )
        return api_id

    def create(self, *, requests):
        self.create_calls += 1
        if self.create_error is not None:
            raise self.create_error
        api_id = self._new()
        self.requests[api_id] = list(requests)
        if self.raise_after_create is not None:
            raise self.raise_after_create
        return self.remote[api_id]

    def foreign(self) -> str:
        """A batch someone else made: its custom ids carry no ``b{seq}-`` prefix."""
        api_id = self._new()
        self.lines[api_id] = [_line("job-1")]
        return api_id

    def end(self, api_id: str, lines: list | None = None) -> None:
        remote = self.remote[api_id]
        remote.processing_status = "ended"
        remote.ended_at = datetime.now(UTC)
        if lines is not None:
            self.lines[api_id] = lines
        elif api_id not in self.lines:
            self.lines[api_id] = [_line(r["custom_id"]) for r in self.requests[api_id]]

    def custom_ids(self, api_id: str) -> list[str]:
        return [r["custom_id"] for r in self.requests[api_id]]

    def last(self) -> str:
        return list(self.remote)[-1]

    def retrieve(self, api_id):
        return self.remote[api_id]

    def list(self, *, limit=20):
        return sorted(self.remote.values(), key=lambda b: b.created_at, reverse=True)

    def results(self, api_id):
        self.results_calls += 1
        return iter(list(self.lines[api_id]))


@pytest.fixture(autouse=True)
def _fresh_reconciler(monkeypatch):
    monkeypatch.setattr(batches, "_RECONCILER", batches._Reconciler())


def _restart(monkeypatch) -> None:
    monkeypatch.setattr(batches, "_RECONCILER", batches._Reconciler())


@pytest.fixture
def fake(monkeypatch):
    client = FakeBatches()
    monkeypatch.setattr(batches, "_batches_client", lambda: client)
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


async def _screens(db_factory, n: int) -> list[uuid.UUID]:
    async with db_factory() as db:
        epoch = LandscapeEpoch(status="candidate")
        db.add(epoch)
        await db.flush()
        ids = []
        for _ in range(n):
            doc = LandscapeDocument(
                kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
                s1_status="eligible",
            )
            db.add(doc)
            await db.flush()
            screen = LandscapeScreen(document_id=doc.id, epoch_id=epoch.id)
            db.add(screen)
            await db.flush()
            ids.append(screen.id)
        await db.commit()
    return ids


def _request() -> dict:
    return {
        "model": MODEL, "max_tokens": 100,
        "messages": [{"role": "user", "content": "An invented summary."}],
    }


async def _submit(db_factory, ids: list[uuid.UUID]) -> uuid.UUID:
    return await batches.submit(
        db_factory, stage="screen", row_model=LandscapeScreen,
        items=[(row_id, _request()) for row_id in ids],
    )


async def _rows(db_factory, ids: list[uuid.UUID]) -> dict[uuid.UUID, LandscapeScreen]:
    async with db_factory() as db:
        rows = (
            await db.scalars(select(LandscapeScreen).where(LandscapeScreen.id.in_(ids)))
        ).all()
    return {row.id: row for row in rows}


async def _statuses(db_factory, ids: list[uuid.UUID]) -> list[str]:
    rows = await _rows(db_factory, ids)
    return [rows[row_id].status for row_id in ids]


async def _batch(db_factory, batch_id: uuid.UUID) -> LandscapeBatch:
    async with db_factory() as db:
        return await db.get(LandscapeBatch, batch_id)


async def _batch_of(db_factory, row_id: uuid.UUID) -> LandscapeBatch:
    """The latest batch row holding ``row_id``."""
    async with db_factory() as db:
        return await db.scalar(
            select(LandscapeBatch)
            .join(LandscapeBatchItem, LandscapeBatchItem.batch_id == LandscapeBatch.id)
            .where(LandscapeBatchItem.row_id == row_id)
            .order_by(LandscapeBatch.seq.desc())
            .limit(1)
        )


async def _drain(db_factory, chunk_size: int = 1000) -> list:
    return [chunk async for chunk in batches.poll(db_factory, chunk_size=chunk_size)]


async def _apply_generic(db_factory, outcome) -> bool:
    async with db_factory() as db:
        row = await db.get(LandscapeScreen, outcome.row_id)
        changed = await batches.apply_result(
            db, LandscapeScreen, row_id=outcome.row_id, batch_item_id=outcome.batch_item_id,
            values=batches.transition(
                outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                model=outcome.model,
            ),
        )
        await db.commit()
    return changed


async def _apply_done(db_factory, outcome) -> bool:
    async with db_factory() as db:
        changed = await batches.apply_result(
            db, LandscapeScreen, row_id=outcome.row_id, batch_item_id=outcome.batch_item_id,
            values={"status": "done", "decision": "yes"},
        )
        await db.commit()
    return changed


def _crash_after_create(monkeypatch) -> None:
    async def _killed(*_a, **_k):
        raise _Killed()

    monkeypatch.setattr(batches, "_store_created", _killed)


def _armed_rows() -> set[uuid.UUID]:
    return {row for scan in batches._RECONCILER.scans for row in scan.rows}


async def test_intent_row_committed_before_create(db_factory, fake):
    fake.create_error = RuntimeError("connection reset")
    ids = await _screens(db_factory, 2)
    batch_id = await _submit(db_factory, ids)

    batch = await _batch(db_factory, batch_id)
    assert batch.status == "submitting"
    assert batch.api_batch_id is None
    assert batch.create_failures == 1
    assert batch.row_table == "landscape_screens"
    assert batch.seq is not None
    async with db_factory() as db:
        items = (
            await db.scalars(
                select(LandscapeBatchItem).where(LandscapeBatchItem.batch_id == batch_id)
            )
        ).all()
    assert sorted(i.item_seq for i in items) == [1, 2]
    rows = await _rows(db_factory, ids)
    by_row = {i.row_id: i.id for i in items}
    for row_id in ids:
        assert rows[row_id].status == "submitting"
        assert rows[row_id].batch_item_id == by_row[row_id]


async def test_reconcile_adopts_orphan_older_than_24h(db_factory, fake, monkeypatch):
    ids = await _screens(db_factory, 2)
    _crash_after_create(monkeypatch)
    with pytest.raises(_Killed):
        await _submit(db_factory, ids)
    batch_id = (await _batch_of(db_factory, ids[0])).id
    async with db_factory() as db:
        await db.execute(
            update(LandscapeBatch)
            .where(LandscapeBatch.id == batch_id)
            .values(created_at=func.now() - text("interval '30 hours'"))
        )
        await db.commit()
    api_id = fake.last()
    fake.remote[api_id].created_at = datetime.now(UTC) - timedelta(hours=30)
    fake.end(api_id)

    _restart(monkeypatch)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 1
    assert await _statuses(db_factory, ids) == ["submitted", "submitted"]
    batch = await _batch(db_factory, batch_id)
    assert batch.api_batch_id == api_id
    assert batch.status == "submitted"
    assert fake.create_calls == 1


async def test_reconcile_returns_while_a_listed_batch_runs(db_factory, fake, monkeypatch):
    ids = await _screens(db_factory, 1)
    _crash_after_create(monkeypatch)
    with pytest.raises(_Killed):
        await _submit(db_factory, ids)
    api_id = fake.last()

    _restart(monkeypatch)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    assert await _statuses(db_factory, ids) == ["submitting"]
    assert fake.create_calls == 1

    fake.end(api_id)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 1
    assert await _statuses(db_factory, ids) == ["submitted"]
    assert fake.create_calls == 1


async def test_reconcile_list_is_fixed_at_startup(db_factory, fake, monkeypatch):
    ids = await _screens(db_factory, 1)
    _crash_after_create(monkeypatch)
    with pytest.raises(_Killed):
        await _submit(db_factory, ids)
    orphan = fake.last()
    fake.end(orphan)
    running = fake.foreign()

    _restart(monkeypatch)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    (scan,) = batches._RECONCILER.scans
    assert set(scan.listed) == {orphan, running}

    late = fake.foreign()  # made after the list was fixed, and still running
    fake.end(running)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 1
    assert late not in scan.listed
    assert fake.remote[late].processing_status == "in_progress"
    assert await _statuses(db_factory, ids) == ["submitted"]


async def test_disabled_reconcile_never_resubmits(db_factory, fake, monkeypatch):
    ids = await _screens(db_factory, 2)
    original_create = batches._create

    async def _killed(*_a, **_k):
        raise _Killed()

    monkeypatch.setattr(batches, "_create", _killed)
    with pytest.raises(_Killed):
        await _submit(db_factory, ids)
    monkeypatch.setattr(batches, "_create", original_create)
    batch_id = (await _batch_of(db_factory, ids[0])).id

    _restart(monkeypatch)
    for _ in range(2):
        assert await batches.reconcile_orphans(db_factory, may_submit=False) == 0
        assert await _statuses(db_factory, ids) == ["submitting", "submitting"]
        assert (await _batch(db_factory, batch_id)).status == "submitting"
    assert fake.create_calls == 0

    # Enabled again: no batch was found and this process holds no requests for the row, so
    # its rows go back to their stage's queue, and the row is closed.
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    assert await _statuses(db_factory, ids) == ["pending", "pending"]
    assert (await _batch(db_factory, batch_id)).status == "ingested"
    assert fake.create_calls == 0


async def test_failed_create_rearms_reconciliation(db_factory, fake):
    fake.create_error = RuntimeError("503")
    ids = await _screens(db_factory, 1)
    batch_id = await _submit(db_factory, ids)

    assert (await _batch(db_factory, batch_id)).create_failures == 1
    assert batch_id in _armed_rows()
    assert batch_id in batches._RECONCILER.requests

    # No batch exists, but this is only the first failure and submitting is off: held.
    assert await batches.reconcile_orphans(db_factory, may_submit=False) == 0
    assert await _statuses(db_factory, ids) == ["submitting"]
    assert fake.create_calls == 1
    assert batch_id in _armed_rows()


async def test_repeated_create_failure_terminates(db_factory, fake):
    fake.create_error = RuntimeError("503")
    ids = await _screens(db_factory, 2)
    batch_id = await _submit(db_factory, ids)
    await batches.reconcile_orphans(db_factory, may_submit=True)
    await batches.reconcile_orphans(db_factory, may_submit=True)
    assert fake.create_calls == 3
    assert await _statuses(db_factory, ids) == ["submitting", "submitting"]

    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    rows = await _rows(db_factory, ids)
    assert [rows[i].status for i in ids] == ["failed:create", "failed:create"]
    assert all(rows[i].error for i in ids)
    batch = await _batch(db_factory, batch_id)
    assert batch.create_failures == 3
    assert batch.status == "ingested"
    assert not _armed_rows()

    await batches.reconcile_orphans(db_factory, may_submit=True)
    assert fake.create_calls == 3


async def test_create_raised_but_batch_exists_is_adopted(db_factory, fake):
    fake.raise_after_create = RuntimeError("read timeout")
    ids = await _screens(db_factory, 2)
    batch_id = await _submit(db_factory, ids)
    api_id = fake.last()
    assert (await _batch(db_factory, batch_id)).create_failures == 1

    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    fake.end(api_id)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 1

    batch = await _batch(db_factory, batch_id)
    assert batch.api_batch_id == api_id
    assert batch.status == "submitted"
    assert await _statuses(db_factory, ids) == ["submitted", "submitted"]
    assert fake.create_calls == 1


async def test_poll_streams_in_chunks(db_factory, fake):
    ids = await _screens(db_factory, 5)
    batch_id = await _submit(db_factory, ids)
    fake.end(fake.last())

    chunks = await _drain(db_factory, chunk_size=2)
    assert [len(outcomes) for _, outcomes, _ in chunks] == [2, 2, 1]
    assert [last for _, _, last in chunks] == [False, False, True]
    assert {bid for bid, _, _ in chunks} == {batch_id}
    assert {o.row_id for _, outcomes, _ in chunks for o in outcomes} == set(ids)
    assert (await _batch(db_factory, batch_id)).status == "ended"


async def test_unmatched_row_resubmitted_once_and_items_held_until_then(db_factory, fake):
    fake.create_error = RuntimeError("503")
    ids = await _screens(db_factory, 2)
    batch_id = await _submit(db_factory, ids)
    fake.create_error = None
    running = fake.foreign()

    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    assert fake.create_calls == 1
    assert await _statuses(db_factory, ids) == ["submitting", "submitting"]

    fake.end(running)
    await batches.reconcile_orphans(db_factory, may_submit=True)
    assert fake.create_calls == 2
    assert await _statuses(db_factory, ids) == ["submitted", "submitted"]
    batch = await _batch(db_factory, batch_id)
    assert batch.status == "submitted"
    assert batch.api_batch_id == fake.last()

    await batches.reconcile_orphans(db_factory, may_submit=True)
    assert fake.create_calls == 2


async def test_crash_between_create_and_store(db_factory, fake, monkeypatch):
    ids = await _screens(db_factory, 3)
    _crash_after_create(monkeypatch)
    with pytest.raises(_Killed):
        await _submit(db_factory, ids)
    api_id = fake.last()

    _restart(monkeypatch)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0
    fake.end(api_id)
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 1
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0

    chunks = await _drain(db_factory)
    outcomes = [o for _, outs, _ in chunks for o in outs]
    assert len(outcomes) == 3
    assert all([await _apply_done(db_factory, o) for o in outcomes])
    await batches.mark_ingested(db_factory, chunks[-1][0])

    assert len(fake.remote) == 1
    assert fake.create_calls == 1
    assert await _statuses(db_factory, ids) == ["done", "done", "done"]
    batch = await _batch_of(db_factory, ids[0])
    assert batch.status == "ingested"
    async with db_factory() as db:
        calls = await db.scalar(
            select(func.count(LandscapeLlmCall.id)).where(LandscapeLlmCall.batch_id == batch.id)
        )
    assert calls == 3


async def test_ingestion_compare_and_set(db_factory, fake):
    ids = await _screens(db_factory, 2)
    batch_id = await _submit(db_factory, ids)
    fake.end(fake.last())
    chunks = await _drain(db_factory)
    outcomes = chunks[0][1]
    assert [await _apply_done(db_factory, o) for o in outcomes] == [True, True]

    # The chunk replayed after a crash, with its ledger rows: nothing changes.
    replay = await _drain(db_factory)
    assert [await _apply_done(db_factory, o) for o in replay[0][1]] == [False, False]
    assert await _statuses(db_factory, ids) == ["done", "done"]
    async with db_factory() as db:
        calls = await db.scalar(
            select(func.count(LandscapeLlmCall.id)).where(LandscapeLlmCall.batch_id == batch_id)
        )
    assert calls == 2

    # A late result for an item since resubmitted under a new batch item changes nothing.
    (late_id,) = await _screens(db_factory, 1)
    await _submit(db_factory, [late_id])
    first_api = fake.last()
    fake.end(first_api, [_line(fake.custom_ids(first_api)[0], kind="expired")])
    (old_outcome,) = [o for _, outs, _ in await _drain(db_factory) for o in outs
                      if o.row_id == late_id]
    assert await _apply_generic(db_factory, old_outcome)
    await _submit(db_factory, [late_id])
    rows = await _rows(db_factory, [late_id])
    assert rows[late_id].status == "submitted"
    assert rows[late_id].batch_item_id != old_outcome.batch_item_id
    assert not await _apply_done(db_factory, old_outcome)
    assert await _statuses(db_factory, [late_id]) == ["submitted"]


async def test_replayed_errored_or_refused_outcome_fires_once(db_factory, fake, monkeypatch):
    errored_id, refused_id = await _screens(db_factory, 2)
    await _submit(db_factory, [errored_id, refused_id])
    api_id = fake.last()
    first, second = fake.custom_ids(api_id)
    fake.end(api_id, [_line(first, kind="errored"), _line(second, stop_reason="refusal")])

    outcomes = (await _drain(db_factory))[0][1]
    assert sorted(o.kind for o in outcomes) == ["errored", "refusal"]
    assert [await _apply_generic(db_factory, o) for o in outcomes] == [True, True]
    assert [await _apply_generic(db_factory, o) for o in outcomes] == [False, False]

    rows = await _rows(db_factory, [errored_id, refused_id])
    assert rows[errored_id].status == "pending"
    assert rows[errored_id].attempts == 1
    assert rows[refused_id].status == "pending"
    assert rows[refused_id].fallback_from == MODEL

    calls = []

    async def fake_call_sync(stage, request, *, db_factory, item_ref, skip_models=frozenset(),
                             ledger_stage=None):
        calls.append((stage, skip_models))
        return CallResult(
            message=_message(), text="{}", stop_reason="end_turn", refusal_category=None,
            served_by="claude-opus-5", usage={}, error=None,
        )

    async def on_success(db, row, result):
        row.decision = "yes"

    async def build(db, row):
        return _request()

    monkeypatch.setattr(batches, "call_sync", fake_call_sync)
    adapter = batches.FallbackAdapter(
        stage="screen", row_model=LandscapeScreen, build=build, on_success=on_success,
    )
    assert await batches.run_fallbacks(db_factory, adapter) == 1
    assert await batches.run_fallbacks(db_factory, adapter) == 0
    assert calls == [("screen", frozenset({MODEL}))]
    rows = await _rows(db_factory, [errored_id, refused_id])
    assert rows[refused_id].status == "done"
    assert rows[refused_id].served_by_model == "claude-opus-5"
    assert rows[refused_id].decision == "yes"
    # The errored row is resubmitted by its stage, never by the fallback chain.
    assert rows[errored_id].status == "pending"


async def test_fallback_build_gets_a_session(db_factory, fake, monkeypatch):
    """``build`` reads the row's document in a session of its own, closed before the call."""
    [row_id] = await _screens(db_factory, 1)
    async with db_factory() as db:
        await db.execute(
            update(LandscapeScreen).where(LandscapeScreen.id == row_id)
            .values(fallback_from=MODEL)
        )
        await db.commit()
    built: list = []

    async def build(db, row):
        assert isinstance(db, AsyncSession)
        doc = await db.get(LandscapeDocument, row.document_id)
        built.append((row.id, doc.canonical_external_id))
        request = _request()
        request["messages"][0]["content"] = doc.canonical_external_id
        return request

    async def fake_call_sync(stage, request, *, db_factory, item_ref, skip_models=frozenset(),
                             ledger_stage=None):
        assert built, "the request was not built before the call"
        assert request["messages"][0]["content"] == built[0][1]
        return CallResult(
            message=_message(), text="{}", stop_reason="end_turn", refusal_category=None,
            served_by="claude-opus-5", usage={}, error=None,
        )

    async def on_success(db, row, result):
        row.decision = "yes"

    monkeypatch.setattr(batches, "call_sync", fake_call_sync)
    adapter = batches.FallbackAdapter(
        stage="screen", row_model=LandscapeScreen, build=build, on_success=on_success,
    )
    assert await batches.run_fallbacks(db_factory, adapter) == 1
    assert [row for row, _ in built] == [row_id]
    assert built[0][1].startswith("doi:10.9999/invented-")
    assert (await _rows(db_factory, [row_id]))[row_id].status == "done"


async def test_ingested_batch_not_read_again(db_factory, fake):
    ids = await _screens(db_factory, 2)
    batch_id = await _submit(db_factory, ids)
    fake.end(fake.last())
    assert len(await _drain(db_factory)) == 1
    await batches.mark_ingested(db_factory, batch_id)
    reads = fake.results_calls

    assert await _drain(db_factory) == []
    assert fake.results_calls == reads
    batch = await _batch(db_factory, batch_id)
    assert batch.status == "ingested"
    assert batch.ingested_at is not None


async def test_fallback_rows_never_batched(db_factory, fake):
    plain_id, waiting_id = await _screens(db_factory, 2)
    async with db_factory() as db:
        await db.execute(
            update(LandscapeScreen)
            .where(LandscapeScreen.id == waiting_id)
            .values(fallback_from=MODEL)
        )
        await db.commit()

    await _submit(db_factory, [plain_id, waiting_id])
    assert len(fake.custom_ids(fake.last())) == 1
    rows = await _rows(db_factory, [plain_id, waiting_id])
    assert rows[plain_id].status == "submitted"
    assert rows[waiting_id].status == "pending"
    assert rows[waiting_id].batch_item_id is None

    with pytest.raises(ValueError):
        await _submit(db_factory, [waiting_id])
    assert fake.create_calls == 1


async def test_results_out_of_order_keyed_by_custom_id(db_factory, fake):
    ids = await _screens(db_factory, 3)
    batch_id = await _submit(db_factory, ids)
    api_id = fake.last()
    cids = fake.custom_ids(api_id)
    fake.end(api_id, [
        _line(cids[2], stop_reason="refusal"),
        _line(cids[0]),
        _line(cids[1], kind="errored"),
    ])

    outcomes = (await _drain(db_factory))[0][1]
    kinds = {o.row_id: o.kind for o in outcomes}
    assert kinds == {ids[0]: "succeeded", ids[1]: "errored", ids[2]: "refusal"}
    assert {o.row_id: o.item_seq for o in outcomes} == {ids[0]: 1, ids[1]: 2, ids[2]: 3}
    async with db_factory() as db:
        items = (
            await db.scalars(
                select(LandscapeBatchItem).where(LandscapeBatchItem.batch_id == batch_id)
            )
        ).all()
    assert {i.row_id: i.result_kind for i in items} == kinds


async def test_usage_recorded_with_batch_true(db_factory, fake):
    (row_id,) = await _screens(db_factory, 1)
    batch_id = await _submit(db_factory, [row_id])
    api_id = fake.last()
    (cid,) = fake.custom_ids(api_id)
    fake.end(api_id, [_line(cid, input_tokens=1000, output_tokens=200)])
    await _drain(db_factory)

    async with db_factory() as db:
        (call,) = (
            await db.scalars(
                select(LandscapeLlmCall).where(LandscapeLlmCall.batch_id == batch_id)
            )
        ).all()
    assert call.batch is True
    assert call.batch_id == batch_id
    assert call.item_seq == 1
    assert call.custom_id == cid
    assert call.stage == "screen"
    assert call.model == MODEL
    assert call.input_tokens == 1000
    assert call.output_tokens == 200
    assert call.cost_usd == cost_for_request(
        MODEL, input_tokens=1000, output_tokens=200, cache_read=0, cache_creation=0, batch=True
    )


async def test_fallback_ledger_stage_passed_to_call_sync(db_factory, fake, monkeypatch):
    """An adapter's ``ledger_stage`` reaches ``call_sync``, so the reject audit's re-runs are
    ledgered as ``audit`` while built by the audited stage (D66); unset, it stays None and
    ``call_sync`` ledgers under ``stage``."""
    ids = await _screens(db_factory, 2)
    seen: list[tuple[str, str | None]] = []

    async def fake_call_sync(stage, request, *, db_factory, item_ref, skip_models=frozenset(),
                             ledger_stage=None):
        seen.append((stage, ledger_stage))
        return CallResult(
            message=_message(), text="{}", stop_reason="end_turn", refusal_category=None,
            served_by="claude-opus-5", usage={}, error=None,
        )

    async def build(db, row):
        return _request()

    async def on_success(db, row, result):
        row.decision = "yes"

    monkeypatch.setattr(batches, "call_sync", fake_call_sync)
    for row_id, ledger_stage in zip(ids, (None, "audit"), strict=True):
        async with db_factory() as db:
            await db.execute(
                update(LandscapeScreen).where(LandscapeScreen.id == row_id)
                .values(fallback_from=MODEL)
            )
            await db.commit()
        adapter = batches.FallbackAdapter(
            stage="screen", row_model=LandscapeScreen, build=build, on_success=on_success,
            **({} if ledger_stage is None else {"ledger_stage": ledger_stage}),
        )
        assert await batches.run_fallbacks(db_factory, adapter) == 1

    assert seen == [("screen", None), ("screen", "audit")]


# --- sync mode (landscape_batch_mode = "sync"; the Batches endpoint refused, 2026-11-16) ----


@pytest.fixture
def sync_mode(monkeypatch, fake):
    """Sync mode with a fake Messages call; each request's user message is the stop reason to
    answer with, or "raise". Returns the requests sent, in order."""
    monkeypatch.setattr(get_settings(), "landscape_batch_mode", "sync")
    # One at a time: every session of a test shares one connection (savepoints).
    monkeypatch.setattr(get_settings(), "landscape_sync_concurrency", 1)
    sent: list[dict] = []

    async def abeta_create(client, **request):
        sent.append(request)
        stop = request["messages"][0]["content"]
        if stop == "raise":
            raise RuntimeError("invented API failure")
        if stop == "ratelimit":
            raise anthropic.RateLimitError(
                "invented rate limit",
                response=httpx.Response(429, request=httpx.Request("POST", "https://invalid.test")),
                body=None,
            )
        return _message(stop)

    monkeypatch.setattr(batches.llm, "abeta_create", abeta_create)
    monkeypatch.setattr(batches.llm, "get_anthropic_client", lambda: object())
    return sent


async def _submit_stops(db_factory, ids: list[uuid.UUID], stops: list[str]) -> uuid.UUID:
    return await batches.submit(
        db_factory, stage="screen", row_model=LandscapeScreen,
        items=[
            (row_id, {**_request(), "messages": [{"role": "user", "content": stop}]})
            for row_id, stop in zip(ids, stops, strict=True)
        ],
    )


async def _drain_local(db_factory, *, run: bool = True, should_stop=None) -> list:
    """Every chunk one poll yields, ingesting none of them."""
    async def may_run():
        return run

    return [
        chunk async for chunk in batches.poll(db_factory, may_run=may_run, should_stop=should_stop)
    ]


async def _ingest_all(db_factory, outcomes) -> None:
    for outcome in outcomes:
        apply = _apply_done if outcome.kind == "succeeded" else _apply_generic
        assert await apply(db_factory, outcome)


async def test_sync_mode_runs_items_in_poll_at_the_full_rate(db_factory, fake, sync_mode):
    ids = await _screens(db_factory, 4)
    batch_id = await _submit_stops(db_factory, ids, ["end_turn", "refusal", "max_tokens", "raise"])
    batch = await _batch(db_factory, batch_id)
    assert fake.create_calls == 0
    assert sync_mode == []
    assert batch.api_batch_id == f"local:{batch_id}"
    assert batch.status == "submitted"
    assert await _statuses(db_factory, ids) == ["submitted"] * 4

    [(got, outcomes, is_last)] = await _drain_local(db_factory)
    assert got == batch_id and is_last
    assert [o.kind for o in outcomes] == ["succeeded", "refusal", "truncated", "errored"]
    assert [o.row_id for o in outcomes] == ids
    assert len(sync_mode) == 4
    assert (await _batch(db_factory, batch_id)).status == "ended"
    async with db_factory() as db:
        ledger = (await db.scalars(
            select(LandscapeLlmCall).where(LandscapeLlmCall.batch_id == batch_id)
            .order_by(LandscapeLlmCall.item_seq)
        )).all()
        kinds = (await db.scalars(
            select(LandscapeBatchItem.result_kind).where(LandscapeBatchItem.batch_id == batch_id)
            .order_by(LandscapeBatchItem.item_seq)
        )).all()
    assert [(c.item_seq, c.batch) for c in ledger] == [(1, False), (2, False), (3, False), (4, False)]
    assert ledger[0].cost_usd == cost_for_request(
        MODEL, input_tokens=100, output_tokens=20, cache_read=0, cache_creation=0, batch=False
    )
    assert kinds == ["succeeded", "refusal", "truncated", "errored"]

    await _ingest_all(db_factory, outcomes)
    await batches.mark_ingested(db_factory, batch_id)
    assert (await _batch(db_factory, batch_id)).status == "ingested"
    assert await _drain_local(db_factory) == []
    assert len(sync_mode) == 4


async def test_sync_mode_sends_nothing_for_a_row_that_left_the_batch(
    db_factory, fake, sync_mode,
):
    """A row reset and batched again since submission is answered ``canceled`` with no call
    and no ledger row (D78): the compare-and-set would discard its answer."""
    ids = await _screens(db_factory, 3)
    first = await _submit_stops(db_factory, ids, ["end_turn"] * 3)
    async with db_factory() as db:
        await db.execute(
            update(LandscapeScreen).where(LandscapeScreen.id == ids[1]).values(status="pending")
        )
        await db.commit()
    second = await _submit_stops(db_factory, [ids[1]], ["end_turn"])

    chunks = await _drain_local(db_factory)

    by_batch = {got: outcomes for got, outcomes, _ in chunks}
    assert [o.kind for o in by_batch[first]] == ["succeeded", "canceled", "succeeded"]
    assert [o.kind for o in by_batch[second]] == ["succeeded"]
    assert len(sync_mode) == 3
    async with db_factory() as db:
        ledgered = await db.scalar(
            select(func.count()).select_from(LandscapeLlmCall)
            .where(LandscapeLlmCall.batch_id == first)
        )
        kinds = (await db.scalars(
            select(LandscapeBatchItem.result_kind).where(LandscapeBatchItem.batch_id == first)
            .order_by(LandscapeBatchItem.item_seq)
        )).all()
    assert ledgered == 2
    assert kinds == ["succeeded", "canceled", "succeeded"]
    assert not await _apply_generic(db_factory, by_batch[first][1])
    assert (await _rows(db_factory, ids))[ids[1]].status == "submitted"


async def test_sync_mode_calls_nothing_unless_allowed(db_factory, fake, sync_mode):
    ids = await _screens(db_factory, 2)
    batch_id = await _submit_stops(db_factory, ids, ["end_turn", "end_turn"])

    assert await _drain_local(db_factory, run=False) == []
    assert await _drain_local(db_factory, should_stop=lambda: True) == []
    assert sync_mode == []
    assert (await _batch(db_factory, batch_id)).status == "submitted"
    assert await _statuses(db_factory, ids) == ["submitted", "submitted"]


async def test_sync_mode_replays_answers_without_calling_again(db_factory, fake, sync_mode):
    """A chunk whose ingestion failed is read again from memory, as an API batch's results
    are re-read: nothing is paid for twice."""
    ids = await _screens(db_factory, 2)
    batch_id = await _submit_stops(db_factory, ids, ["end_turn", "refusal"])
    [(_, first, _)] = await _drain_local(db_factory)
    [(_, again, is_last)] = await _drain_local(db_factory, run=False)

    assert is_last
    assert [(o.item_seq, o.kind) for o in again] == [(o.item_seq, o.kind) for o in first]
    assert len(sync_mode) == 2
    async with db_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(LandscapeLlmCall)
            .where(LandscapeLlmCall.batch_id == batch_id)
        ) == 2


async def test_sync_mode_stops_between_chunks_and_a_restart_releases_the_rest(
    db_factory, fake, sync_mode, monkeypatch
):
    monkeypatch.setattr(batches, "_LOCAL_CHUNK", 2)
    ids = await _screens(db_factory, 4)
    batch_id = await _submit_stops(db_factory, ids, ["end_turn"] * 4)

    chunks = await _drain_local(db_factory, should_stop=lambda: len(sync_mode) >= 2)
    assert [(len(outcomes), is_last) for _, outcomes, is_last in chunks] == [(2, False)]
    await _ingest_all(db_factory, chunks[0][1])

    _restart(monkeypatch)
    assert await _drain_local(db_factory) == []
    assert len(sync_mode) == 2
    assert await _statuses(db_factory, ids) == ["done", "done", "pending", "pending"]
    assert (await _batch(db_factory, batch_id)).status == "ingested"


async def test_sync_mode_reconcile_releases_unconfirmed_rows_without_listing(
    db_factory, fake, monkeypatch
):
    """The Batches endpoint refused the create (HIPAA-configured organization); in sync mode
    the row is released at once, and the API is never listed."""
    ids = await _screens(db_factory, 2)
    fake.create_error = RuntimeError(
        "this endpoint is not available for HIPAA-configured organizations"
    )
    batch_id = await _submit(db_factory, ids)
    assert (await _batch(db_factory, batch_id)).create_failures == 1

    def no_listing(**_kw):
        raise AssertionError("sync mode must not list API batches")

    monkeypatch.setattr(fake, "list", no_listing)
    monkeypatch.setattr(get_settings(), "landscape_batch_mode", "sync")
    assert await batches.reconcile_orphans(db_factory, may_submit=True) == 0

    assert await _statuses(db_factory, ids) == ["pending", "pending"]
    assert (await _batch(db_factory, batch_id)).status == "ingested"
    assert fake.create_calls == 1


async def _ledger_count(db_factory, batch_id: uuid.UUID) -> int:
    async with db_factory() as db:
        return await db.scalar(
            select(func.count()).select_from(LandscapeLlmCall)
            .where(LandscapeLlmCall.batch_id == batch_id)
        )


async def test_sync_mode_a_stop_ends_new_calls_and_each_call_is_ledgered_at_once(
    db_factory, fake, sync_mode, monkeypatch
):
    """A shutdown inside a chunk starts no further call, and every call already answered has
    its ledger row and is yielded (review finding: nothing paid may go unrecorded)."""
    ids = await _screens(db_factory, 3)
    batch_id = await _submit_stops(db_factory, ids, ["end_turn"] * 3)

    chunks = await _drain_local(db_factory, should_stop=lambda: len(sync_mode) >= 1)
    assert [([o.item_seq for o in outcomes], is_last) for _, outcomes, is_last in chunks] == [
        ([1], False)
    ]
    assert len(sync_mode) == 1
    assert await _ledger_count(db_factory, batch_id) == 1


async def test_sync_mode_rate_limit_leaves_the_rest_for_the_next_poll(
    db_factory, fake, sync_mode, monkeypatch
):
    """A 429 does not become an ``errored`` outcome (which would use up the row's retry): the
    item stays unanswered, nothing is ledgered for it, and no further call starts."""
    ids = await _screens(db_factory, 3)
    batch_id = await _submit_stops(db_factory, ids, ["end_turn", "ratelimit", "end_turn"])

    chunks = await _drain_local(db_factory)
    assert [([o.item_seq for o in outcomes], is_last) for _, outcomes, is_last in chunks] == [
        ([1], False)
    ]
    assert len(sync_mode) == 2
    assert await _ledger_count(db_factory, batch_id) == 1
    assert await _statuses(db_factory, ids) == ["submitted"] * 3


async def test_sync_mode_rereads_may_run_before_each_chunk(
    db_factory, fake, sync_mode, monkeypatch
):
    """A pause, kill switch or live simulation that starts while a local batch runs stops it
    at the next chunk (review finding)."""
    monkeypatch.setattr(batches, "_LOCAL_CHUNK", 2)
    ids = await _screens(db_factory, 4)
    await _submit_stops(db_factory, ids, ["end_turn"] * 4)
    allowed = iter([True, False])

    async def may_run():
        return next(allowed)

    async for _, outcomes, _ in batches.poll(db_factory, may_run=may_run):
        await _ingest_all(db_factory, outcomes)

    assert len(sync_mode) == 2
    assert await _statuses(db_factory, ids) == ["done", "done", "submitted", "submitted"]


# --- lanes (D75): each stage's local batches beside the other stages' ---------------------------


def _one_session_at_a_time(db_factory):
    """``db_factory`` with one session open at a time: every session of a test shares one
    connection, which takes one statement at a time, while lanes overlap their calls."""
    lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def factory():
        async with lock, db_factory() as session:
            yield session

    return factory


async def test_sync_mode_runs_each_stage_beside_the_others(
    db_factory, fake, sync_mode, monkeypatch
):
    """The screening batch comes first in ``seq``, and its call waits for the extraction
    batch's call to start: run one batch at a time, it would time out and be errored."""
    extract_started = asyncio.Event()

    async def abeta_create(client, **request):
        if request["messages"][0]["content"] == "screen":
            await asyncio.wait_for(extract_started.wait(), timeout=5)
        else:
            extract_started.set()
        return _message("end_turn")

    monkeypatch.setattr(batches.llm, "abeta_create", abeta_create)
    factory = _one_session_at_a_time(db_factory)
    first, second = await _screens(db_factory, 2)
    screen_id = await batches.submit(
        factory, stage="screen", row_model=LandscapeScreen,
        items=[(first, {**_request(), "messages": [{"role": "user", "content": "screen"}]})],
    )
    extract_id = await batches.submit(
        factory, stage="extract", row_model=LandscapeScreen,
        items=[(second, {**_request(), "messages": [{"role": "user", "content": "extract"}]})],
    )

    chunks = await _drain_local(factory)

    assert {batch_id: [o.kind for o in outcomes] for batch_id, outcomes, _ in chunks} == {
        screen_id: ["succeeded"], extract_id: ["succeeded"],
    }
    assert all(is_last for _, _, is_last in chunks)


async def test_sync_mode_does_not_replay_a_chunk_already_ingested(
    db_factory, fake, sync_mode, monkeypatch
):
    monkeypatch.setattr(batches, "_LOCAL_CHUNK", 2)
    ids = await _screens(db_factory, 4)
    await _submit_stops(db_factory, ids, ["end_turn"] * 4)
    [(_, first, _)] = await _drain_local(db_factory, should_stop=lambda: len(sync_mode) >= 2)

    # not ingested yet: read again from memory
    [(_, again, is_last)] = await _drain_local(db_factory, run=False)
    assert [o.item_seq for o in again] == [1, 2] and not is_last
    await _ingest_all(db_factory, first)

    chunks = await _drain_local(db_factory)
    assert [([o.item_seq for o in outcomes], is_last) for _, outcomes, is_last in chunks] == [
        ([3, 4], True)
    ]
    assert len(sync_mode) == 4


async def test_a_failing_lane_stops_alone(db_factory, fake, sync_mode, monkeypatch):
    """An error in one lane is raised once the other lanes have ended."""
    factory = _one_session_at_a_time(db_factory)
    first, second = await _screens(db_factory, 2)
    await batches.submit(
        factory, stage="screen", row_model=LandscapeScreen,
        items=[(first, {**_request(), "messages": [{"role": "user", "content": "end_turn"}]})],
    )
    extract_id = await batches.submit(
        factory, stage="extract", row_model=LandscapeScreen,
        items=[(second, {**_request(), "messages": [{"role": "user", "content": "end_turn"}]})],
    )
    real_run = batches._run_local

    async def run_local(db_factory_, batch, **kwargs):
        if batch.stage == "screen":
            raise RuntimeError("invented lane failure")
        async for chunk in real_run(db_factory_, batch, **kwargs):
            yield chunk

    monkeypatch.setattr(batches, "_run_local", run_local)
    seen = []

    async def may_run():
        return True

    with pytest.raises(RuntimeError, match="invented lane failure"):
        async for chunk in batches.poll(factory, may_run=may_run):
            seen.append(chunk)
    assert [(batch_id, is_last) for batch_id, _, is_last in seen] == [(extract_id, True)]
