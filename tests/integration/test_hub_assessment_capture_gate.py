"""One interview, one verdict — and it comes from the turn that ENDS the interview.

Two production defects meet in this gate, and they pull in opposite directions.

**Duplication (run 60c53424).** THREE `opportunity_assessments` rows for a single
pearce interview (thread 1787151586.955459, weighted scores 2.51 / 2.66 / 2.69).
The thread's twelve messages put the hub's replies at ordinals 2, 4, 6, 8, 10 and
12, and the three rows' `slack_ts` values were the ordinal-8, ordinal-10 and
ordinal-12 replies. `_capture_hub_assessment` persisted all three: it ran on every
phase-4 reply with no phase check, and nothing asked whether the thread already
held a verdict. Not hypothetical — run 88d81cd8 carries huganir x3, hart x3,
pearce x2, culotta x2 and cai x2 on single threads.

**Destruction (run 076e80b6).** The first fix gated on "is the ordinal 12", and
that premise — "a later turn will supply another sidecar" — is false for the most
common verdict class. The prompts tell the hub to deliver a NEGATIVE verdict by
opening its reply with ⏸️, and ⏸️ is exactly what `_check_thread_outcome` closes
the thread on, milliseconds later, in the same code path. Measured over 204 hub
`thread_reply` turns across three runs: all 23 `pass` sidecars ever emitted landed
on DECIDE ordinals (6/8/10) and ALL 23 carried ⏸️. In run 076e80b6, 4 of the 5
`premature_sidecar` refusals were the thread's TERMINAL message — complete
verdicts, 12-14k output tokens each, with orphaned `specialist_consults` rows and
nothing stored. Under that gate a `pass` could never be stored at all: delivering
one closes the thread long before ordinal 12 (1 of 62 threads reached it).

So the gate asks "does this reply end the interview?", not "is the ordinal 12":

* a reply that CONCLUDES (ordinal 12) or CLOSES the thread (⏸️) may store a
  verdict — that is the interview's real, last word;
* an early, non-closing sidecar is still refused and recorded
  (`premature_sidecar`), because a later turn is genuinely still owed a verdict;
* and a later concluding/closing verdict SUPERSEDES an earlier provisional one
  (last write wins, the earlier row retired) while a re-capture of the same turn
  is still refused as a duplicate.

Every refusal is recorded as an `AssessmentDrop` rather than dropped silently: the
reply is already in Slack by the time any of this runs, so an invisible refusal is
the same failure the drops table exists to end.
"""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import (
    AssessmentDrop,
    OpportunityAssessment,
    SimulationRun,
    ThreadDecision,
)
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


# `phase4_guidance` takes the ORDINAL (thread.message_count + 1), not the prior
# count: EXPLORE <= 4, DECIDE <= 11, CONCLUDE above. These are the two sides of
# that boundary, named so a reader does not have to redo the arithmetic.
_DECIDE_COUNT = 7       # ordinal 8  — an ordinary mid-interview turn
_LAST_DECIDE_COUNT = 10  # ordinal 11 — the last DECIDE turn before CONCLUDE
_CONCLUDE_COUNT = 11    # ordinal 12 — the turn whose guidance asks for a sidecar


def _verdict(score: int = 3) -> dict:
    return {
        "subject_agent_id": "gordy",
        "recommendation": "route-to-incubation",
        "rationale": "A low-input paired metabolomics and chromatin workflow.",
        "scores": {key: score for key in RUBRIC_WEIGHTS},
    }


