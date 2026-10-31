"""S9 final assessment against Postgres (spec §7.10, §7.11 step 4; L60, D37, D59, D62, F2).

The batch layer is faked: ``batches.submit`` records the items it is given, so each test
reads the exact request S9 would send, and results are fed to ``ingest_final_outcomes`` as
``BatchOutcome`` objects. Every session shares one connection whose outer transaction rolls
back, so ``now()`` is the same for every row a test writes; times that matter are set
explicitly. Every name below is invented.
"""
import contextlib
import itertools
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome
from src.landscaping.schemas import GATING_KEYS, KEY_POINT_KEYS, stage_schema
from src.landscaping.stages import final
from src.landscaping.stages.cut import cut_due, run_cut
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeBatch,
    LandscapeBatchItem,
    LandscapeCandidateGrouping,
    LandscapeConsult,
    LandscapeCut,
    LandscapeDecision,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRanking,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
)
from src.services import blackbird_rubric

pytestmark = pytest.mark.integration

MODEL = "claude-opus-5"
INVENTED_LAB = "Lab of Invented Marsh Biology"
DRAINED = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
#: ``landscape_review_batches.issued_on`` is unique: one day per issued batch.
_ISSUE_DAYS = itertools.count()
#: The rubric on disk's dimensions, which ``epochs.build_snapshot`` stores.
DIMENSION_KEYS = tuple(dim.key for dim in blackbird_rubric.load_rubric().dimensions)
_RANK_WORDS = re.compile(r"\b(rank|ranks|ranked|ranking|position|positions)\b", re.IGNORECASE)


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


@pytest.fixture
def submitted(monkeypatch):
    """The items of every faked ``batches.submit`` call, in order."""
    calls: list[list[tuple[uuid.UUID, dict]]] = []

    async def fake_submit(db_factory, *, stage, row_model, items):
        assert stage == "final"
        assert row_model is LandscapeAssessment
        calls.append(list(items))
        return uuid.uuid4()

    monkeypatch.setattr(batches, "submit", fake_submit)
    return calls


# --- builders -------------------------------------------------------------------------------


async def _epoch(db_factory, status: str, *, drained_at: datetime | None = None) -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(
            status=status, snapshot=epochs.build_snapshot(), drained_at=drained_at,
            rejudge_started_at=DRAINED - timedelta(days=1) if drained_at else None,
        )
        db.add(epoch)
        await db.commit()
        return epoch.id


def _key(symbol: str) -> str:
    return f"biology;{symbol};inhibit;MONDO:{uuid.uuid4().int % 10_000_000:07d}"


async def _idea(
    db_factory, epoch_id: uuid.UUID, *, key: str | None = None, symbol: str = "INV1",
    deep_status: str | None = "done",
) -> LandscapeIdea:
    """One idea with one document and, unless ``deep_status`` is None, one S6 record."""
    async with db_factory() as db:
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            title=f"{symbol} loss slows an invented carcinoma",
            summary_text=f"{symbol} knockdown slowed tumour growth in mice.",
            published_on=date(2026, 3, 1), document_type="article",
            qualifying_institution=INVENTED_LAB, lab_key="name:marsh|q", s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        extraction = LandscapeExtraction(
            document_id=doc.id, epoch_id=epoch_id, basis="summary", status="done",
            raw={"ideas": [], "linked_publications": []},
        )
        idea = LandscapeIdea(
            epoch_id=epoch_id, idea_key=key or _key(symbol), idea_type="biology",
            targets=[{"hgnc_id": "HGNC:90001", "symbol": symbol, "label": symbol}],
            direction="inhibit",
            disease={"mondo_id": "MONDO:9999999", "label": "invented carcinoma"},
            modality_variants=["small_molecule"], stage="target_validation",
            evidence_grade="in_vivo", jhu_group_count=1, version_hash=uuid.uuid4().hex,
            evidence_summary={"open_targets": {"association": 0.42}}, in_current_cut=True,
        )
        db.add_all([extraction, idea])
        await db.flush()
        db.add(LandscapeIdeaEvidence(
            idea_id=idea.id, extraction_id=extraction.id, candidate_index=0,
        ))
        if deep_status is not None:
            deep = LandscapeDeep(
                idea_id=idea.id, idea_version=idea.version_hash, epoch_id=epoch_id,
                status=deep_status, summary=f"{symbol} is a credible invented target.",
                gating={k: "met" for k in GATING_KEYS},
                full_text_evidence={"quotes": [f"{symbol} loss reduced growth"]},
            )
            db.add(deep)
            await db.flush()
            db.add(LandscapeConsult(
                deep_id=deep.id, domain="scientific", opinion={"view": "Invented opinion."},
            ))
        await db.commit()
        return idea


