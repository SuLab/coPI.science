"""The reject audit and staff rescue against Postgres (plan Task 6.2; spec §10.5, §10.4; L29,
L43, L44, D66).

The Message Batches API is a fake ``messages.batches`` resource whose batches end at once, each
result chosen from the request; the real ``batches.submit`` and ``batches.poll`` run against it.
Every session shares one connection whose outer transaction rolls back. Each epoch stores a
snapshot of the judging inputs (spec §7.11). Every name and identifier is invented.
"""
# ``FakeBatches`` defines a method named ``list``; postponed annotations keep ``list[dict]``
# below from resolving to it.
from __future__ import annotations

import contextlib
import itertools
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import batches, comparator, epochs
from src.landscaping.eval import reject_audit
from src.landscaping.rescue import RescueConflict, carry_rescues, rescue
from src.landscaping.stages import cut as cut_stage
from src.landscaping.stages import extract
from src.landscaping.stages.cut import run_cut
from src.models.landscape import (
    LandscapeBatch,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeLlmCall,
    LandscapeRegressionItem,
    LandscapeRegressionSet,
    LandscapeRejectAudit,
    LandscapeRescue,
    LandscapeScreen,
    LandscapeTriage,
)
from tests import factories

pytestmark = pytest.mark.integration

_counter = itertools.count()
DISAGREE = "invented-disagreement"


# --- the fake API ---------------------------------------------------------------------------


def _message(text: str, *, model: str) -> SimpleNamespace:
    return SimpleNamespace(
        model=model, stop_reason="end_turn", stop_details=None,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def _audit_answer(params: dict) -> SimpleNamespace:
    """A screen request (effort ``low``) answers ``yes`` for a document marked ``DISAGREE``,
    else ``no``; a triage request passes a dossier marked ``DISAGREE``, else fails a gate."""
    user = params["messages"][0]["content"]
    disagree = DISAGREE in user
    if params["output_config"]["effort"] == "low":
        payload = {
            "in_scope": "yes" if disagree else "no", "research_kind": [],
            "targets_mentioned": [], "diseases_mentioned": [], "modality_mentioned": None,
            "reason": "An invented audit reason.",
        }
    else:
        payload = {
            "gating": {"invented_gate": "met" if disagree else "not_met"},
            "in_therapeutic_scope": True,
        }
    return SimpleNamespace(
        type="succeeded", message=_message(json.dumps(payload), model=params["model"])
    )


class FakeBatches:
    """``client.messages.batches``: every batch has ended by the time it is retrieved, and
    ``reply(params)`` gives each request's result."""

    def __init__(self) -> None:
        self.reply = _audit_answer
        self.remote: dict[str, SimpleNamespace] = {}
        self.requests: dict[str, list[dict]] = {}

    def create(self, *, requests):
        api_id = f"msgbatch_audit_{len(self.remote) + 1:04d}"
        now = datetime.now(UTC)
        self.remote[api_id] = SimpleNamespace(
            id=api_id, created_at=now, processing_status="ended", ended_at=now
        )
        self.requests[api_id] = list(requests)
        return self.remote[api_id]

    def retrieve(self, api_id):
        return self.remote[api_id]

    def list(self, *, limit=20):
        return sorted(self.remote.values(), key=lambda b: b.created_at, reverse=True)

    def results(self, api_id):
        return iter([
            SimpleNamespace(custom_id=r["custom_id"], result=self.reply(r["params"]))
            for r in self.requests[api_id]
        ])

    def params(self) -> list[dict]:
        return [r["params"] for requests in self.requests.values() for r in requests]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(batches, "_RECONCILER", batches._Reconciler())
    # Distinct diseases never fold in these tests; no Mondo lookup is made.
    monkeypatch.setattr(cut_stage, "is_ancestor", lambda a, b: False)


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


# --- builders -------------------------------------------------------------------------------


async def _epoch(db_factory, status: str = "candidate") -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(status=status, snapshot=epochs.build_snapshot())
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _actor(db_factory):
    async with db_factory() as db:
        user = await factories.make_user(db, name="Invented Staffer", user_role="manager")
        await db.commit()
        return user


async def _doc(db_factory, *, title: str = "Invented study") -> uuid.UUID:
    n = next(_counter)
    async with db_factory() as db:
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"invented:audit-{n}-{uuid.uuid4().hex}",
            title=f"{title} {n}", summary_text=f"An invented summary {n}.",
            summary_hash=uuid.uuid4().hex, s1_status="eligible",
        )
        db.add(doc)
        await db.commit()
        return doc.id


async def _screen(
    db_factory, epoch_id: uuid.UUID, doc_id: uuid.UUID, *, status: str = "excluded:screen"
) -> uuid.UUID:
    async with db_factory() as db:
        row = LandscapeScreen(
            document_id=doc_id, epoch_id=epoch_id, status=status,
            decision="no" if status == "excluded:screen" else "yes",
        )
        db.add(row)
        await db.commit()
        return row.id


