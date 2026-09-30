"""Spec P0-04: what the shutdown sweep announces depends on WHY the run ended.

TODAY (default Stop, SIGTERM): every owed headline, open interviews included,
capped at 25 — exactly as before; which 25 is unspecified (the owed query has no
ORDER BY), so parity is asserted on sets and counts. FINALIZE (natural end): the
same, then finalized_at. HOLD: only ended interviews, then held_at."""
import logging

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import OpportunityAssessment, SimulationRun, ThreadDecision
from tests.integration.test_assessment_headline_delivery import (
    _headlines,
    _wire_summary_channel,
)
from tests.integration.test_hub_assessment_capture_gate import (
    _assessments,
    _delete_run,
    _hub,
    _new_run,
    _reply_with_sidecar,
)
from tests.integration.test_review_supersession import _FailOnceFactory, _no_seed

pytestmark = pytest.mark.integration


async def _seed_owed(factory, run_id, thread_ids, *, ended=()):
    async with factory() as db:
        for i, thread_id in enumerate(thread_ids):
            db.add(OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird", subject_agent_id="gordy",
                channel_name="single-cell-omics", thread_id=thread_id,
                slack_ts=f"200.{i:06d}", recommendation="conditional", scores={},
            ))
        for thread_id in ended:
            db.add(ThreadDecision(
                simulation_run_id=run_id, thread_id=thread_id, channel="single-cell-omics",
                agent_a="blackbird", agent_b="gordy", outcome="timeout",
            ))
        await db.commit()


async def _run_row(factory, run_id):
    async with factory() as db:
        return await db.get(SimulationRun, run_id)


async def _engine(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    sim, _hub_agent = _hub(factory, run_id)
    _wire_summary_channel(sim)
    return sim, factory, run_id


@pytest.mark.parametrize("reason", [None, "operator", "signal"])
async def test_the_default_stop_and_sigterm_announce_every_owed_headline(engine, reason):
    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, ["t-ended", "t-open-1", "t-open-2"], ended=["t-ended"])
        if reason:
            sim.request_stop(reason)
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 3, "open interviews included"
        run = await _run_row(factory, run_id)
        assert run.finalized_at is None and run.held_at is None
    finally:
        await _delete_run(factory, run_id)


async def test_more_than_25_owed_posts_exactly_25_and_the_rest_stay_claimable(engine, caplog):
    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, [f"t{i:02d}" for i in range(27)])
        sim.request_stop("operator")
        with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
            await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 25
        rows = await _assessments(factory, run_id)
        rest = [r for r in rows if r.summary_posted_at is None]
        assert len(rest) == 2 and all(r.summary_claimed_at is None for r in rest)
        lost = [r.getMessage() for r in caplog.records if "LOST 2" in r.getMessage()]
        assert lost and "--apply" in lost[0] and "--finalize" not in lost[0]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.parametrize("reason", ["operator_hold", "stall", "exception", "start_failed"])
async def test_hold_ends_announce_only_ended_interviews_and_set_held_at(engine, reason):
    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, ["t-ended", "t-open"], ended=["t-ended"])
        sim.request_stop(reason)
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 1
        rows = {r.thread_id: r for r in await _assessments(factory, run_id)}
        assert rows["t-ended"].summary_posted_at is not None
        assert rows["t-open"].summary_posted_at is None
        assert rows["t-open"].summary_claimed_at is None, "held, not claimed"
        run = await _run_row(factory, run_id)
        assert run.held_at is not None and run.finalized_at is None
    finally:
        await _delete_run(factory, run_id)


async def test_operator_hold_then_signal_still_holds(engine):
    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, ["t-ended", "t-open"], ended=["t-ended"])
        sim.request_stop("operator_hold")
        sim.request_stop("signal")
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 1
    finally:
        await _delete_run(factory, run_id)