async def _opportunity(
    db_factory, epoch_id: uuid.UUID, keys: list[str], *, version: int = 1,
    is_probe: bool = False, opportunity_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """An opportunity (created unless given) with version ``version`` over ``keys``."""
    async with db_factory() as db:
        if opportunity_id is None:
            opportunity = LandscapeOpportunity(version_counter=version)
            db.add(opportunity)
            await db.flush()
            opportunity_id = opportunity.id
        else:
            opportunity = await db.get(LandscapeOpportunity, opportunity_id)
            opportunity.version_counter = version
        db.add(LandscapeOpportunityVersion(
            opportunity_id=opportunity_id, version=version, epoch_id=epoch_id,
            member_idea_keys=keys, label="Invented marsh programme", theme="An invented theme.",
            is_probe=is_probe, status="done",
        ))
        await db.commit()
        return opportunity_id


async def _cut(
    db_factory, epoch_id: uuid.UUID, ideas: list[LandscapeIdea], *, at: datetime
) -> uuid.UUID:
    async with db_factory() as db:
        cut = LandscapeCut(
            epoch_id=epoch_id, status="complete", n=len(ideas), k=len(ideas),
            ordered_idea_ids=[str(idea.id) for idea in ideas], folds={"folded": {}},
            created_at=at, completed_at=at,
        )
        db.add(cut)
        await db.commit()
        return cut.id


async def _grouping(
    db_factory, epoch_id: uuid.UUID, cut_id: uuid.UUID, keys: list[str], *,
    status: str = "done", current: bool = True,
) -> uuid.UUID:
    async with db_factory() as db:
        grouping = LandscapeCandidateGrouping(
            epoch_id=epoch_id, cut_id=cut_id, grouping_key=uuid.uuid4().hex,
            member_idea_keys=keys, label="Invented grouping", status=status, current=current,
        )
        db.add(grouping)
        await db.commit()
        return grouping.id


async def _issue_probe(db_factory, active_id: uuid.UUID, key: str) -> uuid.UUID:
    """A probe opportunity drawn in ``active_id`` and issued in a blind review batch."""
    opportunity_id = await _opportunity(db_factory, active_id, [key], is_probe=True)
    async with db_factory() as db:
        issued_on = date(2026, 1, 1) + timedelta(days=next(_ISSUE_DAYS))
        batch = LandscapeReviewBatch(issued_on=issued_on, blind=True)
        db.add(batch)
        await db.flush()
        db.add(LandscapeReviewBatchItem(
            batch_id=batch.id, opportunity_id=opportunity_id, is_probe=True, position=1,
        ))
        await db.commit()
    return opportunity_id


async def _assessment(db_factory, **values) -> uuid.UUID:
    async with db_factory() as db:
        row = LandscapeAssessment(**values)
        db.add(row)
        await db.commit()
        return row.id


def _verdict(**overrides) -> dict:
    verdict = {
        "company_or_project": "Invented marsh programme",
        "headline": "Blocking INV1 slows an invented carcinoma",
        "key_points": {key: [f"An invented point on {key}."] for key in KEY_POINT_KEYS},
        "elevator_pitch": "An invented pitch.",
        "score_rationale": "Invented reasons.",
        "strengths": ["An invented strength."],
        "risks": ["An invented risk."],
        "competitive_landscape": ["No invented competitor."],
        "evidence_maturity": ["Invented mouse data."],
        "gating": {key: "met" for key in GATING_KEYS},
        "gating_rationales": {key: "Invented." for key in GATING_KEYS},
        "scores": {key: 2 + n % 4 for n, key in enumerate(DIMENSION_KEYS)},
        "dimension_rationales": {key: "Invented." for key in DIMENSION_KEYS},
        "red_flags": [],
        "recommendation": "conditional",
        "rationale": "Invented rationale.",
        "recommended_next_experiment": "An invented experiment.",
        "confidence": "Moderate",
        "claim_support": {key: [["doc:invented-1"]] for key in KEY_POINT_KEYS},
        "member_notes": [{"idea": "invented", "note": "Adds the invented target."}],
    }
    verdict.update(overrides)
    return verdict


async def _ingest(db_factory, row_id: uuid.UUID, reply: dict | str) -> LandscapeAssessment:
    """Feed one succeeded result for ``row_id`` and return the row as stored."""
    async with db_factory() as db:
        batch = LandscapeBatch(
            stage="final", model=MODEL, row_table="landscape_assessments", status="ended",
        )
        db.add(batch)
        await db.flush()
        item = LandscapeBatchItem(batch_id=batch.id, item_seq=1, row_id=row_id)
        db.add(item)
        await db.flush()
        row = await db.get(LandscapeAssessment, row_id)
        row.status, row.batch_item_id = "submitted", item.id
        await db.commit()
        batch_id, item_id = batch.id, item.id
    text = reply if isinstance(reply, str) else json.dumps(reply)
    message = SimpleNamespace(
        model=MODEL, stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
    )
    await final.ingest_final_outcomes(db_factory, [BatchOutcome(
        batch_row_id=batch_id, batch_item_id=item_id, item_seq=1, row_id=row_id,
        stage="final", kind="succeeded", model=MODEL, message=message, usage={},
    )])
    async with db_factory() as db:
        return await db.get(LandscapeAssessment, row_id, populate_existing=True)


def _user_message(request: dict) -> str:
    return request["messages"][0]["content"]


async def _rows(db_factory, epoch_id: uuid.UUID) -> list[LandscapeAssessment]:
    async with db_factory() as db:
        return list((await db.scalars(
            select(LandscapeAssessment).where(LandscapeAssessment.epoch_id == epoch_id)
        )).all())


# --- S9 requests and results ----------------------------------------------------------------


async def test_final_request_carries_no_rank(db_factory, submitted):
    epoch_id = await _epoch(db_factory, "active")
    first = await _idea(
        db_factory, epoch_id, key="biology;INVA;inhibit;MONDO:0000001", symbol="INVA",
    )
    second = await _idea(
        db_factory, epoch_id, key="biology;INVB;inhibit;MONDO:0000002", symbol="INVB",
    )
    await _cut(db_factory, epoch_id, [second, first], at=DRAINED)
    async with db_factory() as db:
        db.add_all([
            LandscapeRanking(epoch_id=epoch_id, idea_id=second.id, position=1),
            LandscapeRanking(epoch_id=epoch_id, idea_id=first.id, position=7),
        ])
        await db.commit()
    await _opportunity(db_factory, epoch_id, [second.idea_key, first.idea_key])

    assert await final.submit_final_batch(db_factory, epoch_id=epoch_id) == 1

    [[(_, request)]] = submitted
    message = _user_message(request)
    assert _RANK_WORDS.search(message) is None
    assert "in_current_cut" not in message
    # Members follow idea_key order, never S7 or S5 order.
    assert message.index(first.idea_key) < message.index(second.idea_key)
    assert "Invented marsh programme" in message
    assert "<publication_abstract>" in message and "<methods_section>" in message
    assert "Invented opinion." in message


async def test_weighted_score_from_the_epochs_rubric(db_factory, monkeypatch):
    epoch_id = await _epoch(db_factory, "active")
    idea = await _idea(db_factory, epoch_id)
    opportunity_id = await _opportunity(db_factory, epoch_id, [idea.idea_key])
    row_id = await _assessment(
        db_factory, opportunity_id=opportunity_id, opportunity_version=1, epoch_id=epoch_id,
    )
    original = final.epoch_weighted_score
    seen: list[tuple] = []

    def spy(rubric, scores):
        seen.append((rubric, dict(scores)))
        return original(rubric, scores)

    monkeypatch.setattr(final, "epoch_weighted_score", spy)
    verdict = _verdict(weighted_score=5.0, band="advance")

    row = await _ingest(db_factory, row_id, verdict)

    async with db_factory() as db:
        stored = (await epochs.epoch_inputs(db, epoch_id)).rubric
    assert row.status == "done"
    assert seen == [(stored, verdict["scores"])] and seen[0][0] is stored
    # The model's own score and band are ignored; the values match the loaded rubric's.
    assert row.weighted_score == blackbird_rubric.weighted_score(verdict["scores"])
    assert row.band == blackbird_rubric.band(row.weighted_score)
    assert row.raw_verdict == verdict
    assert row.structured_lines and "INV1" in row.structured_lines[0]
    assert row.served_by_model == MODEL


async def test_invalid_gating_retried_then_failed(db_factory):
    epoch_id = await _epoch(db_factory, "active")
    idea = await _idea(db_factory, epoch_id)
    opportunity_id = await _opportunity(db_factory, epoch_id, [idea.idea_key])
    row_id = await _assessment(
        db_factory, opportunity_id=opportunity_id, opportunity_version=1, epoch_id=epoch_id,
    )
    boolean_gates = _verdict(gating={key: True for key in GATING_KEYS})

    row = await _ingest(db_factory, row_id, boolean_gates)
    assert row.status == "pending"
    assert row.fallback_from is None
    assert row.error.startswith(final._INVALID_ONCE)

    row = await _ingest(db_factory, row_id, boolean_gates)
    assert row.status == "failed:invalid"
    assert row.weighted_score is None


async def test_claim_support_covers_every_bullet(db_factory):
    assert "claim_support" in stage_schema("final", blackbird_rubric.load_rubric())["required"]
    epoch_id = await _epoch(db_factory, "active")
    idea = await _idea(db_factory, epoch_id)
    opportunity_id = await _opportunity(db_factory, epoch_id, [idea.idea_key])
    row_id = await _assessment(
        db_factory, opportunity_id=opportunity_id, opportunity_version=1, epoch_id=epoch_id,
    )
    key_points = {key: ["Invented first.", "Invented second."] for key in KEY_POINT_KEYS}
    support = {key: [["doc:invented-1"], "estimate"] for key in KEY_POINT_KEYS}
    short, empty = KEY_POINT_KEYS[0], KEY_POINT_KEYS[1]
    support[short] = [["doc:invented-2"]]
    support[empty] = [[], ["record:invented"]]

    row = await _ingest(
        db_factory, row_id, _verdict(key_points=key_points, claim_support=support),
    )

    assert row.status == "done"
    assert set(row.claim_support) == set(KEY_POINT_KEYS)
    for group, bullets in row.key_points.items():
        assert len(row.claim_support[group]) == len(bullets) == 2
    assert row.claim_support[short] == [["doc:invented-2"], "estimate"]
    assert row.claim_support[empty] == ["estimate", ["record:invented"]]
    assert row.claim_support[KEY_POINT_KEYS[2]] == [["doc:invented-1"], "estimate"]


async def test_reassessment_at_most_once_per_cycle(db_factory, submitted):
    epoch_id = await _epoch(db_factory, "active")
    idea = await _idea(db_factory, epoch_id)
    other = await _idea(db_factory, epoch_id, symbol="INV2")
    opportunity_id = await _opportunity(db_factory, epoch_id, [idea.idea_key])
    first_id = await _assessment(
        db_factory, opportunity_id=opportunity_id, opportunity_version=1, epoch_id=epoch_id,
        status="done",
    )
    # S8 mints version 2 when a member joins.
    await _opportunity(
        db_factory, epoch_id, [idea.idea_key, other.idea_key], version=2,
        opportunity_id=opportunity_id,
    )

    assert await final.submit_final_batch(db_factory, epoch_id=epoch_id) == 0
    assert submitted == []

    async with db_factory() as db:
        first = await db.get(LandscapeAssessment, first_id)
        first.created_at = await db.scalar(select(func.now() - timedelta(days=8)))
        await db.commit()

    assert await final.submit_final_batch(db_factory, epoch_id=epoch_id) == 1
    rows = {row.opportunity_version: row for row in await _rows(db_factory, epoch_id)}
    assert set(rows) == {1, 2}
    assert rows[2].status == "pending" and rows[2].grouping_id is None
    [[(row_id, _)]] = submitted
    assert row_id == rows[2].id


async def test_candidate_assessment_keyed_by_grouping(db_factory, submitted):
    epoch_id = await _epoch(db_factory, "candidate")
    a = await _idea(db_factory, epoch_id)
    b = await _idea(db_factory, epoch_id, symbol="INV2")
    c = await _idea(db_factory, epoch_id, symbol="INV3")
    d = await _idea(db_factory, epoch_id, symbol="INV4")
    cut_id = await _cut(db_factory, epoch_id, [a, b, c, d], at=DRAINED)
    grouping_id = await _grouping(db_factory, epoch_id, cut_id, [a.idea_key, b.idea_key])
    await _grouping(db_factory, epoch_id, cut_id, [c.idea_key, d.idea_key], status="submitted")
    await _grouping(db_factory, epoch_id, cut_id, [d.idea_key], current=False)

    assert await final.submit_final_batch(db_factory, epoch_id=epoch_id) == 1

    [row] = await _rows(db_factory, epoch_id)
    assert row.grouping_id == grouping_id
    assert row.opportunity_id is None and row.opportunity_version is None
    async with db_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(LandscapeOpportunityVersion)
            .where(LandscapeOpportunityVersion.epoch_id == epoch_id)
        ) == 0


