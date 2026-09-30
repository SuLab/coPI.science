"""Spec P0-08: the engine claims each thread immediately before posting its
headline, so it can never double-post against the repair script (SA4-06), and a
transport error leaves an IN DOUBT claim instead of a later duplicate."""
import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.state import ThreadState
from src.models import OpportunityAssessment
from src.services.headline_claims import claim_thread
from tests.integration.test_assessment_headline_delivery import (
    _headlines,
    _wire_summary_channel,
)
from tests.integration.test_hub_assessment_capture_gate import (
    _assessments,
    _delete_run,
    _drive_reply,
    _hub,
    _new_run,
    _reply_with_sidecar,
)
from tests.integration.test_review_supersession import _FailOnceFactory, _no_seed

pytestmark = pytest.mark.integration

_CONCLUDE_COUNT = 11


async def test_a_capture_claims_then_marks_posted(engine, monkeypatch):
    sim, _agent, _thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        [row] = await _assessments(factory, run_id)
        assert row.summary_claimed_at is not None and row.summary_posted_at is not None
        assert len(_headlines(client)) == 1
    finally:
        await _delete_run(factory, run_id)


def _summary_post_raises(sim):
    _wire_summary_channel(sim)
    client = sim.slack_clients["blackbird"]
    real = client.apost_message

    async def flaky(channel, text, **kw):
        if channel == ASSESSMENTS_SUMMARY_CHANNEL:
            raise ConnectionResetError("reset after send")
        return await real(channel, text, **kw)

    client.apost_message = flaky


async def test_a_transport_error_keeps_the_claim_in_doubt(engine, monkeypatch):
    """Review Focus 3."""
    sim, _agent, _thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_summary_post_raises,
    )
    try:
        [row] = await _assessments(factory, run_id)
        assert row.summary_claimed_at is not None and row.summary_posted_at is None
        assert sim.headlines.is_announced("t1") is True, "never retried automatically"
        assert sim._in_doubt_headlines == ["t1"]
        await sim.stop()
        [row] = await _assessments(factory, run_id)
        assert row.summary_posted_at is None, "the stop sweep did not re-post an in-doubt headline"
        assert _headlines(client) == []
    finally:
        await _delete_run(factory, run_id)


def _summary_post_refused(sim):
    _wire_summary_channel(sim)
    client = sim.slack_clients["blackbird"]
    real = client.apost_message
    state = {"refused": False}

    async def refuse_once(channel, text, **kw):
        if channel == ASSESSMENTS_SUMMARY_CHANNEL and not state["refused"]:
            state["refused"] = True
            return None  # Slack answered and refused: a definite failure
        return await real(channel, text, **kw)

    client.apost_message = refuse_once


async def test_a_definite_failure_releases_the_claim_for_a_later_post(engine, monkeypatch):
    sim, _agent, _thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_summary_post_refused,
    )
    try:
        [row] = await _assessments(factory, run_id)
        assert row.summary_claimed_at is None and row.summary_posted_at is None
        assert sim.headlines.is_announced("t1") is False
        await sim.stop()
        [row] = await _assessments(factory, run_id)
        assert row.summary_posted_at is not None
        assert len(_headlines(client)) == 1
    finally:
        await _delete_run(factory, run_id)


async def test_a_script_held_claim_blocks_the_engine(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, _hub_agent = _hub(factory, run_id)
    _wire_summary_channel(sim)
    try:
        async with factory() as db:
            db.add(OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird", subject_agent_id="gordy",
                channel_name="single-cell-omics", thread_id="t9", slack_ts="9.1",
                recommendation="conditional", scores={},
            ))
            await db.commit()
        async with factory() as db:
            assert await claim_thread(db, run_id, "t9")  # the script holds it
        assert await sim._announce_owed_headline("t9", trigger="test") is False
        assert _headlines(sim.slack_clients["blackbird"]) == []
    finally:
        await _delete_run(factory, run_id)


async def _two_verdict_interview(engine, *, first_posted: bool):
    """A provisional verdict at ordinal 8 (row A), then a terminal ordinal-12
    verdict whose FIRST write fails and is queued (row B)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, hub = _hub(factory, run_id)
    _wire_summary_channel(sim)
    sim._seed_consults_from_db = _no_seed
    failing = _FailOnceFactory(factory)
    sim.session_factory = failing
    thread = ThreadState(
        thread_id="t1", channel="single-cell-omics", other_agent_id="gordy", message_count=7,
    )
    hub.state.active_threads["t1"] = thread
    await sim._capture_hub_assessment(
        hub, thread, _reply_with_sidecar(3), "100.000001", closes_thread=False,
    )
    if first_posted:
        async with factory() as db:
            await db.execute(
                update(OpportunityAssessment)
                .where(OpportunityAssessment.simulation_run_id == run_id)
                .values(summary_posted_at=func.now())
            )
            await db.commit()
        sim.headlines.mark_announced("t1")
    thread.message_count = 11
    failing.armed = True
    await sim._capture_hub_assessment(
        hub, thread, _reply_with_sidecar(4), "100.000002", closes_thread=False,
    )
    assert failing.armed is False, "control: the terminal verdict's first write failed"
    assert len(sim._pending_assessments) == 1
    return sim, factory, run_id


async def test_a_failed_first_persist_gives_exactly_one_headline_across_a_resume(engine):
    """Spec P0-08 test: the queued replacement is never claimed at capture."""
    sim, factory, run_id = await _two_verdict_interview(engine, first_posted=False)
    try:
        assert _headlines(sim.slack_clients["blackbird"]) == [], "no post from a queued verdict"
        assert sim.headlines.is_announced("t1") is False
        await sim._flush_pending_assessments()
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 1

        resumed, _ = _hub(factory, run_id)
        _wire_summary_channel(resumed)
        await resumed._rehydrate_assessed_threads()
        await resumed.stop()
        assert _headlines(resumed.slack_clients["blackbird"]) == []
        [row] = await _assessments(factory, run_id)
        assert row.summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


async def test_a_queued_replacement_inherits_the_posted_stamp(engine):
    """Review Focus 4: the posted stamp survives the queued replacement.

    Supersede is in place (one row per run and thread, migration 0055), so the
    stamp stays on the landed row and the flush's upsert keeps it (COALESCE)
    while it applies the replacement verdict."""
    sim, factory, run_id = await _two_verdict_interview(engine, first_posted=True)
    try:
        [before] = await _assessments(factory, run_id)
        assert before.summary_posted_at is not None
        await sim._flush_pending_assessments()
        assert sim._pending_assessments == []
        [row] = await _assessments(factory, run_id)
        assert row.id == before.id
        assert row.verdict_revision == (before.verdict_revision or 1) + 1, "replacement applied"
        assert row.summary_posted_at == before.summary_posted_at
        assert sim.headlines.is_announced("t1") is True

        resumed, _ = _hub(factory, run_id)
        _wire_summary_channel(resumed)
        await resumed._rehydrate_assessed_threads()
        await resumed.stop()
        assert _headlines(sim.slack_clients["blackbird"]) == []
        assert _headlines(resumed.slack_clients["blackbird"]) == []
    finally:
        await _delete_run(factory, run_id)
