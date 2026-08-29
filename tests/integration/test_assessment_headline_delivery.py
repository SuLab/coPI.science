"""Every assessment whose interview has ENDED gets exactly one
#assessments-summary headline, exactly once, durably.

See docs/audits/2026-08-29-lost-assessment-headlines/README.md. Production run
61ccad6d lost the rothstein verdict's headline (conditional, 2.85 — the run's
highest score) because the interview ended by `max_thread_messages` timeout
instead of by a terminal reply, and nothing announces on that path.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.message_log import LogEntry
from src.agent.simulation import _HeldVerdict
from src.models import OpportunityAssessment, SimulationRun
from tests.fakes import FakeSlackClient
from tests.integration.test_hub_assessment_capture_gate import (  # noqa: F401
    _assessments,
    _delete_run,
    _drive_reply,
    _drops,
    _hub,
    _new_run,
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
        # restarted process rebuilds `_assessed_threads` from the rows
        # (`_rehydrate_assessed_threads`) and a pre-`0041` row carries no stamp
        # to rebuild from, so the DB predicate is genuinely the only thing
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


class _SlackRefusesTheHeadline(FakeSlackClient):
    """A transport that accepts the interview reply and silently REFUSES the
    headline, returning `None` from `post_message` without raising.

    That is `AgentSlackClient.post_message`'s real contract, not an invented
    one: it ends `if not posted: return None` (src/agent/slack_client.py), and
    `apost_message` is a `to_thread` wrapper that forwards it — so
    `not_in_channel` after a failed autojoin, an archived channel,
    `invalid_auth` and a chunk failure all reach the engine as a falsy return
    and nothing else.

    Subclassed rather than teaching `tests/fakes.py::FakeSlackClient` to fail:
    that fake's truthy-dict return is depended on by every other suite.
    """

    def __init__(self, *args, refuse_channel: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._refuse_channel = refuse_channel
        self.refused: list[str] = []

    def post_message(self, channel: str, text: str, thread_ts: str | None = None):
        if channel == self._refuse_channel:
            # Recorded, but NOT appended to `posted`/`posted_messages` — Slack
            # refused it, so no such message exists for `_headlines` to find.
            self.refused.append(text)
            return None
        return super().post_message(channel, text, thread_ts)


@pytest.mark.asyncio
async def test_a_slack_refused_headline_is_never_recorded_as_posted(
    engine, monkeypatch,
):
    """Fix round 2, finding 1. `_post_assessment_summary` discarded
    `apost_message`'s return value, so a REFUSED post — which raises nothing —
    was reported as success, and `_capture_hub_assessment` then stamped
    `summary_posted_at` on a headline that never reached the channel. That
    stamp is permanent and unqualified: the close path, the shutdown sweep and
    `scripts/backfill_assessment_headlines.py` all read it as "already
    announced" (`select_rows_needing_headline` skips a stamped row outright),
    so the verdict becomes invisible to every repair path there is — the column
    silently redefined from "it posted" to "we tried".

    Driven at the TRANSPORT boundary, deliberately unlike
    `test_a_failed_in_turn_post_stays_discoverable_and_is_rescued_later`, which
    stubs `_post_assessment_summary` itself to return False. That one pins what
    the CALLER does with a False; this one pins that a swallowed Slack refusal
    produces one at all. Both are needed: with the bug in place the caller-side
    test still passed.
    """

    def _wire_and_refuse_the_headline(sim):
        _wire_summary_channel(sim)
        sim.slack_clients["blackbird"] = _SlackRefusesTheHeadline(
            agent_id="blackbird", refuse_channel=ASSESSMENTS_SUMMARY_CHANNEL,
        )

    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_and_refuse_the_headline,
    )
    try:
        # The CONCLUDE turn tried, and Slack refused. Assert the attempt
        # happened, or everything below would hold vacuously.
        assert len(client.refused) == 1, "the headline post was attempted"
        assert _headlines(client) == [], "and nothing reached the channel"

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1
        assert rows[0].summary_posted_at is None, (
            "a refused post must never be recorded as posted — a stamp here "
            "hides the verdict from the sweep and from the repair script"
        )
        assert sim._assessed_threads["t1"].announced is False, (
            "and the held verdict stays discoverable in memory too"
        )

        # The boundary contract itself: a falsy transport answer is a failure,
        # not a success with a missing side effect.
        assert await sim._post_assessment_summary(
            agent, thread, {"company_or_project": "x", "recommendation": "conditional",
                            "scores": {}}, "1.0",
        ) is False

        # Still owed, and still found: the shutdown sweep re-derives this thread
        # from `summary_posted_at IS NULL` and tries again.
        await sim.stop()
        assert len(client.refused) == 3, (
            "the sweep re-attempted the headline the refusal left owed"
        )
        rows = await _assessments(factory, run_id)
        assert rows[0].summary_posted_at is None, (
            "still un-announced after a second refusal — never stamped on a "
            "post that did not happen"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_shutdown_seeds_owed_headlines_from_the_database_not_memory(
    engine, monkeypatch,
):
    """Fix round 1, finding 1. The shutdown sweep seeds from
    `summary_posted_at IS NULL`, not from `_assessed_threads`, because the
    in-memory map can say `announced=False` over a row that is already
    stamped — and those entries sit at the FRONT of the insertion-ordered
    dict, ahead of anything genuinely owed this session. Before the DB-derived
    seed, walking `_assessed_threads` alone would queue all of them, and
    `_drain_pending_headlines` counts `drained` per POP rather than per
    successful post — so the bound-limited sweep burned its whole budget on
    no-op reads for already-public headlines and reported the genuinely owed
    verdict, queued last, as LOST.

    `_rehydrate_assessed_threads` hardcoded `announced=False` for EVERY
    reloaded verdict when this test was written; it now derives the flag from
    `summary_posted_at is not None` (commit bfbef77, same branch), which
    narrows that disagreement without ending it — a row stamped out of band
    (`backfill_assessment_headlines.py --stamp-only` against a live run) and
    every pre-`0041` row still produce exactly the state seeded below. It is
    constructed directly here rather than via a rehydrate, so the test pins
    the sweep's seed and nothing else.

    `HEADLINES_MAX_AT_SHUTDOWN` is monkeypatched down to 2 rather than
    building 26+ rows — the failure mode reproduces at any bound smaller than
    the rehydrated count, and this keeps the test fast. Against the buggy
    memory-derived seed this fails two ways at once: the owed thread is never
    announced, and it is named in a LOST error it does not deserve.
    """
    monkeypatch.setattr("src.agent.simulation.HEADLINES_MAX_AT_SHUTDOWN", 2)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    _wire_summary_channel(sim)
    client = sim.slack_clients["blackbird"]

    already_announced_ids = ["announced-1", "announced-2", "announced-3"]
    owed_thread_id = "owed-1"

    async with factory() as db:
        for thread_id in already_announced_ids:
            db.add(OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird",
                channel_name="single-cell-omics", thread_id=thread_id,
                recommendation="decline", scores={},
                summary_posted_at=datetime.now(UTC),
            ))
        db.add(OpportunityAssessment(
            simulation_run_id=run_id, agent_id="blackbird",
            channel_name="single-cell-omics", thread_id=owed_thread_id,
            recommendation="route-to-incubation", scores={},
        ))
        await db.commit()

    # The state a resume produces whenever the stamp cannot be trusted to
    # rebuild the flag (a pre-`0041` row, or one stamped out of band while this
    # process ran): every verdict of this run held with `announced=False`, the
    # already-public ones inserted FIRST — the insertion order a rehydrate
    # walking `created_at` would produce.
    for thread_id in [*already_announced_ids, owed_thread_id]:
        sim._assessed_threads[thread_id] = _HeldVerdict(
            ordinal=12, final=True, slack_ts=None, announced=False,
        )

    try:
        await sim.stop()

        assert sim._pending_headlines == [], (
            "nothing was reported LOST — the DB seed is exact, not bounded "
            "by memory order"
        )
        assert sim._assessed_threads[owed_thread_id].announced is True, (
            "the genuinely owed verdict was announced despite 3 stale "
            "rehydrated entries ahead of it and a bound of 2"
        )
        assert len(_headlines(client)) == 1, (
            "exactly the owed verdict's headline, none of the already-public ones"
        )

        async with factory() as db:
            owed_row = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.thread_id == owed_thread_id,
                )
            )).scalar_one()
            assert owed_row.summary_posted_at is not None

            for thread_id in already_announced_ids:
                stale = (await db.execute(
                    select(OpportunityAssessment).where(
                        OpportunityAssessment.thread_id == thread_id,
                    )
                )).scalar_one()
                assert stale.summary_posted_at is not None, (
                    "already-announced rows are untouched, not re-posted"
                )
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# The flag has to survive a restart and a supersession, not just a post.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydration_reads_the_durable_headline_flag(engine, monkeypatch):
    """A restart must not re-post a headline that is already public, and must
    not suppress one that never posted. Before 0041 the flag was hardcoded
    False, which got the second case right and the first case wrong."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, agent = _hub(factory, run_id)
    try:
        async with factory() as db:
            db.add(OpportunityAssessment(
                id=uuid.uuid4(), simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t-announced", slack_ts="1.0",
                summary_posted_at=datetime.now(UTC),
            ))
            db.add(OpportunityAssessment(
                id=uuid.uuid4(), simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t-owed", slack_ts="2.0",
            ))
            await db.commit()

        await sim._rehydrate_assessed_threads()

        assert sim._assessed_threads["t-announced"].announced is True
        assert sim._assessed_threads["t-owed"].announced is False
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_superseded_verdict_carries_its_headline_stamp_forward(
    engine, monkeypatch,
):
    """`announced` carries forward in memory so the channel keeps its first
    word. The COLUMN has to agree, or a restart re-announces the replacement.

    Ruling P6: driving this with a SECOND `_drive_reply` (as an earlier draft
    of this test did) is unreachable — after the first ordinal-12 reply the
    thread already holds 12 messages, so a second `_reply_to_thread` hits
    `message_count >= settings.max_thread_messages` and closes the thread as
    `timeout` instead of replying; no supersession would ever happen. Instead,
    drive the second verdict by calling `_capture_hub_assessment` directly —
    it is the exact unit under test, and owns both the `already_announced`
    carry-forward and the `_mark_summary_posted` call — with
    `thread.message_count` raised so the ordinal still lands in CONCLUDE.
    """
    sim, agent, thread, client, factory, run_id = await _drive_reply(
        engine, monkeypatch, _reply_with_sidecar(), prior_messages=_CONCLUDE_COUNT,
        configure=_wire_summary_channel,
    )
    try:
        assert len(_headlines(client)) == 1
        rows = await _assessments(factory, run_id)
        assert rows[0].summary_posted_at is not None

        # A later CONCLUDE turn supersedes it: the first row is deleted and a
        # new one takes its place on the same thread. `message_count = 13`
        # makes the next ordinal 14 — still CONCLUDE (> 11) — without hitting
        # `max_thread_messages` the way a second real `_reply_to_thread` would.
        thread.message_count = 13
        await sim._capture_hub_assessment(
            agent, thread, _reply_with_sidecar(4), "2.2", closes_thread=False,
        )

        rows = await _assessments(factory, run_id)
        assert len(rows) == 1, "one interview, one row"
        assert rows[0].summary_posted_at is not None, (
            "the replacement inherits the stamp — the headline is already public"
        )
        assert len(_headlines(client)) == 1, "and no second headline is posted"
        assert [d.reason for d in await _drops(factory, run_id)] == [
            "duplicate_thread_verdict"
        ], "the retirement of the superseded row is recorded"
    finally:
        await _delete_run(factory, run_id)