async def test_candidate_scope_includes_issued_probes(db_factory, submitted):
    active_id = await _epoch(db_factory, "active")
    candidate_id = await _epoch(db_factory, "candidate")
    issued_key, drawn_key = _key("INV7"), _key("INV8")
    await _idea(db_factory, active_id, key=issued_key, symbol="INV7")
    await _idea(db_factory, active_id, key=drawn_key, symbol="INV8")
    probe_id = await _issue_probe(db_factory, active_id, issued_key)
    await _opportunity(db_factory, active_id, [drawn_key], is_probe=True)  # drawn, not issued
    await _idea(db_factory, candidate_id, key=issued_key, symbol="INV7")
    await _idea(db_factory, candidate_id, key=drawn_key, symbol="INV8")

    assert await final.submit_final_batch(db_factory, epoch_id=candidate_id) == 1

    [row] = await _rows(db_factory, candidate_id)
    assert (row.opportunity_id, row.opportunity_version) == (probe_id, 1)
    assert row.grouping_id is None


# --- candidate readiness --------------------------------------------------------------------


async def _ready_candidate(db_factory, *, s9_status: str = "done"):
    """A drained candidate with a post-drain cut of one idea, grouped, with S9 at
    ``s9_status``; returns ``(epoch_id, idea, cut_id, grouping_id, assessment_id)``."""
    epoch_id = await _epoch(db_factory, "candidate", drained_at=DRAINED)
    idea = await _idea(db_factory, epoch_id)
    cut_id = await _cut(db_factory, epoch_id, [idea], at=DRAINED + timedelta(hours=1))
    grouping_id = await _grouping(db_factory, epoch_id, cut_id, [idea.idea_key])
    assessment_id = await _assessment(
        db_factory, epoch_id=epoch_id, grouping_id=grouping_id, status=s9_status,
    )
    return epoch_id, idea, cut_id, grouping_id, assessment_id