async def _idea(
    db_factory, epoch_id: uuid.UUID, *, key: str | None = None, version: str = "v1",
    triage_status: str | None = "excluded:gate", score: float = 3.0, dossier: str = "",
) -> tuple[uuid.UUID, uuid.UUID | None]:
    """An idea with one qualifying document and, unless ``triage_status`` is None, an S4 row;
    ``(idea id, triage row id)``."""
    n = next(_counter)
    async with db_factory() as db:
        idea = LandscapeIdea(
            epoch_id=epoch_id,
            idea_key=key or f"invented-audit-idea-{n}-{uuid.uuid4().hex[:8]}",
            idea_type="biology", version_hash=version, direction="inhibit",
            targets=[{"hgnc_id": f"HGNC:{91000 + n}"}],
            disease={"mondo_id": f"MONDO:{9100000 + n}"},
            jhu_group_count=1,
        )
        db.add(idea)
        await db.flush()
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"invented:audit-idea-{n}-{uuid.uuid4().hex}",
            s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        extraction = LandscapeExtraction(
            document_id=doc.id, epoch_id=epoch_id, basis="summary", status="done"
        )
        db.add(extraction)
        await db.flush()
        db.add(LandscapeIdeaEvidence(
            idea_id=idea.id, extraction_id=extraction.id, candidate_index=0
        ))
        triage_id = None
        if triage_status is not None:
            row = LandscapeTriage(
                idea_id=idea.id, idea_version=version, epoch_id=epoch_id, status=triage_status,
                triage_score=score, evidence_count=3,
                raw={"dossier": f"Invented blinded dossier {n}. {dossier}"},
            )
            db.add(row)
            await db.flush()
            triage_id = row.id
        await db.commit()
        return idea.id, triage_id


async def _get(db_factory, model, row_id):
    async with db_factory() as db:
        return await db.get(model, row_id)


async def _count(db_factory, model, *where) -> int:
    async with db_factory() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where))


async def _audits(db_factory, epoch_id) -> list[LandscapeRejectAudit]:
    async with db_factory() as db:
        return list((await db.scalars(
            select(LandscapeRejectAudit).where(LandscapeRejectAudit.epoch_id == epoch_id)
        )).all())


async def _ingest_all(db_factory) -> None:
    async for batch_id, chunk, is_last in batches.poll(db_factory):
        await reject_audit.ingest_audit_outcomes(db_factory, chunk)
        if is_last:
            await batches.mark_ingested(db_factory, batch_id)


# --- the reject audit -----------------------------------------------------------------------


async def test_sample_size_respected(db_factory, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "landscape_reject_audit_size", 3)
    epoch_id = await _epoch(db_factory)
    rejected_screens = {
        await _screen(db_factory, epoch_id, await _doc(db_factory)) for _ in range(5)
    }
    passed_screen = await _screen(db_factory, epoch_id, await _doc(db_factory), status="done")
    rejected_triage = {(await _idea(db_factory, epoch_id))[1] for _ in range(4)}
    _, passed_triage = await _idea(db_factory, epoch_id, triage_status="done")

    assert await reject_audit.submit_reject_audit(db_factory, epoch_id=epoch_id) == 6

    audits = await _audits(db_factory, epoch_id)
    by_kind = {kind: {a.item_id for a in audits if a.item_kind == kind}
               for kind in ("screen", "triage")}
    assert len(by_kind["screen"]) == 3 and by_kind["screen"] <= rejected_screens
    assert len(by_kind["triage"]) == 3 and by_kind["triage"] <= rejected_triage
    assert passed_screen not in by_kind["screen"] and passed_triage not in by_kind["triage"]
    assert all(a.status == "submitted" for a in audits)

    # One Message Batch at ledger stage ``audit``, each request built by the audited stage's
    # builder (its effort) on the audit model (D66).
    async with db_factory() as db:
        (batch,) = (await db.scalars(select(LandscapeBatch))).all()
    assert batch.stage == "audit" and batch.request_count == 6
    params = fake.params()
    assert {p["model"] for p in params} == {get_settings().landscape_audit_model}
    assert sorted(p["output_config"]["effort"] for p in params) == ["low"] * 3 + ["medium"] * 3
    assert all("temperature" not in p for p in params)

    # Nothing was rejected since: the next cycle's audit samples nothing new.
    assert await reject_audit.submit_reject_audit(db_factory, epoch_id=epoch_id) == 0
    assert len(await _audits(db_factory, epoch_id)) == 6