async def test_a_natural_end_announces_everything_and_finalizes(engine):
    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, ["t-ended", "t-open"], ended=["t-ended"])
        sim.request_stop("time_limit")
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 2
        run = await _run_row(factory, run_id)
        assert run.finalized_at is not None and run.held_at is None
    finally:
        await _delete_run(factory, run_id)


async def test_a_finalize_with_30_owed_sets_finalized_at_and_logs_5_lost(engine, caplog):
    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, [f"t{i:02d}" for i in range(30)])
        sim.request_stop("target_drained")
        with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
            await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 25
        assert (await _run_row(factory, run_id)).finalized_at is not None
        lost = [r.getMessage() for r in caplog.records if "LOST 5" in r.getMessage()]
        assert lost and "--finalize --apply" in lost[0]
    finally:
        await _delete_run(factory, run_id)


async def test_hold_stop_with_an_unreadable_owed_query_posts_nothing(engine):
    """Review Focus 1: a HOLD never falls back to announcing open interviews."""
    from src.agent.simulation import _HeldVerdict

    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, ["t-ended", "t-open"], ended=["t-ended"])
        for thread_id in ("t-ended", "t-open"):
            sim._assessed_threads[thread_id] = _HeldVerdict(
                ordinal=12, final=False, slack_ts=None, announced=False,
            )
        failing = _FailOnceFactory(factory)
        sim.session_factory = failing
        failing.armed = True  # the sweep's owed-headline SELECT is the first session
        sim.request_stop("operator_hold")
        await sim.stop()
        assert failing.armed is False
        assert _headlines(sim.slack_clients["blackbird"]) == []
        assert (await _run_row(factory, run_id)).held_at is not None
    finally:
        await _delete_run(factory, run_id)


async def test_a_queued_first_verdict_is_posted_by_the_finalize_sweep(engine):
    """Spec P0-08 test: the verdict's first write failed, so the capture did not
    post; the natural-end sweep runs after the final flush and posts it."""
    from src.agent.state import ThreadState

    sim, factory, run_id = await _engine(engine)
    try:
        hub = sim.agents["blackbird"]
        sim._seed_consults_from_db = _no_seed
        failing = _FailOnceFactory(factory)
        sim.session_factory = failing
        thread = ThreadState(
            thread_id="t1", channel="single-cell-omics", other_agent_id="gordy",
            message_count=11,
        )
        hub.state.active_threads["t1"] = thread
        failing.armed = True
        await sim._capture_hub_assessment(
            hub, thread, _reply_with_sidecar(4), "100.000009", closes_thread=False,
        )
        assert len(sim._pending_assessments) == 1
        assert _headlines(sim.slack_clients["blackbird"]) == []
        sim.request_stop("time_limit")
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 1
        assert (await _run_row(factory, run_id)).finalized_at is not None
    finally:
        await _delete_run(factory, run_id)


async def test_a_row_claimed_elsewhere_is_not_reported_lost(engine, caplog):
    """An owed row whose claim another holder has (an earlier process's in-doubt
    post) is skipped, and the operator is pointed at --list-in-doubt, not told
    the headline is LOST and to re-post with --apply (which would skip it too)."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    sim, factory, run_id = await _engine(engine)
    try:
        await _seed_owed(factory, run_id, ["t-claimed", "t-free"])
        async with factory() as db:
            await db.execute(
                update(OpportunityAssessment)
                .where(OpportunityAssessment.simulation_run_id == run_id,
                       OpportunityAssessment.thread_id == "t-claimed")
                .values(summary_claimed_at=datetime.now(UTC) - timedelta(minutes=11))
            )
            await db.commit()
        with caplog.at_level(logging.ERROR, logger="src.agent.simulation"):
            await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 1
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert not any("LOST" in m for m in errors), errors
        assert any("CLAIMED ELSEWHERE 1" in m and "t-claimed" in m and "--list-in-doubt" in m
                   for m in errors), errors
    finally:
        await _delete_run(factory, run_id)
