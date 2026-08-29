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
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine, _HeldVerdict
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


async def _drive_reply(
    engine, monkeypatch, raw_response, *, prior_messages,
    configure: Callable[[SimulationEngine], None] | None = None,
):
    """Drive the REAL path: `_reply_to_thread` on a live engine, so the close
    decision reaches the capture gate the way production computes it.

    ``prior_messages`` is seeded into the engine's real `MessageLog`, not just
    onto `ThreadState.message_count`: `_reply_to_thread` overwrites that field
    with ``len(get_thread_history(thread_id))`` before the phase is computed, so a
    ThreadState built with ``message_count=11`` over an EMPTY log is an ordinal-1
    EXPLORE turn, not the CONCLUDE turn it looks like (CLAUDE.md's warning, and
    the bug 81dbe44 found in both existing harnesses).

    ``configure``, if given, runs on the freshly-built engine BEFORE
    `_reply_to_thread` — a seam for tests that need engine state (e.g. the
    assessments-summary channel wiring) in place before the capture path runs
    INSIDE this call. Calling it only after this function returns would be too
    late: `_reply_to_thread` (and any headline it posts) has already happened.

    Returns ``(sim, agent, thread, client, factory, run_id)``.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    if configure is not None:
        configure(sim)
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


# ---------------------------------------------------------------------------
# Supersession keeps what it deletes, and deletes only what it means to
#
# `_retire_superseded_verdict` DELETEs a row and records a
# `duplicate_thread_verdict` drop. The refusal path a few lines above it passes
# `raw_verdict` under a comment saying a refusal "is never a licence to destroy
# it" — supersession was the one path that both deleted a row and kept nothing,
# so the earlier verdict (its score, its rationale, its red flags) existed
# nowhere afterwards. See `AssessmentDrop.raw_verdict`.
#
# The other half is what the DELETE must NOT match. `_capture_hub_assessment`
# reads `superseded` BEFORE the replacement is persisted and retires AFTER, so
# the replacement is already committed on the same run, agent and thread by the
# time this runs. A DELETE keyed on the thread would take both and end the
# interview with ZERO assessments while logging success; `slack_ts` is
# load-bearing precisely because it is the one field that differs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_superseded_verdict_is_recoverable_from_its_drop_row(engine):
    """The drop is the only trace the retired row leaves, so it has to carry the
    verdict itself — not just a sentence saying one was superseded.

    The two verdicts differ in every dimension score (3s vs 4s), so recovering
    the WRONG one is distinguishable from recovering none.
    """
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
        assert len(rows) == 1 and rows[0].slack_ts == "2.2"
        drops = await _drops(factory, run_id)
        assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
        assert drops[0].raw_verdict == _verdict(3), (
            "the retired verdict must survive its own deletion, exactly as the "
            "model emitted it — and it must be the EARLIER one, not the "
            "replacement"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_supersession_does_not_delete_the_replacement_row(engine):
    """The trap in re-keying the DELETE on `thread_id`.

    Both rows share the run, the agent AND the thread — the replacement is
    already committed when the retire runs — so any predicate that does not
    include `slack_ts` matches both and leaves the interview with nothing. This
    asserts the surviving row positively rather than only counting rows: a
    DELETE that took both would show `len(rows) == 0`, and one that took neither
    would show 2.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(3), "1.1", closes_thread=False,
        )
        first = await _assessments(factory, run_id)
        assert len(first) == 1 and first[0].thread_id == "t1", (
            "the provisional row is written against this thread — which is what "
            "makes a thread-keyed DELETE match it AND its replacement"
        )

        thread.message_count += 2
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=True,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "the interview must never end holding zero verdicts"
        assert rows[0].slack_ts == "2.2"
        assert rows[0].thread_id == "t1"
        assert rows[0].scores["differentiation_unmet_need"] == 4, "the LATER verdict survives"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_null_slack_ts_does_not_poison_the_retry_queue_filter(engine):
    """A held verdict with no `slack_ts` must not sweep the retry queue.

    The queue filter matches `row.get("slack_ts") == superseded.slack_ts`. With
    `None` on the right that matches EVERY queued assessment that has no Slack
    ts of its own — other interviews' verdicts included — and those rows are
    dropped from `_pending_assessments` and never written. Rehydrated verdicts
    (`_rehydrate_assessed_threads`) are exactly where a `None` slack_ts comes
    from, so this is reachable, not theoretical.

    The unrelated queued row below belongs to a DIFFERENT thread and has no
    slack_ts — the shape that used to be swept.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    other_run_row = {
        "simulation_run_id": run_id, "agent_id": "blackbird",
        "subject_agent_id": "hart", "channel_name": "general",
        "slack_ts": None, "thread_id": "t-elsewhere",
    }
    sim._pending_assessments.append(other_run_row)
    # A verdict this process only knows about because it read it back off the
    # table at startup: no slack_ts on the row, so none on the held record.
    sim._assessed_threads["t1"] = _HeldVerdict(
        ordinal=0, final=False, slack_ts=None, announced=False,
    )
    try:
        thread.message_count += 2
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=True,
        )

        assert sim._pending_assessments == [other_run_row], (
            "another interview's queued verdict must survive a supersession it "
            "has nothing to do with"
        )
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1 and rows[0].slack_ts == "2.2"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# `_assessed_threads` survives a restart
#
# The map is process-local, so before `thread_id` was stored the table could not
# say WHICH interview a verdict came from and a restarted process started blind:
# the interview's own later turn looked like a first verdict and landed a second
# row, and a lab bot closing a thread that already held a verdict produced a
# spurious `closed_before_verdict` drop.
# ---------------------------------------------------------------------------


async def _seed_assessment(factory, run_id, *, thread_id, slack_ts, subject="gordy"):
    async with factory() as db:
        db.add(OpportunityAssessment(
            simulation_run_id=run_id, agent_id="blackbird",
            subject_agent_id=subject, channel_name="single-cell-omics",
            thread_id=thread_id, slack_ts=slack_ts,
            recommendation="conditional",
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_assessed_threads_is_rehydrated_after_a_restart(engine):
    """Every field of the rehydrated record is a decision, and three of them are
    decisions about which way to fail.

    * `ordinal=0` — any guess at or above the real ordinal makes
      `_sidecar_refusal` refuse the interview's legitimate later verdict
      (`if ordinal <= held.ordinal`). Zero costs at most a spurious
      `duplicate_thread_verdict` drop if the same turn is re-captured.
    * `announced=False` — `True` would suppress the `#assessments-summary`
      headline for a verdict stored provisionally before the restart, which is a
      silent D12 breach. `False` merely repeats today's behaviour.
    * `final` is DERIVED, not guessed: a closed thread has a `ThreadDecision`,
      so `final = thread_id in self._closed_thread_ids`. `final=True` as a
      "conservative" default would be the worst of the three — `_sidecar_refusal`
      refuses everything on a final thread, so the interview's own concluding
      verdict would be refused and only its `raw_verdict` would survive on a
      drop row.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    other_run_id = await _new_run(factory)
    await _seed_assessment(factory, run_id, thread_id="t-open", slack_ts="1.1")
    await _seed_assessment(factory, run_id, thread_id="t-closed", slack_ts="2.2")
    await _seed_assessment(factory, run_id, thread_id=None, slack_ts="3.3")
    await _seed_assessment(
        factory, other_run_id, thread_id="t-other-run", slack_ts="4.4",
    )
    sim, _agent = _hub(factory, run_id)
    # What `_rebuild_agent_state` leaves behind: closing a thread writes a
    # `ThreadDecision`, and that set is the only evidence of it this method has.
    sim._closed_thread_ids.add("t-closed")
    try:
        await sim._rehydrate_assessed_threads()

        assert set(sim._assessed_threads) == {"t-open", "t-closed"}, (
            "a row with no thread_id cannot be placed, and another run's rows "
            "are not this run's interviews"
        )
        held = sim._assessed_threads["t-open"]
        assert held.ordinal == 0
        assert held.final is False
        assert held.announced is False
        assert held.slack_ts == "1.1"
        assert sim._assessed_threads["t-closed"].final is True, (
            "a thread with a ThreadDecision is closed; nothing may supersede it"
        )
    finally:
        await _delete_run(factory, run_id)
        await _delete_run(factory, other_run_id)