async def test_candidate_s9_complete_counts_terminal_failures(db_factory):
    epoch_id = await _epoch(db_factory, "candidate", drained_at=DRAINED)
    a = await _idea(db_factory, epoch_id)
    b = await _idea(db_factory, epoch_id, symbol="INV2")
    c = await _idea(db_factory, epoch_id, symbol="INV3")
    cut_id = await _cut(db_factory, epoch_id, [a, b, c], at=DRAINED + timedelta(hours=1))
    ids = [await _grouping(db_factory, epoch_id, cut_id, [i.idea_key]) for i in (a, b, c)]
    statuses = ("failed:invalid", "refused", "submitted")
    rows = [
        await _assessment(db_factory, epoch_id=epoch_id, grouping_id=gid, status=status)
        for gid, status in zip(ids, statuses, strict=True)
    ]

    async with db_factory() as db:
        assert await final.candidate_s9_complete(db, epoch_id=epoch_id) is False
        (await db.get(LandscapeAssessment, rows[2])).status = "failed:errored"
        await db.commit()
        assert await final.candidate_s9_complete(db, epoch_id=epoch_id) is True
        assert await final.candidate_ready(db, epoch_id=epoch_id) is True


async def test_probe_without_deep_gets_failed_no_deep(db_factory):
    active_id = await _epoch(db_factory, "active")
    candidate_id = await _epoch(db_factory, "candidate", drained_at=DRAINED)
    gated, refused, passed = _key("INV7"), _key("INV8"), _key("INV9")
    probes = {}
    for key, status in ((gated, "excluded:deep_gate"), (refused, "refused"), (passed, "done")):
        await _idea(db_factory, active_id, key=key)
        probes[key] = await _issue_probe(db_factory, active_id, key)
        await _idea(db_factory, candidate_id, key=key, deep_status=status)

    async with db_factory() as db:
        assert await final.mark_unassessable_probes(db, epoch_id=candidate_id) == 2
        assert await final.mark_unassessable_probes(db, epoch_id=candidate_id) == 0

    rows = {row.opportunity_id: row for row in await _rows(db_factory, candidate_id)}
    assert set(rows) == {probes[gated], probes[refused]}
    assert {row.status for row in rows.values()} == {"failed:no_deep"}
    assert all(row.opportunity_version == 1 for row in rows.values())


