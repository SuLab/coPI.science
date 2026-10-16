"""scripts/backfill_hub_declines.py: finds a run's hub declines without an assessment,
writes them only with --apply, announces owed ones only with --post, and refuses to write
while an engine holds the engine lock."""
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from scripts.backfill_hub_declines import _build_arg_parser, run_backfill
from src.models import (
    AgentMessage,
    AgentRegistry,
    AssessmentDrop,
    HubDecline,
    OpportunityAssessment,
    SimulationProcessStatus,
    SimulationRun,
    ThreadDecision,
)
from src.models.hub_decline import REASON_MAX_CHARS
from src.services import decline_claims
from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock

pytestmark = pytest.mark.integration

HUB_REPLY = "⏸️ I'm declining this one. Translational potential: unconfirmed."


@pytest.fixture
async def factory(engine):
    f = async_sessionmaker(engine, expire_on_commit=False)
    async with f() as db:
        status = await db.get(SimulationProcessStatus, 1)
        if status is not None:
            await db.delete(status)
            await db.commit()
    return f


@pytest.fixture
async def world(factory):
    """A run, a hub agent and two lab agents, all removed afterwards."""
    suffix = uuid.uuid4().hex[:8]
    hub, lab, lab2 = f"hub-{suffix}", f"lab-{suffix}", f"lab2-{suffix}"
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        db.add_all([
            AgentRegistry(agent_id=hub, bot_name="HubBot", pi_name="Blackbird",
                          status="active", role="scout_hub"),
            AgentRegistry(agent_id=lab, bot_name="LabBot", pi_name="Jane Hu",
                          status="active", role="pi_lab"),
            AgentRegistry(agent_id=lab2, bot_name="Lab2Bot", pi_name="Sam Lee",
                          status="active", role="pi_lab"),
        ])
        await db.commit()
        run_id = run.id
    yield {"run": run_id, "hub": hub, "lab": lab, "lab2": lab2}
    async with factory() as db:
        stale = await db.get(SimulationRun, run_id)
        if stale is not None:
            await db.delete(stale)
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_([hub, lab, lab2])))
        await db.commit()


async def _interview(factory, run_id, thread_id, *, agent_a, agent_b, closer_agent,
                     closed_by_role="scout_hub", reply=HUB_REPLY, channel="general"):
    """A decided interview: the root post, an older and a newer closing reply by
    ``closer_agent``, and a ``no_proposal`` decision."""
    async with factory() as db:
        db.add(ThreadDecision(
            simulation_run_id=run_id, thread_id=thread_id, channel=channel,
            agent_a=agent_a, agent_b=agent_b, outcome="no_proposal",
            closed_by_role=closed_by_role,
        ))
        other = agent_b if closer_agent == agent_a else agent_a
        for i, (author, content, slack_ts) in enumerate([
            (other, "Our pitch.", None),
            (closer_agent, "Tell me more.", f"{thread_id}1"),
            (closer_agent, reply, f"{thread_id}2"),
        ]):
            db.add(AgentMessage(
                simulation_run_id=run_id, agent_id=author, channel_id="C1",
                channel_name=channel, message_ts=f"{thread_id}-{i}", thread_ts=thread_id,
                phase="thread_reply", content=content, posted_at=float(i + 1),
                slack_ts=slack_ts,
            ))
        await db.commit()


async def _declines(factory, run_id):
    async with factory() as db:
        return list((await db.execute(
            select(HubDecline).where(HubDecline.simulation_run_id == run_id)
            .order_by(HubDecline.thread_id)
        )).scalars().all())


def _args(*argv):
    return _build_arg_parser().parse_args(list(argv))


class FakeAnnouncer:
    def __init__(self, outcome="posted"):
        self.outcome = outcome
        self.calls: list[HubDecline] = []

    async def announce_decline(self, row):
        self.calls.append(row)
        return self.outcome


def _maker(announcer):
    async def make(_factory, _run_id):
        return announcer
    return make


async def test_preview_writes_nothing(factory, world, pg_url):
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    assert await run_backfill(_args("--run", str(world["run"])), factory, lock_url=pg_url) == 0
    assert await _declines(factory, world["run"]) == []