def test_rehydration_runs_after_the_thread_decisions_are_loaded():
    """`final` is derived from `_closed_thread_ids`, which `_rebuild_agent_state`
    populates — so rehydrating first would mark every restored verdict
    non-final, including the ones whose interview is already over.

    Asserted on `SimulationEngine.start`'s source because the alternative is
    booting the whole engine (Slack reconcile, message-log hydration, roster
    sync) to observe an ordering that is a one-line invariant.
    """
    import inspect

    source = inspect.getsource(SimulationEngine.start)
    # The CALL expressions, not the bare names. Mutation-tested: deleting the
    # `await self._rehydrate_assessed_threads()` line left this test GREEN when
    # it matched on the name alone, because the comment ABOVE that line names
    # the method too — so the assertion was satisfied by prose describing a call
    # that no longer existed.
    rebuild = "await self._rebuild_agent_state()"
    rehydrate = "await self._rehydrate_assessed_threads()"
    assert rebuild in source
    assert rehydrate in source
    assert source.index(rebuild) < source.index(rehydrate)


@pytest.mark.asyncio
async def test_a_rehydrated_verdict_is_superseded_rather_than_duplicated(engine):
    """The behaviour rehydration exists for, end to end: a process that restarts
    mid-interview and then reaches the concluding turn must end with ONE row —
    the later one — instead of two."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _seed_assessment(factory, run_id, thread_id="t1", slack_ts="1.1")
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        await sim._rehydrate_assessed_threads()
        assert "t1" in sim._assessed_threads

        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=True,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "the pre-restart row was retired, not duplicated"
        assert rows[0].slack_ts == "2.2"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_superseded_row_that_cannot_be_found_says_so(engine, caplog):
    """"No row to copy" and "the SELECT never ran" must not look identical.

    `_superseded_raw_verdict` returns None for four different reasons and only
    one of them — an exception — was logged. On the single path whose stated
    purpose is "never lose the retired verdict", a drop row with
    `raw_verdict IS NULL` and a silent log leaves an operator unable to tell
    whether the copy was attempted and found nothing or was skipped entirely.

    Driven by holding a verdict whose `slack_ts` matches no stored row, which is
    what a rehydrated record pointing at a row a later cleanup removed looks
    like.
    """
    import logging

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    sim._assessed_threads["t1"] = _HeldVerdict(
        ordinal=0, final=False, slack_ts="no-such-ts", announced=False,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
            await sim._capture_hub_assessment(
                agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=True,
            )

        drops = await _drops(factory, run_id)
        assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
        assert drops[0].raw_verdict is None, "there was genuinely nothing to copy"
        assert any(
            "no stored row" in record.message or "no stored row" in record.getMessage()
            for record in caplog.records
        ), "the not-found branch must announce itself"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_superseded_row_with_a_null_verdict_says_so(engine, caplog):
    """The FIFTH silent-None case: the row IS found, but `raw_verdict` is NULL.

    `_superseded_raw_verdict`'s docstring listed four ways it answers `None` and
    warned on the one that matters (not-found). It missed a fifth: a row that
    matches the filter but stores a NULL `raw_verdict` makes `rows[0]` itself
    `None`, and the function returned it with no log at all. That is the same
    indistinguishability the not-found warning exists to end, on the same path
    whose stated purpose is "never lose the retired verdict" — and it is the
    SHAPE OF EVERY ROW WRITTEN BEFORE 0035, so it is the likely case on any
    restart that rehydrates an older run.

    `_seed_assessment` leaves `raw_verdict` NULL, which is exactly the fixture.
    """
    import logging

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    await _seed_assessment(factory, run_id, thread_id="t1", slack_ts="1.1")
    sim, agent = _hub(factory, run_id)
    thread = _thread(_CONCLUDE_COUNT)
    try:
        await sim._rehydrate_assessed_threads()
        assert "t1" in sim._assessed_threads

        with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
            await sim._capture_hub_assessment(
                agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=True,
            )

        drops = await _drops(factory, run_id)
        assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
        assert drops[0].raw_verdict is None
        messages = [r.getMessage() for r in caplog.records]
        assert any("raw_verdict" in m and "NULL" in m for m in messages), (
            "a stored row with a NULL raw_verdict produced a drop row that "
            "cannot carry the verdict, and said nothing about it: "
            f"{messages}"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_rehydration_logs_the_number_of_threads_it_restored(engine, caplog):
    """The log said `len(self._assessed_threads)` where `len(rows)` was meant.

    They are equal only while every thread has exactly one row — which is
    precisely the invariant this whole mechanism exists because production
    BROKE (one pearce interview held three). So the number diverges exactly when
    an operator is reading the log to find out how bad the duplication is.
    """
    import logging

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    # Three rows, two threads: the historical duplication, reproduced.
    await _seed_assessment(factory, run_id, thread_id="t-a", slack_ts="1.1")
    await _seed_assessment(factory, run_id, thread_id="t-a", slack_ts="1.2")
    await _seed_assessment(factory, run_id, thread_id="t-b", slack_ts="2.1")
    sim, _agent = _hub(factory, run_id)
    try:
        with caplog.at_level(logging.INFO, logger="src.agent.simulation"):
            await sim._rehydrate_assessed_threads()

        assert len(sim._assessed_threads) == 2
        messages = [r.getMessage() for r in caplog.records]
        # Both numbers, and they must be the right way round: 3 rows read, 2
        # interviews restored. Asserting the pair rather than just the row count
        # is what stops the fix from being "swap the variable" and reintroducing
        # the same ambiguity in the other direction.
        assert any(
            "Rehydrated 3 stored verdict(s) across 2 interview(s)" in m
            for m in messages
        ), f"expected 3 rows across 2 interviews; got {messages}"
    finally:
        await _delete_run(factory, run_id)
