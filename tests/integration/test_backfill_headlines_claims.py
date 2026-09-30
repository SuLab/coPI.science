"""Spec P0-08: scripts/backfill_assessment_headlines.py claims before posting,
refuses a live engine, and selects by how the run ended."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from scripts.backfill_assessment_headlines import (
    IN_DOUBT,
    POSTED,
    UNCLAIMED,
    _build_arg_parser,
    engine_live_refusal,
    newest_owed_per_thread,
    run_repair,
)
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.models import (
    OpportunityAssessment,
    SimulationProcessStatus,
    SimulationRun,
    ThreadDecision,
)
from src.services.headline_claims import claim_thread
from tests.fakes import FakeSlackClient
from tests.integration.test_assessment_headline_delivery import _headlines, _wire_summary_channel
from tests.integration.test_hub_assessment_capture_gate import _delete_run, _hub, _new_run

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as db:
        status = await db.get(SimulationProcessStatus, 1)
        if status is not None:
            await db.delete(status)
            await db.commit()
    return f


async def _owed(factory, run_id, thread_id, *, project="Owed Co", created_at=None, posted=False):
    async with factory() as db:
        row = OpportunityAssessment(
            simulation_run_id=run_id, agent_id="blackbird", subject_agent_id="gordy",
            channel_name="single-cell-omics", thread_id=thread_id, slack_ts=None,
            company_or_project=project, recommendation="conditional", scores={},
            summary_posted_at=datetime.now(UTC) if posted else None,
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        await db.commit()
        return row.id


async def _end(factory, run_id, thread_id):
    async with factory() as db:
        db.add(ThreadDecision(
            simulation_run_id=run_id, thread_id=thread_id, channel="single-cell-omics",
            agent_a="blackbird", agent_b="gordy", outcome="timeout",
        ))
        await db.commit()


def _args(*argv):
    return _build_arg_parser().parse_args(list(argv))


def _posts(client):
    return client.posted_messages.get(ASSESSMENTS_SUMMARY_CHANNEL, [])


async def test_a_live_engine_refuses_writes_and_run_crashed_overrides(factory):
    async with factory() as db:
        db.add(SimulationProcessStatus(id=1, state="starting"))
        await db.commit()
    try:
        async with factory() as db:
            refusal = await engine_live_refusal(db, run_crashed=False)
            assert refusal and "starting" in refusal
            assert await engine_live_refusal(db, run_crashed=True) is None
        run_id = await _new_run(factory)
        try:
            await _owed(factory, run_id, "t1")
            client = FakeSlackClient(agent_id="blackbird")
            assert await run_repair(
                _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
            ) == 2
            assert _posts(client) == []
            assert await run_repair(
                _args("--run", str(run_id), "--apply", "--run-crashed"), factory,
                make_client=lambda a: client,
            ) == 0
            assert len(_posts(client)) == 1
        finally:
            await _delete_run(factory, run_id)
    finally:
        async with factory() as db:
            status = await db.get(SimulationProcessStatus, 1)
            if status is not None:
                await db.delete(status)
                await db.commit()


async def test_a_kill_between_claim_and_post_is_listed_in_doubt_and_skipped(factory, caplog):
    run_id = await _new_run(factory)
    try:
        row_id = await _owed(factory, run_id, "t1")
        async with factory() as db:
            assert await claim_thread(db, run_id, "t1")  # the killed poster's claim
            await db.execute(
                update(OpportunityAssessment).where(OpportunityAssessment.id == row_id)
                .values(summary_claimed_at=text("now() - interval '11 minutes'"))
            )
            await db.commit()
        client = FakeSlackClient(agent_id="blackbird")
        with caplog.at_level(logging.INFO, logger="backfill_assessment_headlines"):
            assert await run_repair(
                _args("--run", str(run_id), "--list-in-doubt"), factory,
                make_client=lambda a: client,
            ) == 0
        assert any(str(row_id) in r.getMessage() and "IN DOUBT" in r.getMessage()
                   for r in caplog.records)
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        assert _posts(client) == [], "a rerun skips the in-doubt thread"

        assert await run_repair(
            _args("--run", str(run_id), "--release-in-doubt", str(row_id)), factory,
            make_client=lambda a: client,
        ) == 0
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        assert len(_posts(client)) == 1, "released, then posted once"
    finally:
        await _delete_run(factory, run_id)


async def test_the_engine_sweep_and_the_script_concurrently_post_once(factory):
    run_id = await _new_run(factory)
    try:
        await _owed(factory, run_id, "t1")
        sim, _agent = _hub(factory, run_id)
        _wire_summary_channel(sim)
        script_client = FakeSlackClient(agent_id="blackbird")
        await asyncio.gather(
            sim._announce_owed_headline("t1", trigger="shutdown"),
            run_repair(
                _args("--run", str(run_id), "--apply"), factory,
                make_client=lambda a: script_client,
            ),
        )
        assert len(_headlines(sim.slack_clients["blackbird"])) + len(_posts(script_client)) == 1
    finally:
        await _delete_run(factory, run_id)


async def _supersede_in_place(factory, row_id, project):
    """What the engine's upsert does to a landed row (migration 0055, one row per
    run and thread): the verdict columns change, the announcement stamps stay."""
    async with factory() as db:
        await db.execute(
            update(OpportunityAssessment).where(OpportunityAssessment.id == row_id)
            .values(company_or_project=project, verdict_revision=2)
        )
        await db.commit()


async def test_a_thread_with_a_posted_row_is_never_reposted_and_the_newest_row_renders(factory):
    run_id = await _new_run(factory)
    try:
        posted_id = await _owed(factory, run_id, "t-posted", posted=True)
        await _supersede_in_place(factory, posted_id, "Replacement Co")
        owed_id = await _owed(factory, run_id, "t-two", project="Older Co")
        await _supersede_in_place(factory, owed_id, "Newest Co")
        client = FakeSlackClient(agent_id="blackbird")
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        [text_posted] = _posts(client)
        assert "Newest Co" in text_posted and "Older Co" not in text_posted
        assert "Replacement Co" not in text_posted
    finally:
        await _delete_run(factory, run_id)


def test_the_picker_skips_a_posted_thread_whole_and_takes_the_newest_row():
    """The script's per-thread picker, on rows the unique (run, thread) index no
    longer lets the table hold together: it still guards any such set."""
    now = datetime.now(UTC)

    def row(thread_id, project, *, minutes_ago=0, posted=False):
        return OpportunityAssessment(
            thread_id=thread_id, company_or_project=project,
            created_at=now - timedelta(minutes=minutes_ago),
            summary_posted_at=now if posted else None, summary_claimed_at=None,
        )

    rows = [row("t-posted", "Posted Co", minutes_ago=9, posted=True), row("t-posted", "Later Co"),
            row("t-two", "Older Co", minutes_ago=5), row("t-two", "Newest Co")]
    candidates, skipped = newest_owed_per_thread(rows)
    assert [r.company_or_project for r in candidates] == ["Newest Co"]
    assert sorted(r.company_or_project for r, _why in skipped) == ["Later Co", "Older Co", "Posted Co"]


async def test_a_null_thread_row_is_claimed_by_id(factory):
    run_id = await _new_run(factory)
    try:
        row_id = await _owed(factory, run_id, None)
        client = FakeSlackClient(agent_id="blackbird")
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        assert len(_posts(client)) == 1
        async with factory() as db:
            row = await db.get(OpportunityAssessment, row_id)
            assert row.summary_claimed_at is not None and row.summary_posted_at is not None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.parametrize("held,expected", [(True, 1), (False, 2)])
async def test_without_finalize_open_threads_follow_the_hold(factory, held, expected):
    run_id = await _new_run(factory)
    try:
        if held:
            async with factory() as db:
                await db.execute(
                    update(SimulationRun).where(SimulationRun.id == run_id)
                    .values(held_at=text("now()"))
                )
                await db.commit()
        await _owed(factory, run_id, "t-ended")
        await _owed(factory, run_id, "t-open")
        await _end(factory, run_id, "t-ended")
        client = FakeSlackClient(agent_id="blackbird")
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        assert len(_posts(client)) == expected
    finally:
        await _delete_run(factory, run_id)


async def test_a_held_run_still_posts_a_row_with_no_thread(factory):
    run_id = await _new_run(factory)
    try:
        async with factory() as db:
            await db.execute(
                update(SimulationRun).where(SimulationRun.id == run_id)
                .values(held_at=text("now()"))
            )
            await db.commit()
        await _owed(factory, run_id, None)
        client = FakeSlackClient(agent_id="blackbird")
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        assert len(_posts(client)) == 1
    finally:
        await _delete_run(factory, run_id)


async def test_finalize_posts_open_threads_and_closes_the_run(factory):
    run_id = await _new_run(factory)
    try:
        async with factory() as db:
            await db.execute(
                update(SimulationRun).where(SimulationRun.id == run_id)
                .values(held_at=text("now()"))
            )
            await db.commit()
        await _owed(factory, run_id, "t-open")
        client = FakeSlackClient(agent_id="blackbird")
        assert await run_repair(
            _args("--run", str(run_id), "--finalize", "--apply"), factory,
            make_client=lambda a: client,
        ) == 0
        assert len(_posts(client)) == 1
        async with factory() as db:
            run = await db.get(SimulationRun, run_id)
            assert run.finalized_at is not None and run.held_at is None
    finally:
        await _delete_run(factory, run_id)


async def test_30_owed_at_a_stop_then_apply_posts_the_other_5(factory, monkeypatch):
    run_id = await _new_run(factory)
    try:
        for i in range(30):
            await _owed(factory, run_id, f"t{i:02d}")
        sim, _agent = _hub(factory, run_id)
        _wire_summary_channel(sim)
        sim.request_stop("operator")
        await sim.stop()
        assert len(_headlines(sim.slack_clients["blackbird"])) == 25
        client = FakeSlackClient(agent_id="blackbird")
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 0
        assert len(_posts(client)) == 5
        async with factory() as db:
            unposted = await db.scalar(text(
                "SELECT count(*) FROM opportunity_assessments "
                "WHERE simulation_run_id = :r AND summary_posted_at IS NULL"
            ), {"r": run_id})
        assert unposted == 0
    finally:
        await _delete_run(factory, run_id)


async def test_a_transport_error_leaves_the_row_in_doubt(factory):
    run_id = await _new_run(factory)
    try:
        row_id = await _owed(factory, run_id, "t1")
        client = FakeSlackClient(agent_id="blackbird")

        def boom(*a, **kw):
            raise ConnectionResetError("reset after send")

        client.post_message = boom
        assert await run_repair(
            _args("--run", str(run_id), "--apply"), factory, make_client=lambda a: client,
        ) == 1, "an in-doubt post is a nonzero exit"
        async with factory() as db:
            row = await db.get(OpportunityAssessment, row_id)
            assert row.summary_claimed_at is not None and row.summary_posted_at is None
        assert (IN_DOUBT, POSTED, UNCLAIMED) == ("in_doubt", "posted", "unclaimed")
    finally:
        await _delete_run(factory, run_id)