async def test_probe_idea_absent_gets_failed_no_deep(db_factory):
    active_id = await _epoch(db_factory, "active")
    candidate_id = await _epoch(db_factory, "candidate", drained_at=DRAINED)
    key = _key("INV7")
    await _idea(db_factory, active_id, key=key)
    probe_id = await _issue_probe(db_factory, active_id, key)

    async with db_factory() as db:
        assert await final.mark_unassessable_probes(db, epoch_id=candidate_id) == 1

    [row] = await _rows(db_factory, candidate_id)
    assert (row.opportunity_id, row.opportunity_version, row.status) == (
        probe_id, 1, "failed:no_deep",
    )
    assert "no row" in row.error


async def test_absent_probe_idea_not_marked_while_s3_pending(db_factory):
    active_id = await _epoch(db_factory, "active")
    candidate_id = await _epoch(db_factory, "candidate")  # not drained: S3 still owed
    key = _key("INV7")
    await _idea(db_factory, active_id, key=key)
    await _issue_probe(db_factory, active_id, key)
    async with db_factory() as db:
        doc = LandscapeDocument(
            kind="work", canonical_external_id=f"doi:10.9999/invented-{uuid.uuid4().hex}",
            title="INV7 in an invented carcinoma", s1_status="eligible",
        )
        db.add(doc)
        await db.flush()
        db.add(LandscapeExtraction(
            document_id=doc.id, epoch_id=candidate_id, basis="summary", status="pending",
        ))
        await db.commit()

    async with db_factory() as db:
        assert await final.mark_unassessable_probes(db, epoch_id=candidate_id) == 0
        assert await final.candidate_ready(db, epoch_id=candidate_id) is False

    assert await _rows(db_factory, candidate_id) == []


