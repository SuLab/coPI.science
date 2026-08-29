"""Every assessment whose interview has ENDED gets exactly one
#assessments-summary headline, exactly once, durably.

See docs/audits/2026-08-29-lost-assessment-headlines/README.md. Production run
61ccad6d lost the rothstein verdict's headline (conditional, 2.85 — the run's
highest score) because the interview ended by `max_thread_messages` timeout
instead of by a terminal reply, and nothing announces on that path.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.message_log import LogEntry
from src.models import OpportunityAssessment, SimulationRun
from tests.integration.test_hub_assessment_capture_gate import (  # noqa: F401
    _assessments,
    _delete_run,
    _drive_reply,
    _reply_with_sidecar,
)

pytestmark = pytest.mark.integration

# `phase4_guidance` takes the ORDINAL (message_count + 1). Seeding N prior
# messages makes the generated reply ordinal N+1.
_CONCLUDE_COUNT = 11     # ordinal 12 — the hub's own concluding turn
_LAST_DECIDE_COUNT = 10  # ordinal 11 — the turn that lost rothstein's headline


def _wire_summary_channel(sim):
    """Without this a headline is skipped for an unrelated reason
    (`channel_id=None, transport not connected`) and a delivery test passes
    while proving nothing. Production fills this in via
    `_ensure_assessments_summary_channel`. Must be applied via `_drive_reply`'s
    `configure=` seam — BEFORE `_reply_to_thread` runs internally — or it is
    wired too late to affect the drive it is meant to cover."""
    sim._assessments_summary_channel_id = "C-SUMMARY"
    sim._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = "C-SUMMARY"
    sim._channel_id_map["single-cell-omics"] = "C_OMICS"


def _headlines(client):
    return [p for p in client.posted if p.get("channel") == ASSESSMENTS_SUMMARY_CHANNEL]


@pytest.mark.asyncio
async def test_summary_posted_at_defaults_to_null(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
        run_id = run.id
    try:
        async with factory() as db:
            row = OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t1",
            )
            db.add(row)
            await db.commit()
            row_id = row.id
        async with factory() as db:
            stored = (await db.execute(
                select(OpportunityAssessment).where(OpportunityAssessment.id == row_id)
            )).scalar_one()
            assert stored.summary_posted_at is None, (
                "a fresh verdict has not been announced"
            )
    finally:
        async with factory() as db:
            stale = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await db.delete(stale)
                await db.commit()


@pytest.mark.asyncio
async def test_a_posted_headline_is_recorded_on_the_row(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        # The ordinal-12 reply announces on its own — the wiring landed before
        # `_reply_to_thread` ran, so `_post_assessment_summary` had a connected
        # transport and channel id to post through.
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert len(_headlines(client)) == 1, "the CONCLUDE turn announces"
        assert rows[0].summary_posted_at is not None, (
            "a posted headline is recorded durably, so a restart cannot re-post it"
        )
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# Announcement is a property of the interview ENDING, not of one lucky reply.
# ---------------------------------------------------------------------------


def _pi_takes_the_conclude_slot(sim):
    """Message 12 of 12, written by the PI — so the hub never gets an
    ordinal-12 turn of its own and the next tick closes the thread as full.

    This is the exact shape of production run 61ccad6d's rothstein interview: a
    4181-char PI reply was split into two Slack messages, which spent the
    thread's last slot and locked the hub out of its own CONCLUDE turn.
    """
    sim.message_log.append(LogEntry(
        ts="t1.pi12", channel="single-cell-omics",
        sender_agent_id="gordy", sender_name="GordyBot",
        content="the PI's ordinal-12 reply", thread_ts="t1",
        posted_at=99.0, slack_ts="t1.pi12", slack_channel_id="C_OMICS",
    ))


@pytest.mark.asyncio
async def test_a_timed_out_interview_still_announces_its_verdict(engine, monkeypatch):
    """Production run 61ccad6d, rothstein: the hub concluded at ordinal 11
    (DECIDE, because a 4181-char PI reply had been split into two Slack
    messages), the thread hit `max_thread_messages` one second later, and the
    headline was lost forever."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_LAST_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert _headlines(client) == [], "an ordinal-11 verdict is provisional"

        # The PI takes ordinal 12, the single CONCLUDE slot.
        _pi_takes_the_conclude_slot(sim)

        # The hub's next turn finds the thread full and closes it as `timeout`
        # without generating any reply at all.
        thread.has_pending_reply = True
        agent.state.active_threads["t1"] = thread
        await sim._reply_to_thread(agent, thread)
        assert thread.status == "closed"

        await sim._drain_pending_headlines()

        assert len(_headlines(client)) == 1, (
            "an interview that ENDS announces the verdict it holds"
        )
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_declined_interview_announces_exactly_once(engine, monkeypatch):
    """The ⏸️ path already announces inside `_capture_hub_assessment`, before
    `_check_thread_outcome` closes the thread. The close hook must not add a
    second headline.

    The `configure=` wiring is what makes this test say what it claims. Wired
    only after `_drive_reply` returned, the in-turn post would fail for an
    unrelated reason (`channel_id=None`), `announced` would be reset to False,
    and the drain would RESCUE the headline — one headline, from the path this
    test exists to prove is not used.
    """
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch,
        _reply_with_sidecar(closing=True), prior_messages=_LAST_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert thread.status == "closed"
        assert len(_headlines(client)) == 1, "the ⏸️ reply announced in-turn"
        assert sim._assessed_threads["t1"].announced is True
        assert sim._pending_headlines == [], (
            "an already-announced verdict is not queued for a second headline"
        )
        await sim._drain_pending_headlines()
        assert len(_headlines(client)) == 1, "exactly one, never two"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_draining_twice_does_not_post_twice(engine, monkeypatch):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_LAST_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        _pi_takes_the_conclude_slot(sim)
        thread.has_pending_reply = True
        agent.state.active_threads["t1"] = thread
        await sim._reply_to_thread(agent, thread)

        await sim._drain_pending_headlines()

        # Drop the in-memory record BEFORE re-queueing, and do not "tidy" this
        # line away. The first drain left `_assessed_threads["t1"]` with
        # `announced=True`, and `_announce_owed_headline` short-circuits on that
        # before it issues any SQL — so with the record in place the second drain
        # would return without ever touching the database, and this test would
        # pass while the guard it names went completely unexercised. Deleting it
        # forces the fall-through to the `summary_posted_at IS NULL` predicate,
        # which is the load-bearing one. It is also the realistic shape: a
        # restarted process rehydrates every verdict with `announced=False`
        # (`_rehydrate_assessed_threads`), so the DB is genuinely the only thing
        # standing between a re-queued thread and a second public headline.
        del sim._assessed_threads["t1"]
        sim._pending_headlines.append("t1")   # simulate a re-queue
        await sim._drain_pending_headlines()

        assert len(_headlines(client)) == 1, (
            "`summary_posted_at` is the at-most-once guard, not the queue"
        )
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# The run ending is the interview ending too — the shutdown sweep.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_announces_a_still_open_interview_s_verdict(
    engine, monkeypatch,
):
    """The run's timer is the end of the interview too. Production run
    61ccad6d's timer fired five minutes after rothstein's verdict was stored —
    by then the thread had already been closed, but an OPEN one is the same
    situation: no later turn is coming."""
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_LAST_DECIDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert thread.status != "closed", "the interview is still open"
        assert _headlines(client) == []

        await sim.stop()

        assert len(_headlines(client)) == 1, (
            "a run that ends announces every verdict it is holding"
        )
        rows = await _assessments(factory, run_id)
        assert rows[0].summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_shutdown_does_not_re_announce_an_already_posted_headline(
    engine, monkeypatch,
):
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert len(_headlines(client)) == 1
        await sim.stop()
        assert len(_headlines(client)) == 1, "exactly one, never two"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_failed_in_turn_post_stays_discoverable_and_is_rescued_later(
    engine, monkeypatch,
):
    """Carried finding from Task 3's review: `_capture_hub_assessment` resets
    `announced` back to False when `_post_assessment_summary` returns False on
    the CONCLUDE turn itself, so a transient Slack failure there must not
    permanently hide the verdict from the close path, the shutdown sweep, or
    the repair script — that branch had no direct test before this one.

    Seam: `_post_assessment_summary` itself, made to fail exactly once, rather
    than breaking `FakeSlackClient.apost_message`. That pins the boundary
    contract the branch actually checks (a `False` return), not one particular
    way of producing it, and needs no manual un-patching to let the later
    rescue attempt succeed.
    """

    def _wire_and_fail_the_first_post(sim):
        _wire_summary_channel(sim)
        original = sim._post_assessment_summary
        state = {"failed_once": False}

        async def flaky_once(*args, **kwargs):
            if not state["failed_once"]:
                state["failed_once"] = True
                return False
            return await original(*args, **kwargs)

        monkeypatch.setattr(sim, "_post_assessment_summary", flaky_once)

    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_and_fail_the_first_post,
    )
    try:
        assert _headlines(client) == [], "the CONCLUDE turn's post attempt failed"
        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].summary_posted_at is None, (
            "a failed post must not be recorded as posted"
        )
        assert sim._assessed_threads["t1"].announced is False, (
            "announced is reset so the verdict stays discoverable by the "
            "close path, the shutdown sweep, and the repair script"
        )

        await sim.stop()

        assert len(_headlines(client)) == 1, (
            "a failed post must never permanently hide a verdict"
        )
        rows = await _assessments(factory, run_id)
        assert rows[0].summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)