async def test_apply_writes_the_hub_decline(factory, world, pg_url):
    long_reply = HUB_REPLY + " " + "x" * REASON_MAX_CHARS
    # The hub is agent_b here: the hub is found by role, not by position.
    await _interview(factory, world["run"], "t1", agent_a=world["lab"], agent_b=world["hub"],
                     closer_agent=world["hub"], reply=long_reply, channel="single-cell")
    assert await run_backfill(
        _args("--run", str(world["run"]), "--apply"), factory, lock_url=pg_url,
    ) == 0
    [row] = await _declines(factory, world["run"])
    assert (row.agent_id, row.subject_agent_id, row.channel_name, row.thread_id, row.slack_ts) == (
        world["hub"], world["lab"], "single-cell", "t1", "t12")
    assert row.reason_text == long_reply[:REASON_MAX_CHARS]
    assert row.summary_claimed_at is None and row.summary_posted_at is None


async def test_a_thread_with_an_assessment_is_skipped(factory, world, pg_url):
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    async with factory() as db:
        db.add(OpportunityAssessment(
            simulation_run_id=world["run"], agent_id=world["hub"],
            subject_agent_id=world["lab"], channel_name="general", thread_id="t1",
            slack_ts=None, company_or_project="X", recommendation="decline", scores={},
        ))
        await db.commit()
    assert await run_backfill(
        _args("--run", str(world["run"]), "--apply"), factory, lock_url=pg_url,
    ) == 0
    assert await _declines(factory, world["run"]) == []


async def test_a_labs_close_is_skipped(factory, world, pg_url):
    # A lab's ⏸️ close recorded as such.
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["lab"], closed_by_role="pi_lab")
    # Recorded as a scout_hub close, but neither agent is a hub.
    await _interview(factory, world["run"], "t2", agent_a=world["lab"], agent_b=world["lab2"],
                     closer_agent=world["lab"])
    assert await run_backfill(
        _args("--run", str(world["run"]), "--apply"), factory, lock_url=pg_url,
    ) == 0
    assert await _declines(factory, world["run"]) == []


async def test_a_rerun_is_a_no_op(factory, world, pg_url):
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    args = _args("--run", str(world["run"]), "--apply")
    assert await run_backfill(args, factory, lock_url=pg_url) == 0
    [first] = await _declines(factory, world["run"])
    assert await run_backfill(args, factory, lock_url=pg_url) == 0
    [again] = await _declines(factory, world["run"])
    assert (again.id, again.created_at) == (first.id, first.created_at)


async def test_post_announces_each_owed_decline_once(factory, world, pg_url):
    for thread in ("t1", "t2"):
        await _interview(factory, world["run"], thread, agent_a=world["hub"],
                         agent_b=world["lab"], closer_agent=world["hub"])
    await _interview(factory, world["run"], "t3", agent_a=world["hub"], agent_b=world["lab2"],
                     closer_agent=world["hub"])
    assert await run_backfill(
        _args("--run", str(world["run"]), "--apply"), factory, lock_url=pg_url,
    ) == 0
    async with factory() as db:  # t3's headline is already claimed: not owed
        row = (await db.execute(select(HubDecline).where(
            HubDecline.simulation_run_id == world["run"], HubDecline.thread_id == "t3",
        ))).scalar_one()
        assert await decline_claims.claim(db, row.id)

    announcer = FakeAnnouncer()
    assert await run_backfill(
        _args("--run", str(world["run"]), "--post"), factory,
        make_announcer=_maker(announcer), lock_url=pg_url,
    ) == 0
    assert sorted(r.thread_id for r in announcer.calls) == ["t1", "t2"]

    lost = FakeAnnouncer(outcome="lost")
    assert await run_backfill(
        _args("--run", str(world["run"]), "--post"), factory,
        make_announcer=_maker(lost), lock_url=pg_url,
    ) == 1
    in_doubt = FakeAnnouncer(outcome="in_doubt")
    assert await run_backfill(
        _args("--run", str(world["run"]), "--post"), factory,
        make_announcer=_maker(in_doubt), lock_url=pg_url,
    ) == 1


async def test_writes_are_refused_while_an_engine_holds_the_lock(factory, world, pg_url):
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    announcer = FakeAnnouncer()
    engine_lock = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await engine_lock.acquire()
    try:
        for flags in (("--apply",), ("--post",), ("--apply", "--post")):
            assert await run_backfill(
                _args("--run", str(world["run"]), *flags), factory,
                make_announcer=_maker(announcer), lock_url=pg_url,
            ) == 2
        # The preview takes no lock.
        assert await run_backfill(
            _args("--run", str(world["run"])), factory, lock_url=pg_url,
        ) == 0
    finally:
        await engine_lock.release()
    assert await _declines(factory, world["run"]) == []
    assert announcer.calls == []
    # Released, the same write succeeds, and the script frees the lock on exit.
    assert await run_backfill(
        _args("--run", str(world["run"]), "--apply"), factory, lock_url=pg_url,
    ) == 0
    assert len(await _declines(factory, world["run"])) == 1
    again = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    try:
        assert await again.acquire()
    finally:
        await again.release()