async def test_candidate_ready_requires_s6_and_s8_on_latest_cut(db_factory):
    epoch_id = await _epoch(db_factory, "candidate", drained_at=DRAINED)
    old = await _idea(db_factory, epoch_id)
    pilot_cut = await _cut(db_factory, epoch_id, [old], at=DRAINED - timedelta(days=3))
    pilot_grouping = await _grouping(db_factory, epoch_id, pilot_cut, [old.idea_key])
    await _assessment(db_factory, epoch_id=epoch_id, grouping_id=pilot_grouping, status="done")

    async with db_factory() as db:
        # The pilot cut predates the drain, so its finished S9 counts for nothing.
        assert await final.candidate_ready(db, epoch_id=epoch_id) is False

    new = await _idea(db_factory, epoch_id, symbol="INV2", deep_status="running")
    cut_id = await _cut(db_factory, epoch_id, [old, new], at=DRAINED + timedelta(hours=1))
    async with db_factory() as db:
        assert await final.candidate_ready(db, epoch_id=epoch_id) is False  # S6 running
        deep = await db.scalar(select(LandscapeDeep).where(LandscapeDeep.idea_id == new.id))
        deep.status = "done"
        await db.commit()
        assert await final.candidate_ready(db, epoch_id=epoch_id) is False  # S8 not on this cut

    grouping_id = await _grouping(
        db_factory, epoch_id, cut_id, [old.idea_key, new.idea_key], status="submitted",
    )
    async with db_factory() as db:
        assert await final.candidate_ready(db, epoch_id=epoch_id) is False  # label pending
        (await db.get(LandscapeCandidateGrouping, grouping_id)).status = "done"
        await db.commit()
        assert await final.candidate_ready(db, epoch_id=epoch_id) is False  # S9 owed

    await _assessment(db_factory, epoch_id=epoch_id, grouping_id=grouping_id, status="done")
    async with db_factory() as db:
        assert await final.candidate_ready(db, epoch_id=epoch_id) is True