def _reply_with_sidecar(score: int = 3, *, closing: bool = False) -> str:
    """A hub reply exactly as the model emits one: the Slack body, then the bare
    (unfenced) sidecar outside it.

    ``closing=True`` opens the body with ⏸️ — the decline convention the prompts
    ask for verbatim, and the marker `_check_thread_outcome` closes the thread on.
    That is how EVERY `pass` verdict production has ever emitted was delivered.
    """
    body = (
        "⏸️ Closing this one as a pass — the differentiation is not there yet."
        if closing
        else "That is enough for me to take a view."
    )
    return (
        f"<slack_message>{body}</slack_message>\n"
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


async def _decisions(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(ThreadDecision)
            .where(ThreadDecision.simulation_run_id == run_id)
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


async def _drive_reply(engine, monkeypatch, raw_response, *, prior_messages):
    """Drive the REAL path: `_reply_to_thread` on a live engine, so the close
    decision reaches the capture gate the way production computes it.

    ``prior_messages`` is seeded into the engine's real `MessageLog`, not just
    onto `ThreadState.message_count`: `_reply_to_thread` overwrites that field
    with ``len(get_thread_history(thread_id))`` before the phase is computed, so a
    ThreadState built with ``message_count=11`` over an EMPTY log is an ordinal-1
    EXPLORE turn, not the CONCLUDE turn it looks like (CLAUDE.md's warning, and
    the bug 81dbe44 found in both existing harnesses).

    Returns ``(sim, agent, thread, client, factory, run_id)``.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = ThreadState(
        thread_id="t1", channel="single-cell-omics", other_agent_id="gordy",
        message_count=prior_messages, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    client = sim.slack_clients["blackbird"]
    for i in range(prior_messages):
        ts = "t1" if i == 0 else f"t1.{i}"
        sim.message_log.append(LogEntry(
            ts=ts,
            channel="single-cell-omics",
            sender_agent_id="gordy" if i % 2 == 0 else "blackbird",
            sender_name="GordyBot" if i % 2 == 0 else "BlackbirdBot",
            content=f"prior interview message {i}",
            thread_ts=None if i == 0 else "t1",
            posted_at=float(i),
            # slack_ts == ts is pure-Slack-on mode: without it the seeded root has
            # no Slack presence and the reply is kept DB-only, so `client.posted`
            # would never see it.
            slack_ts=ts,
            slack_channel_id="C_OMICS",
        ))
    # Prompt construction reads profile files off disk; this drives what happens
    # AFTER the model responds.
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _fake_generate_with_tools(**kwargs):
        return raw_response

    monkeypatch.setattr(
        "src.agent.simulation.generate_with_tools", _fake_generate_with_tools
    )

    # A ⏸️ reply reaches `_close_thread` -> `_update_agent_memory`, which makes a
    # REAL, unmocked LLM call (the documented cause of a flaky hang in
    # test_concurrent_thread_safety). Stub it the same way that suite does.
    async def _fast_memory_update(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "_update_agent_memory", _fast_memory_update)

    await sim._reply_to_thread(agent, thread)
    return sim, agent, thread, client, factory, run_id


# ---------------------------------------------------------------------------
# The defect: a closing reply's verdict is the interview's verdict.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_closing_decide_reply_persists_its_verdict_and_still_closes(
    engine, monkeypatch,
):
    """The mission pin, on the real path. Ordinal 8 is a DECIDE turn, so the
    ordinal-only gate refused this exact reply — but the ⏸️ closes the thread, so
    there is no later turn to supply the verdict again. It is stored, and the
    thread still closes."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(closing=True),
        prior_messages=_DECIDE_COUNT,
    )
    try:
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "the closing reply's verdict must be stored"
        assert rows[0].recommendation == "route-to-incubation"
        assert rows[0].subject_agent_id == "gordy"
        assert await _drops(factory, run_id) == [], "no premature_sidecar refusal"

        # The close still happens, and the verdict did not cost it.
        assert thread.status == "closed"
        assert "t1" in sim._closed_thread_ids
        assert [d.outcome for d in await _decisions(factory, run_id)] == ["no_proposal"]

        # And the sidecar never reached Slack.
        assert len(client.posted) == 1
        assert "assessment_json" not in client.posted[0]["text"]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_an_ordinal_11_closing_reply_persists_its_verdict(engine, monkeypatch):
    """The parity case. If the PI takes message 12 the hub never gets an
    ordinal-12 turn at all (the `max_thread_messages` close fires first), so an
    ordinal-11 verdict was unpersistable by construction. A CLOSING one now
    lands — the ⏸️ is what makes this reply the interview's last."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(closing=True),
        prior_messages=_LAST_DECIDE_COUNT,
    )
    try:
        assert len(await _assessments(factory, run_id)) == 1
        assert await _drops(factory, run_id) == []
        assert thread.status == "closed"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_non_closing_decide_reply_is_stored_as_provisional_on_the_real_path(
    engine, monkeypatch,
):
    """Inverted 2026-08-22. This used to be refused as `premature_sidecar`.

    The refusal's justification was that "a later turn is still owed the
    verdict" — but nothing scheduled that turn, nothing tracked the debt, and
    nothing kept the discarded JSON. Run 8b64a0e0 refused two verdicts this way
    at ordinal 10, one of them the run's highest-scoring idea and its only
    `route-to-incubation`, and the run's timer ended both interviews minutes
    later. The verdict is now stored as provisional and superseded by any later
    one, which is what `_retire_superseded_verdict` was built for.
    """
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_DECIDE_COUNT,
    )
    try:
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "an early verdict is kept, not destroyed"
        assert rows[0].subject_agent_id == "gordy"
        assert await _drops(factory, run_id) == []
        assert thread.status != "closed", "an ordinary DECIDE reply does not close"
        assert sim._assessed_threads["t1"].announced is False, (
            "a provisional verdict is stored for staff but not announced"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_conclude_turn_persists_its_verdict_on_the_real_path(
    engine, monkeypatch,
):
    """The ordinal-12 acceptance path, untouched: a concluding reply that is not a
    decline still stores its verdict."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
    )
    try:
        assert len(await _assessments(factory, run_id)) == 1
        assert await _drops(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# The gate itself, driven directly — one capture per case, so the ruling under
# test is the only thing that can decide the outcome.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sidecar_from_a_non_closing_decide_turn_is_kept(engine):
    """Ordinal 8 is a DECIDE turn whose guidance never asks for a sidecar — but
    the `<assessment_json>` contract sits in the STATIC body of
    `phase4-thread-reply.md`, so the model sees it on every phase-4 turn and
    fills it in when it has made up its mind early.

    That is a verdict, and the measured cost of treating it as noise was two lost
    ones in a single run. It is kept as provisional; a later turn overrides it.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    try:
        await sim._capture_hub_assessment(
            agent, _thread(_DECIDE_COUNT), _reply_with_sidecar(), "1787259939.257539",
            closes_thread=False,
        )
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].subject_agent_id == "gordy"
        assert rows[0].slack_ts == "1787259939.257539"
        assert await _drops(factory, run_id) == []
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
            agent, _thread(_CONCLUDE_COUNT), _reply_with_sidecar(), "1787260617.540319",
            closes_thread=False,
        )
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].recommendation == "route-to-incubation"
        assert rows[0].slack_ts == "1787260617.540319"
        assert await _drops(factory, run_id) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_later_concluding_verdict_supersedes_the_earlier_one(engine):
    """`max_thread_messages=12` gives a thread exactly one CONCLUDE turn today,
    but `thread_guidance` renders CONCLUDE for every ordinal above 11 — so raising
    that setting would hand one interview a run of concluding turns.

    The LAST verdict is the verdict, not the first: the earlier one was formed on a
    shorter record (fewer PI answers, possibly fewer specialist consults), and
    production's own duplicate cleanup kept the newest row per thread for exactly
    that reason. One row survives either way — the interview never ends up with
    two — and the superseded verdict leaves a `duplicate_thread_verdict` drop
    behind so the trail outlives the row."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(3), "1.1", closes_thread=False,
        )
        thread.message_count += 2
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=False,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "one interview, one verdict"
        assert rows[0].slack_ts == "2.2", "the LATER verdict is the one of record"
        drops = await _drops(factory, run_id)
        assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
        assert "superseded" in (drops[0].detail or "")
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_re_capturing_the_same_turn_is_still_a_duplicate(engine):
    """Supersession is for a strictly LATER turn. The same turn captured twice —
    same ordinal, same reply — is the duplicate the gate was built for, and it is
    still refused rather than allowed to rewrite the row."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(3), "1.1", closes_thread=False,
        )
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "1.1", closes_thread=False,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].slack_ts == "1.1", "the stored verdict is untouched"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_closed_interview_s_verdict_cannot_be_superseded(engine):
    """A verdict whose reply CLOSED the thread is final: the interview is over, so
    any further sidecar on that thread is a re-capture, whatever its ordinal."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_DECIDE_COUNT)
    try:
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(3, closing=True), "1.1",
            closes_thread=True,
        )
        thread.message_count += 4
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4, closing=True), "2.2",
            closes_thread=True,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].slack_ts == "1.1", "the closing verdict stands"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
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
            agent, _thread(_CONCLUDE_COUNT, "t1"), _reply_with_sidecar(3), "1.1",
            closes_thread=False,
        )
        await sim._capture_hub_assessment(
            agent, _thread(_DECIDE_COUNT, "t2"), _reply_with_sidecar(4, closing=True),
            "2.2", closes_thread=True,
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
    treating the thread as un-assessed would let the same turn's verdict through
    twice and land BOTH — the duplicate this whole gate exists to prevent.

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
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(3), "1.1", closes_thread=False,
        )
        assert len(sim._pending_assessments) == 1

        sim.simulation_run_id = run_id
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "1.1", closes_thread=False,
        )

        assert await _assessments(factory, run_id) == [], "no second verdict landed"
        assert len(sim._pending_assessments) == 1, "the queued row is untouched"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_supersession_also_retires_a_verdict_still_on_the_retry_queue(engine):
    """Last-write-wins has to reach the retry queue too. A superseded verdict left
    queued would be flushed minutes later and recreate exactly the duplicate the
    supersession just removed — the queue is drained from the main loop and from
    `stop()`, long after this turn."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        sim.simulation_run_id = uuid.uuid4()   # forces the queue path
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(3), "1.1", closes_thread=False,
        )
        assert len(sim._pending_assessments) == 1

        sim.simulation_run_id = run_id
        thread.message_count += 2
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4, closing=True), "2.2",
            closes_thread=True,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].slack_ts == "2.2", "the closing verdict is the one of record"
        assert sim._pending_assessments == [], "the superseded row left the queue"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
    finally:
        await _delete_run(factory, run_id)