async def test_disagreement_recorded(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    screen_agree = await _screen(db_factory, epoch_id, await _doc(db_factory))
    screen_disagree = await _screen(
        db_factory, epoch_id, await _doc(db_factory, title=f"Invented {DISAGREE} study")
    )
    _, triage_agree = await _idea(db_factory, epoch_id)
    _, triage_disagree = await _idea(db_factory, epoch_id, dossier=DISAGREE)

    assert await reject_audit.submit_reject_audit(db_factory, epoch_id=epoch_id) == 4
    await _ingest_all(db_factory)

    audits = {a.item_id: a for a in await _audits(db_factory, epoch_id)}
    assert all(a.status == "done" for a in audits.values())
    assert (audits[screen_agree].original_decision, audits[screen_agree].audit_decision,
            audits[screen_agree].agree) == ("no", "no", True)
    assert (audits[screen_disagree].original_decision, audits[screen_disagree].audit_decision,
            audits[screen_disagree].agree) == ("no", "yes", False)
    assert (audits[triage_agree].original_decision, audits[triage_agree].audit_decision,
            audits[triage_agree].agree) == ("excluded:gate", "excluded:gate", True)
    assert (audits[triage_disagree].audit_decision, audits[triage_disagree].agree) == (
        "pass", False
    )
    # The audited rows themselves are untouched.
    assert (await _get(db_factory, LandscapeScreen, screen_disagree)).status == "excluded:screen"
    # Every audit call is ledgered as ``audit``, and so are its fallback re-runs (D66).
    assert await _count(db_factory, LandscapeLlmCall, LandscapeLlmCall.stage == "audit") == 4
    assert {adapter.ledger_stage for adapter in reject_audit.FALLBACKS} == {"audit"}


# --- rescue ---------------------------------------------------------------------------------


async def test_rescue_reenters_next_stage_not_cut(db_factory, fake):
    epoch_id = await _epoch(db_factory)
    actor = await _actor(db_factory)

    # S2: a rescued document goes to S3.
    doc_id = await _doc(db_factory)
    screen_id = await _screen(db_factory, epoch_id, doc_id)
    async with db_factory() as db:
        await rescue(
            db, item_kind="screen", item_id=screen_id, stage="screen",
            expected_status="excluded:screen", actor=actor, reason="Clearly a target paper.",
        )
    assert (await _get(db_factory, LandscapeScreen, screen_id)).status == "rescued"
    assert await extract.submit_extract_batch(db_factory, epoch_id) == 1, (
        "a rescued S2 document must reach S3"
    )
    assert await _count(
        db_factory, LandscapeExtraction,
        LandscapeExtraction.document_id == doc_id, LandscapeExtraction.epoch_id == epoch_id,
    ) == 1

    # S4: a rescued idea joins the S5 pool and competes there; the rescue gives it no slot.
    kept, _ = await _idea(db_factory, epoch_id, triage_status="done", score=4.0)
    rescued, triage_id = await _idea(db_factory, epoch_id, score=2.0)
    async with db_factory() as db:
        await rescue(
            db, item_kind="triage", item_id=triage_id, stage="triage",
            expected_status="excluded:gate", actor=actor, reason="The gate was misread.",
        )
    assert await _count(db_factory, LandscapeCut, LandscapeCut.epoch_id == epoch_id) == 0
    assert not (await _get(db_factory, LandscapeIdea, rescued)).in_current_cut

    cut = await _get(db_factory, LandscapeCut, await run_cut(db_factory, epoch_id))
    assert cut.ordered_idea_ids == [str(kept), str(rescued)]
    assert cut.k == 1 and str(rescued) not in cut.ordered_idea_ids[:cut.k]


async def test_rescue_requires_reason(db_factory):
    epoch_id = await _epoch(db_factory)
    actor = await _actor(db_factory)
    screen_id = await _screen(db_factory, epoch_id, await _doc(db_factory))

    for reason in ("", "   "):
        async with db_factory() as db:
            with pytest.raises(ValueError, match="reason"):
                await rescue(
                    db, item_kind="screen", item_id=screen_id, stage="screen",
                    expected_status="excluded:screen", actor=actor, reason=reason,
                )

    assert (await _get(db_factory, LandscapeScreen, screen_id)).status == "excluded:screen"
    assert await _count(db_factory, LandscapeRescue) == 0


async def test_rescue_added_to_regression(db_factory):
    # Imported here: the regression set is Task 6.1's, and only this test reads it.
    from src.landscaping.eval import regression

    epoch_id = await _epoch(db_factory, status="active")
    actor = await _actor(db_factory)
    screen_id = await _screen(db_factory, epoch_id, await _doc(db_factory))
    async with db_factory() as db:
        await rescue(
            db, item_kind="screen", item_id=screen_id, stage="screen",
            expected_status="excluded:screen", actor=actor, reason="An invented reason.",
        )

    (recorded,) = await _rescues(db_factory)
    assert (recorded.item_kind, recorded.item_id, recorded.stage, recorded.epoch_id) == (
        "screen", screen_id, "screen", epoch_id
    )
    assert (recorded.actor_id, recorded.actor_name) == (actor.id, actor.name)
    assert recorded.reason == "An invented reason." and recorded.carried is False

    await regression.freeze_regression_set(db_factory, reason="after an invented rescue")
    async with db_factory() as db:
        version = await db.scalar(select(func.max(LandscapeRegressionSet.version)))
        items = (await db.scalars(
            select(LandscapeRegressionItem).where(
                LandscapeRegressionItem.set_version == version,
                LandscapeRegressionItem.origin == "rescue",
            )
        )).all()
    assert any(str(screen_id) in json.dumps(item.item_ref) for item in items)


async def test_rescue_compare_and_set_refuses_stale_status(db_factory):
    epoch_id = await _epoch(db_factory)
    first, second = await _actor(db_factory), await _actor(db_factory)
    screen_id = await _screen(db_factory, epoch_id, await _doc(db_factory))
    kwargs = {
        "item_kind": "screen", "item_id": screen_id, "stage": "screen",
        "expected_status": "excluded:screen", "reason": "Seen as excluded.",
    }
    async with db_factory() as db:
        await rescue(db, actor=first, **kwargs)
    async with db_factory() as db:
        with pytest.raises(RescueConflict):
            await rescue(db, actor=second, **kwargs)

    (recorded,) = await _rescues(db_factory)
    assert recorded.actor_id == first.id
    assert (await _get(db_factory, LandscapeScreen, screen_id)).status == "rescued"


async def test_deep_gate_rescue_reaches_s7(db_factory):
    epoch_id = await _epoch(db_factory)
    actor = await _actor(db_factory)
    idea_id, _ = await _idea(db_factory, epoch_id, triage_status="done")
    async with db_factory() as db:
        deep = LandscapeDeep(
            idea_id=idea_id, idea_version="v1", epoch_id=epoch_id, status="excluded:deep_gate"
        )
        db.add(deep)
        await db.commit()

    async def usable() -> dict:
        async with db_factory() as db:
            return await comparator.usable_versions(
                db, epoch_id=epoch_id, ids=[idea_id], basis="deep"
            )

    assert await usable() == {}
    async with db_factory() as db:
        await rescue(
            db, item_kind="deep", item_id=deep.id, stage="deep_gate",
            expected_status="excluded:deep_gate", actor=actor, reason="The gate was misread.",
        )
    assert await usable() == {idea_id: "v1"}


async def test_rescues_carried_into_new_epoch(db_factory):
    old_epoch = await _epoch(db_factory)
    actor = await _actor(db_factory)
    doc_id = await _doc(db_factory)
    old_screen = await _screen(db_factory, old_epoch, doc_id)
    _, old_triage = await _idea(db_factory, old_epoch, key="invented-carried-idea")
    async with db_factory() as db:
        await rescue(
            db, item_kind="screen", item_id=old_screen, stage="screen",
            expected_status="excluded:screen", actor=actor, reason="Screen reason.",
        )
    async with db_factory() as db:
        await rescue(
            db, item_kind="triage", item_id=old_triage, stage="triage",
            expected_status="excluded:gate", actor=actor, reason="Triage reason.",
        )

    new_epoch = await _epoch(db_factory)
    new_screen = await _screen(db_factory, new_epoch, doc_id)
    _, new_triage = await _idea(
        db_factory, new_epoch, key="invented-carried-idea", triage_status="excluded:scope"
    )
    unrelated = await _screen(db_factory, new_epoch, await _doc(db_factory))

    async with db_factory() as db:
        assert await carry_rescues(db, from_epoch=old_epoch, to_epoch=new_epoch) == 2
    async with db_factory() as db:
        assert await carry_rescues(db, from_epoch=old_epoch, to_epoch=new_epoch) == 0

    assert (await _get(db_factory, LandscapeScreen, new_screen)).status == "rescued"
    assert (await _get(db_factory, LandscapeTriage, new_triage)).status == "rescued"
    assert (await _get(db_factory, LandscapeScreen, unrelated)).status == "excluded:screen"
    carried = {r.item_id: r for r in await _rescues(db_factory) if r.epoch_id == new_epoch}
    assert set(carried) == {new_screen, new_triage}
    assert all(r.carried and r.actor_id == actor.id for r in carried.values())
    assert carried[new_screen].reason == "Screen reason."
    assert carried[new_triage].reason == "Triage reason."


async def _rescues(db_factory) -> list[LandscapeRescue]:
    async with db_factory() as db:
        return list((await db.scalars(
            select(LandscapeRescue).order_by(LandscapeRescue.created_at)
        )).all())