async def test_candidate_activates_across_daily_cut_due(db_factory):
    epoch_id, _, cut_id, _, assessment_id = await _ready_candidate(
        db_factory, s9_status="submitted",
    )
    async with db_factory() as db:
        assert await final.candidate_ready(db, epoch_id=epoch_id) is False
        cut_at = (await db.get(LandscapeCut, cut_id)).created_at

    next_morning = DRAINED.replace(hour=7) + timedelta(days=1)
    assert cut_due(now=next_morning, s4_pending=0, last_cut_at=cut_at) is True
    assert await run_cut(db_factory, epoch_id) is None  # frozen: no new cut

    async with db_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(LandscapeCut).where(LandscapeCut.epoch_id == epoch_id)
        ) == 1
        (await db.get(LandscapeAssessment, assessment_id)).status = "done"
        await db.commit()
        assert await final.candidate_ready(db, epoch_id=epoch_id) is True


# --- S7 and decisions -----------------------------------------------------------------------


async def test_s9_not_blocked_by_s7(db_factory, submitted):
    epoch_id = await _epoch(db_factory, "active")
    ranked = await _idea(db_factory, epoch_id)
    unranked = await _idea(db_factory, epoch_id, symbol="INV2")
    alone = await _idea(db_factory, epoch_id, symbol="INV3")
    await _cut(db_factory, epoch_id, [alone, ranked, unranked], at=DRAINED)
    async with db_factory() as db:
        db.add(LandscapeRanking(epoch_id=epoch_id, idea_id=ranked.id, position=1))
        await db.commit()
    alone_id = await _opportunity(db_factory, epoch_id, [alone.idea_key])
    pair_id = await _opportunity(db_factory, epoch_id, [ranked.idea_key, unranked.idea_key])

    assert await final.submit_final_batch(db_factory, epoch_id=epoch_id) == 2

    rows = {row.id: row.opportunity_id for row in await _rows(db_factory, epoch_id)}
    [items] = submitted
    # The S7-placed member goes first; the subject S7 never placed is still sent.
    assert [rows[row_id] for row_id, _ in items] == [pair_id, alone_id]


async def test_decisions_carried_forward_marked_changed(db_factory, submitted):
    epoch_id = await _epoch(db_factory, "active")
    idea = await _idea(db_factory, epoch_id)
    other = await _idea(db_factory, epoch_id, symbol="INV2")
    opportunity_id = await _opportunity(db_factory, epoch_id, [idea.idea_key])
    decided_id = await _assessment(
        db_factory, opportunity_id=opportunity_id, opportunity_version=1, epoch_id=epoch_id,
        status="done", headline="The decided headline",
    )
    async with db_factory() as db:
        (await db.get(LandscapeAssessment, decided_id)).created_at = await db.scalar(
            select(func.now() - timedelta(days=8))
        )
        db.add(LandscapeDecision(
            opportunity_id=opportunity_id, reviewer_name="Invented Reviewer", decision="pursue",
            reason="Invented reason.", landscape_assessment_id=decided_id,
        ))
        await db.commit()
    await _opportunity(
        db_factory, epoch_id, [idea.idea_key, other.idea_key], version=2,
        opportunity_id=opportunity_id,
    )

    assert await final.submit_final_batch(db_factory, epoch_id=epoch_id) == 1
    [[(new_id, _)]] = submitted
    new = await _ingest(db_factory, new_id, _verdict())

    assert new.status == "done" and new.opportunity_version == 2
    async with db_factory() as db:
        [decision] = (await db.scalars(
            select(LandscapeDecision).where(LandscapeDecision.opportunity_id == opportunity_id)
        )).all()
        decided = await db.get(LandscapeAssessment, decided_id)
    assert decision.decision == "pursue"
    # Still on the assessment it was made on, which now differs from the one shown.
    assert decision.landscape_assessment_id == decided_id != new.id
    assert (decided.status, decided.opportunity_version, decided.headline) == (
        "done", 1, "The decided headline",
    )
    assert new.headline != decided.headline
