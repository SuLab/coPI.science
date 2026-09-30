"""§8.1 end to end through the capture path."""
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, OpportunityAssessment, SimulationRun
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


def _verdict(score):
    return {
        "subject_agent_id": "gordy", "recommendation": "route-to-incubation",
        "rationale": f"rationale {score}", "scores": {k: score for k in RUBRIC_WEIGHTS},
        "company_or_project": f"Project {score}", "elevator_pitch": f"Pitch {score}.",
    }


def _reply(score, closing=False):
    body = "⏸️ Closing." if closing else "Noted."
    return f"<slack_message>{body}</slack_message>\n<assessment_json>{json.dumps(_verdict(score))}</assessment_json>"


async def _engine(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run.id,
    )
    return factory, run.id, sim, hub


async def _all(factory, model, run_id):
    async with factory() as db:
        return (await db.execute(
            select(model).where(model.simulation_run_id == run_id).order_by(model.created_at)
        )).scalars().all()


async def _cleanup(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


@pytest.mark.asyncio
async def test_a_superseded_row_equals_todays_replacement_row(engine):
    """FA-2/FA-4: the in-place row holds what today's replacement row would hold,
    except id, the 0054 columns, created_at and summary_posted_at."""
    factory, run_id, sim, hub = await _engine(engine)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy", message_count=7)
    try:
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(3), "1.1", closes_thread=False)
        thread.message_count = 9
        expected = await sim.verdicts._assessment_row(
            "blackbird", "c", _verdict(4), slack_ts="2.2",
            subject_agent_id_fallback="gordy", thread=thread,
        )
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(4), "2.2", closes_thread=False)
        (row,) = await _all(factory, OpportunityAssessment, run_id)
        skip = {"id", "created_at", "summary_posted_at", "summary_claimed_at",
                "verdict_revision", "verdict_write_id", "verdict_ordinal"}
        mapped = {c.key for c in OpportunityAssessment.__table__.columns}
        assert mapped - skip <= set(expected), (
            "the builder must cover every mapped column except the skip set, or a "
            f"column it omits silently keeps the superseded value: {sorted(mapped - skip - set(expected))}"
        )
        for key, value in expected.items():
            if key in skip:
                continue
            assert getattr(row, key) == value, key
        (drop,) = await _all(factory, AssessmentDrop, run_id)
        assert row.created_at == drop.created_at
        assert row.summary_posted_at is None, "neither verdict was announced, as today"
        assert (row.verdict_revision, row.verdict_ordinal) == (2, 10)
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_new_verdict_prunes_the_queued_one_even_when_its_write_succeeds(engine):
    """S2-09 / SA3-15."""
    factory, run_id, sim, hub = await _engine(engine)
    thread = ThreadState(thread_id="t1", channel="c", other_agent_id="gordy", message_count=7)
    try:
        sim.verdicts.ctx.simulation_run_id = uuid.uuid4()   # FK violation → queued
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(3), "1.1", closes_thread=False)
        assert len(sim.verdicts._pending_assessments) == 1
        queued = sim.verdicts._pending_assessments[0]
        assert queued["verdict_ordinal"] == 8 and queued["verdict_write_id"] is not None
        sim.verdicts.ctx.simulation_run_id = run_id
        thread.message_count = 9
        await sim.verdicts._capture_hub_assessment(hub, thread, _reply(4), "2.2", closes_thread=False)
        assert sim.verdicts._pending_assessments == []
        rows = await _all(factory, OpportunityAssessment, run_id)
        assert len(rows) == 1 and rows[0].raw_verdict == _verdict(4)
        drops = await _all(factory, AssessmentDrop, run_id)
        assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
        assert drops[0].raw_verdict == _verdict(3)
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_late_flush_of_a_stale_queued_verdict_does_not_overwrite(engine):
    factory, run_id, sim, hub = await _engine(engine)
    try:
        stale_row = await sim.verdicts._assessment_row(
            "blackbird", "c", _verdict(3), slack_ts="1.1", subject_agent_id_fallback="gordy",
            thread=ThreadState(thread_id="t1", channel="c", other_agent_id="gordy", message_count=7),
        )
        await sim.verdicts.upsert(
            ThreadState(thread_id="t1", channel="c", other_agent_id="gordy"),
            {**stale_row, "raw_verdict": _verdict(4)}, uuid.uuid4(), 10,
        )
        sim.verdicts._pending_assessments.append(
            {**stale_row, "verdict_write_id": uuid.uuid4(), "verdict_ordinal": 8}
        )
        await sim.verdicts._flush_pending_assessments()
        (row,) = await _all(factory, OpportunityAssessment, run_id)
        assert row.raw_verdict == _verdict(4) and row.verdict_ordinal == 10
        assert [d.raw_verdict for d in await _all(factory, AssessmentDrop, run_id)] == [_verdict(3)]
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_rehydrate_reads_revision_from_the_stored_row(engine):
    factory, run_id, sim, hub = await _engine(engine)
    try:
        async with factory() as db:
            db.add(OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird", channel_name="c",
                thread_id="t9", slack_ts="9.9", verdict_ordinal=10, verdict_revision=3,
            ))
            await db.commit()
        await sim.verdicts.rehydrate(set())
        assert sim.verdicts._assessed_threads["t9"].revision == 3
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_rehydrate_keeps_ordinal_zero_so_a_later_verdict_is_accepted(engine):
    """Owner decision 2026-09-30: the stored verdict_ordinal is written but not
    restored into _HeldVerdict.ordinal, because after a resume message_count is
    rebuilt from the history and a stored ordinal at or above it would make
    _sidecar_refusal refuse the interview's legitimate later verdict."""
    factory, run_id, sim, hub = await _engine(engine)
    thread = ThreadState(thread_id="t9", channel="c", other_agent_id="gordy", message_count=3)
    try:
        async with factory() as db:
            db.add(OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird", channel_name="c",
                thread_id="t9", slack_ts="9.9", verdict_ordinal=7, verdict_revision=1,
            ))
            await db.commit()
        await sim.verdicts.rehydrate(set())
        assert sim.verdicts._assessed_threads["t9"].ordinal == 0
        # The gate accepts a verdict at ordinal 4 (3 messages rebuilt from the
        # history) even though the stored row says 7: `ordinal <= held.ordinal`
        # would refuse it had the stored ordinal been restored.
        assert sim.verdicts._sidecar_refusal(hub.role, thread, closes_thread=False) is None
        # ...and the write lands: an earlier process's row is superseded even
        # though its stored ordinal is higher (ordinals restart on a resume).
        async with factory() as db:
            row = await db.scalar(select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id,
                OpportunityAssessment.thread_id == "t9"))
            values = {c.key: getattr(row, c.key) for c in OpportunityAssessment.__table__.columns
                      if c.key not in ("id", "created_at")}
        values.update(id=None, verdict_write_id=uuid.uuid4(), verdict_ordinal=4, recommendation="pass")
        async with factory() as db:
            result = await sim.verdicts._upsert_in(db, values)
            await db.commit()
        assert result.outcome == "updated"
    finally:
        await _cleanup(factory, run_id)


def test_the_retire_trio_is_gone():
    from src.agent.engine import verdicts

    src = open(verdicts.__file__, encoding="utf-8").read()
    for name in ("_retire_superseded_verdict", "_superseded_row_filter", "_superseded_raw_verdict"):
        assert f"def {name}" not in src
