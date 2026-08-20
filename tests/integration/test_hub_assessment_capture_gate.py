"""One interview, one verdict — and only from the turn that was asked for one.

Production run 60c53424 wrote THREE `opportunity_assessments` rows for a single
pearce interview (thread 1787151586.955459, weighted scores 2.51 / 2.66 / 2.69).
The thread's twelve messages put the hub's replies at ordinals 2, 4, 6, 8, 10 and
12, and the three rows' `slack_ts` values were the ordinal-8, ordinal-10 and
ordinal-12 replies. `phase4_guidance` renders EXPLORE at ordinal <= 4, DECIDE at
<= 11 and CONCLUDE above that, so two of those three sidecars were emitted on
DECIDE turns — turns whose guidance never asks for a sidecar at all. The
`<assessment_json>` contract is nonetheless in the static body of
`prompts/roles/scout_hub/phase4-thread-reply.md` on every phase-4 turn, so the
model could see it and fill it in early.

`_capture_hub_assessment` persisted all three: it ran on every phase-4 reply with
no phase check, and nothing anywhere asked whether the thread already had a
verdict. Both gates live here now, and a refusal is recorded as an
`AssessmentDrop` rather than dropped silently — the concluding reply is already
in Slack by the time either fires, so an invisible refusal would be the same
failure the drops table exists to end.

The historical duplicates this reproduces are not hypothetical: run 88d81cd8
carries huganir x3, hart x3, pearce x2, culotta x2 and cai x2 on single threads.
"""

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


# `phase4_guidance` takes the ORDINAL (thread.message_count + 1), not the prior
# count: EXPLORE <= 4, DECIDE <= 11, CONCLUDE above. These are the two sides of
# that boundary, named so a reader does not have to redo the arithmetic.
_DECIDE_COUNT = 7       # ordinal 8  — an ordinary mid-interview turn
_CONCLUDE_COUNT = 11    # ordinal 12 — the one turn asked for a sidecar


def _verdict(score: int = 3) -> dict:
    return {
        "subject_agent_id": "gordy",
        "recommendation": "route-to-incubation",
        "rationale": "A low-input paired metabolomics and chromatin workflow.",
        "scores": {key: score for key in RUBRIC_WEIGHTS},
    }


def _reply_with_sidecar(score: int = 3) -> str:
    """A hub reply exactly as the model emits one: the Slack body, then the bare
    (unfenced) sidecar outside it."""
    import json

    return (
        "<slack_message>That is enough for me to take a view.</slack_message>\n"
        f"<assessment_json>{json.dumps(_verdict(score))}</assessment_json>"
    )


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


def _hub(factory, run_id):
    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(
        agents=[agent], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    return sim, agent


def _thread(message_count: int, thread_id: str = "t1") -> ThreadState:
    return ThreadState(
        thread_id=thread_id, channel="single-cell-omics", other_agent_id="gordy",
        message_count=message_count,
    )


async def _assessments(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(OpportunityAssessment)
            .where(OpportunityAssessment.simulation_run_id == run_id)
            .order_by(OpportunityAssessment.created_at)
        )).scalars().all()


async def _drops(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AssessmentDrop)
            .where(AssessmentDrop.simulation_run_id == run_id)
            .order_by(AssessmentDrop.created_at)
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


@pytest.mark.asyncio
async def test_a_sidecar_from_a_decide_turn_is_refused(engine):
    """The exact production bug: ordinal 8 is a DECIDE turn, its guidance never
    asks for a sidecar, and a sidecar that shows up anyway must not become the
    interview's verdict of record."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    try:
        await sim._capture_hub_assessment(
            agent, _thread(_DECIDE_COUNT), _reply_with_sidecar(), "1787259939.257539"
        )
        assert await _assessments(factory, run_id) == []
        drops = await _drops(factory, run_id)
        assert [d.reason for d in drops] == ["premature_sidecar"]
        assert drops[0].thread_id == "t1"
        assert drops[0].subject_agent_id == "gordy"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_conclude_turn_still_persists_its_verdict(engine):
    """The gate must not cost the legitimate verdict. Ordinal 12 is the CONCLUDE
    turn, and it is the one reply the sidecar was asked for."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    try:
        await sim._capture_hub_assessment(
            agent, _thread(_CONCLUDE_COUNT), _reply_with_sidecar(), "1787260617.540319"
        )
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].recommendation == "route-to-incubation"
        assert rows[0].slack_ts == "1787260617.540319"
        assert await _drops(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_second_verdict_on_the_same_thread_is_refused(engine):
    """`max_thread_messages=12` gives a thread exactly one CONCLUDE turn today,
    but `thread_guidance` renders CONCLUDE for every ordinal above 11 — so
    raising that setting would hand one interview a run of concluding turns.
    The first verdict for a thread is the verdict."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        await sim._capture_hub_assessment(agent, thread, _reply_with_sidecar(3), "1.1")
        thread.message_count += 2
        await sim._capture_hub_assessment(agent, thread, _reply_with_sidecar(4), "2.2")

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "one interview, one verdict"
        assert rows[0].slack_ts == "1.1", "the first verdict is the one of record"
        drops = await _drops(factory, run_id)
        assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_dedup_is_per_thread_not_per_process(engine):
    """A second interview is not a duplicate. The guard keys on the thread, so a
    different thread in the same process still gets its own verdict — the same
    distinction `_specialist_floor_gap` keys its consult record on."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    try:
        await sim._capture_hub_assessment(
            agent, _thread(_CONCLUDE_COUNT, "t1"), _reply_with_sidecar(3), "1.1"
        )
        await sim._capture_hub_assessment(
            agent, _thread(_CONCLUDE_COUNT, "t2"), _reply_with_sidecar(4), "2.2"
        )
        assert len(await _assessments(factory, run_id)) == 2
        assert await _drops(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_verdict_queued_for_retry_still_counts_as_the_thread_s_verdict(engine):
    """A failed first attempt is HELD, not lost: `_persist_assessment` queues the
    row on `_pending_assessments` and `_flush_pending_assessments` drains it from
    both the main loop and `stop()`. So that row is still destined to exist, and
    treating the thread as un-assessed would let a second verdict through and
    land BOTH — the duplicate this whole gate exists to prevent.

    The narrow cost is deliberate: if a queued row never drains, the interview
    keeps no verdict. That is the retry queue's own pre-existing risk, and it is
    the better failure — one missing verdict beats two contradictory ones.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        # A run id that violates the FK: the insert raises, `_persist_assessment`
        # catches it and queues the row, exactly as a pool timeout would.
        sim.simulation_run_id = uuid.uuid4()
        await sim._capture_hub_assessment(agent, thread, _reply_with_sidecar(3), "1.1")
        assert len(sim._pending_assessments) == 1

        sim.simulation_run_id = run_id
        thread.message_count += 2
        await sim._capture_hub_assessment(agent, thread, _reply_with_sidecar(4), "2.2")

        assert await _assessments(factory, run_id) == [], "no second verdict landed"
        assert len(sim._pending_assessments) == 1, "the queued row is untouched"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
    finally:
        await _delete_run(factory, run_id)