async def test_an_in_doubt_claim_is_listed_then_released(factory, world, pg_url, caplog):
    """A claim kept by a transport error is listed read-only, released on request, and the
    next --post announces the decline."""
    from datetime import timedelta

    from sqlalchemy import func, update

    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    run = str(world["run"])
    assert await run_backfill(_args("--run", run, "--apply"), factory, lock_url=pg_url) == 0
    [row] = await _declines(factory, world["run"])
    async with factory() as db:
        await db.execute(update(HubDecline).where(HubDecline.id == row.id).values(
            summary_claimed_at=func.now() - timedelta(hours=1)))
        await db.commit()
    caplog.set_level("INFO")
    assert await run_backfill(_args("--run", run, "--list-in-doubt"), factory, lock_url=pg_url) == 0
    assert f"IN DOUBT decline {row.id}" in caplog.text
    assert await run_backfill(
        _args("--run", run, "--release-in-doubt", "00000000-0000-0000-0000-000000000000"),
        factory, lock_url=pg_url) == 2
    assert await run_backfill(
        _args("--run", run, "--release-in-doubt", str(row.id)), factory, lock_url=pg_url) == 0
    announcer = FakeAnnouncer()
    assert await run_backfill(_args("--run", run, "--post"), factory,
                              make_announcer=_maker(announcer), lock_url=pg_url) == 0
    assert [r.id for r in announcer.calls] == [row.id]


async def test_a_split_closing_reply_keeps_its_first_chunk(factory, world, pg_url):
    """A reply over Slack's limit is several rows; only the first opens with ⏸️, and it
    is the one kept, as the engine keeps it."""
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    async with factory() as db:
        db.add(AgentMessage(
            simulation_run_id=world["run"], agent_id=world["hub"], channel_id="C1",
            channel_name="general", message_ts="t1-tail", thread_ts="t1", phase="thread_reply",
            content="...the rest of the long reply.", posted_at=9.0, slack_ts="t1tail"))
        await db.commit()
    assert await run_backfill(_args("--run", str(world["run"]), "--apply"), factory,
                              lock_url=pg_url) == 0
    [row] = await _declines(factory, world["run"])
    assert row.reason_text == HUB_REPLY and row.slack_ts == "t12"


async def test_a_non_lab_counterparty_and_a_dropped_verdict_are_skipped(factory, world, pg_url):
    async with factory() as db:
        await db.execute(update(AgentRegistry).where(AgentRegistry.agent_id == world["lab2"])
                         .values(role="grantbot"))
        db.add(AssessmentDrop(simulation_run_id=world["run"], agent_id=world["hub"],
                              subject_agent_id=world["lab"], thread_id="t2",
                              reason="unparseable_sidecar"))
        await db.commit()
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab2"],
                     closer_agent=world["hub"])
    await _interview(factory, world["run"], "t2", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    assert await run_backfill(_args("--run", str(world["run"]), "--apply"), factory,
                              lock_url=pg_url) == 0
    assert await _declines(factory, world["run"]) == []


async def test_a_run_with_unthreaded_assessments_is_refused(factory, world, pg_url):
    async with factory() as db:
        db.add(OpportunityAssessment(
            simulation_run_id=world["run"], agent_id=world["hub"], channel_name="general",
            company_or_project="X", recommendation="pass", thread_id=None))
        await db.commit()
    await _interview(factory, world["run"], "t1", agent_a=world["hub"], agent_b=world["lab"],
                     closer_agent=world["hub"])
    assert await run_backfill(_args("--run", str(world["run"]), "--apply"), factory,
                              lock_url=pg_url) == 2
    assert await _declines(factory, world["run"]) == []


async def test_bad_arguments_are_refused(factory, world, pg_url):
    run = str(world["run"])
    assert await run_backfill(_args("--run", "e91c35df"), factory, lock_url=pg_url) == 2
    assert await run_backfill(_args("--run", run, "--list-in-doubt", "--post"), factory,
                              lock_url=pg_url) == 2
    assert await run_backfill(
        _args("--run", run, "--release-in-doubt", run, "--apply"), factory, lock_url=pg_url) == 2
