"""E1: consecutive empty replies abandon an interview; the abandonment must leave a row.

A single empty reply is NOT a loss: `has_pending_reply` stays True and the next
Phase-4 pass retries the same ordinal. The loss happens at the back-off
(`empty_response_count >= 2`), which abandons the interview — at ANY ordinal,
not just CONCLUDE: run 076e80b6 stranded a thread at message count 2.
Setup mirrors `_drive_reply` in test_hub_assessment_capture_gate.py.

The back-off used to leave the thread status="active" in both participants'
`active_threads` as well, which hung run 652a6102 (19 of 20 verdicts, 13.5 h,
never able to end). Migration 0063 made every terminal abandon branch close the
thread with outcome `abandoned`; the close, and the headline it must NOT
publish, are pinned below.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.engine.headlines import should_announce
from src.agent.engine.helpers import _HeldVerdict
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, SimulationRun, ThreadDecision
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration

_CONCLUDE_COUNT = 11  # prior count 11 -> ordinal 12, the CONCLUDE turn
_EXPLORE_COUNT = 1    # prior count 1  -> ordinal 2, the measured abandonment


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


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
            .order_by(ThreadDecision.decided_at)
        )).scalars().all()


async def _delete_run(factory, run_id):
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)
            await cleanup.commit()


async def _stranded_thread(
    engine, monkeypatch, *, prior_messages, role="scout_hub",
    with_partner=False, reply_text="",
):
    """An engine + thread ready for `_reply_to_thread`, with the model scripted
    to return NOTHING (or ``reply_text``). Returns before driving, so a test can
    drive the same thread twice — the back-off needs two consecutive empties.

    ``with_partner`` puts the other side of the interview on the roster, with
    its own ``ThreadState`` for the same thread, so a test can assert what the
    close does to BOTH participants' ``active_threads``.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    agent = Agent("blackbird", "BlackbirdBot", "Blackbird", role=role)
    agents = [agent]
    clients = {"blackbird": FakeSlackClient(agent_id="blackbird")}
    partner = None
    if with_partner:
        partner = Agent("gordy", "GordyBot", "Gordy", role="pi_lab")
        agents.append(partner)
        clients["gordy"] = FakeSlackClient(agent_id="gordy")
    sim = SimulationEngine(
        agents=agents, slack_clients=clients,
        session_factory=factory, simulation_run_id=run_id,
    )
    thread = ThreadState(
        thread_id="t1", channel="single-cell-omics", other_agent_id="gordy",
        message_count=prior_messages, has_pending_reply=True,
    )
    agent.state.active_threads["t1"] = thread
    if partner is not None:
        partner.state.active_threads["t1"] = ThreadState(
            thread_id="t1", channel="single-cell-omics",
            other_agent_id="blackbird", message_count=prior_messages,
        )
    for i in range(prior_messages):
        ts = "t1" if i == 0 else f"t1.{i}"
        sim.message_log.append(LogEntry(
            ts=ts, channel="single-cell-omics",
            sender_agent_id="gordy" if i % 2 == 0 else "blackbird",
            sender_name="GordyBot" if i % 2 == 0 else "BlackbirdBot",
            content=f"prior interview message {i}",
            thread_ts=None if i == 0 else "t1",
            posted_at=float(i), slack_ts=ts, slack_channel_id="C_OMICS",
        ))
    monkeypatch.setattr(agent, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _scripted_reply(**kwargs):
        return reply_text

    monkeypatch.setattr(
        "src.agent.engine.deps.generate_with_tools", _scripted_reply
    )
    return sim, agent, thread, factory, run_id


async def test_two_empty_replies_at_conclude_record_one_drop(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        rows = await _drops(factory, run_id)
        assert [r.reason for r in rows] == ["empty_reply"], (
            "one abandonment, one row — not one per empty reply"
        )
        assert rows[0].subject_agent_id == "gordy"
        assert rows[0].thread_id == "t1"
        assert thread.has_pending_reply is False, "the back-off must still fire"
    finally:
        await _delete_run(factory, run_id)


async def test_a_single_empty_reply_is_retried_not_recorded(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)

        assert await _drops(factory, run_id) == []
        assert thread.has_pending_reply is True, (
            "one empty reply is retryable — recording it would be a false loss"
        )
    finally:
        await _delete_run(factory, run_id)


async def test_a_mid_interview_abandonment_is_also_recorded(engine, monkeypatch):
    # The measured incident class: run 076e80b6 stranded a thread at count=2.
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_EXPLORE_COUNT,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        rows = await _drops(factory, run_id)
        assert [r.reason for r in rows] == ["empty_reply"]
    finally:
        await _delete_run(factory, run_id)


async def test_a_lab_agent_back_off_records_no_drop(engine, monkeypatch):
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_CONCLUDE_COUNT, role="pi_lab",
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        assert await _drops(factory, run_id) == [], (
            "labs never owe a verdict; their back-off is not an assessment loss"
        )
    finally:
        await _delete_run(factory, run_id)


_SIDECAR_ONLY_REPLY = (
    "<slack_message><assessment_json>{\"recommendation\": \"pass\"}"
    "</assessment_json></slack_message>"
)


async def test_the_empty_reply_back_off_closes_the_interview_for_both_sides(
    engine, monkeypatch,
):
    """0063: the abandon is a CLOSE, not just a cleared `has_pending_reply`.

    Before it, the thread stayed `status="active"` in both participants'
    `active_threads` with nothing able to re-queue it, so
    `_open_interview_count()` never reached 0 and a `--max-proposals` run could
    never end (run 652a6102).
    """
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_EXPLORE_COUNT, with_partner=True,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        assert "t1" not in agent.state.active_threads, (
            "the abandoning agent still holds the interview open"
        )
        assert "t1" not in sim.agents["gordy"].state.active_threads, (
            "the PARTNER still holds the interview open — _close_thread clears "
            "both sides, and only clearing one still hangs the drain"
        )
        assert sim._open_interview_count() == 0

        rows = await _decisions(factory, run_id)
        assert [(r.thread_id, r.outcome, r.closed_by_role) for r in rows] == [
            ("t1", "abandoned", None)
        ], "one ThreadDecision, outcome `abandoned`, triggered by no reply"
        assert len(await _drops(factory, run_id)) == 1, (
            "closing the thread must not add a second assessment_drops row"
        )
    finally:
        await _delete_run(factory, run_id)


async def test_the_suppressed_post_back_off_closes_the_interview(engine, monkeypatch):
    """The suppressed-post branch LOOKS recoverable and is not.

    `suppressed_post_count` clears only after a successful post, and the
    counterparty cannot post again either (its own `has_pending_reply` was
    cleared and the suppressed text never reached the log), so both sides go
    quiet. Driven through the real suppression: the model body strips to empty
    once `_post_message` removes the sidecar and the `<slack_message>` tags.
    """
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_EXPLORE_COUNT, with_partner=True,
        reply_text=_SIDECAR_ONLY_REPLY,
    )
    try:
        await sim._reply_to_thread(agent, thread)
        assert thread.suppressed_post_count == 1, (
            "the reply must reach _post_message and be suppressed there, not "
            "be caught by the empty-response check before it"
        )
        await sim._reply_to_thread(agent, thread)

        assert "t1" not in agent.state.active_threads
        assert "t1" not in sim.agents["gordy"].state.active_threads
        assert sim._open_interview_count() == 0, (
            "two suppressed posts strand the interview exactly as two empty "
            "replies do; it must be closed, not left active"
        )
        rows = await _decisions(factory, run_id)
        assert [r.outcome for r in rows] == ["abandoned"]
    finally:
        await _delete_run(factory, run_id)


async def test_an_abandoned_interview_publishes_no_headline(engine, monkeypatch):
    """Operator decision, 2026-10-09: an abandon announces NOTHING.

    A verdict still held when the engine gives up came from an ordinal the
    interview was still arguing with, and a Slack headline cannot be retracted.
    The verdict stays on the row for the web UI; only the headline is withheld.
    """
    sim, agent, thread, factory, run_id = await _stranded_thread(
        engine, monkeypatch, prior_messages=_EXPLORE_COUNT, with_partner=True,
    )
    try:
        # A provisional (non-final) verdict from an earlier ordinal: exactly
        # what `_close_thread`'s "thread-close" trigger WOULD have enqueued.
        sim._assessed_threads["t1"] = _HeldVerdict(
            ordinal=2, final=False, slack_ts="t1.1",
        )
        await sim._reply_to_thread(agent, thread)
        await sim._reply_to_thread(agent, thread)

        assert "t1" not in agent.state.active_threads, "the close must still happen"
        assert sim._pending_headlines == [], (
            "an abandoned interview must not queue its provisional verdict's "
            "#assessments-summary headline"
        )
    finally:
        await _delete_run(factory, run_id)


def test_the_abandon_trigger_never_announces():
    assert should_announce(
        trigger="thread-abandon", already_announced=False,
    ) is False
