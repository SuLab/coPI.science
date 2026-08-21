"""Two concurrent first-time writers race SELECT-then-INSERT on a unique
key. Pre-fix, the loser's commit raises IntegrityError out of the handler
(a 500 in production). The sessions are separate on purpose — one session
would serialize the race away."""
import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentRegistry, ProposalReview, SimulationRun, User, WaitlistSignup
from tests import factories

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_concurrent_waitlist_signups_do_not_500(engine, monkeypatch):
    from src.routers.public import waitlist_submit

    monkeypatch.setattr(
        "src.routers.public._waitlist_limiter",
        type("_L", (), {"allow": staticmethod(lambda ip: True)})(),
    )
    monkeypatch.setattr(
        "src.routers.public.templates.TemplateResponse",
        lambda *a, **k: "rendered",
    )

    factory = async_sessionmaker(engine, expire_on_commit=False)

    class _Req:  # only what the handler reads
        headers: dict = {}
        client = None
        session: dict = {}

    async def submit():
        async with factory() as db:
            try:
                return await waitlist_submit(
                    _Req(), email="race@example.org", name="R",
                    institution="X", note="", db=db,
                )
            finally:
                await db.close()

    r1, r2 = await asyncio.gather(submit(), submit(), return_exceptions=True)
    for r in (r1, r2):
        assert not isinstance(r, Exception), f"a racer raised: {r!r}"

    async with factory() as db:
        count = (await db.execute(
            select(func.count(WaitlistSignup.id)).where(
                WaitlistSignup.email == "race@example.org"
            )
        )).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_proposal_reviews_do_not_500(engine):
    """Same race, on review_proposal's uq_proposal_reviews_decision_agent.

    Two tabs / a double-click submit the same agent's review for the same
    proposal at once. Pre-fix, the loser's commit raises IntegrityError out
    of the handler; post-fix it rolls back and redirects like a normal
    "already reviewed" outcome, same as the winner.
    """
    from src.routers.agent_page import review_proposal

    factory = async_sessionmaker(engine, expire_on_commit=False)

    class _Req:  # unused by review_proposal, but the signature asks for one
        headers: dict = {}
        client = None
        session: dict = {}

    # NOTE: agent_id/bot_name are left to the factory's own counter
    # (agent1/agent2/... — never a literal like "alpha"/"beta"). This test's
    # writes are real commits against the shared session-scoped `engine`,
    # not the rolled-back `db_session` other integration tests use, so a
    # hardcoded id here would permanently collide with fixtures elsewhere
    # (e.g. tests/integration/test_proposal_review.py's `lab` fixture, which
    # hardcodes agent_id="alpha") the next time this file's tests run in the
    # same session.
    async with factory() as setup_db:
        run = await factories.make_simulation_run(setup_db)
        pi = await factories.make_user(setup_db)
        agent = await factories.make_agent(setup_db, user=pi, status="active")
        decision = await factories.make_thread_decision(
            setup_db, run=run, agent_a=agent.agent_id, agent_b="counterpart",
        )
        await setup_db.commit()
        agent_id, decision_id = agent.agent_id, decision.id
        run_id, agent_pk, pi_id = run.id, agent.id, pi.id

    async def submit():
        async with factory() as db:
            try:
                return await review_proposal(
                    agent_id, decision_id, _Req(), rating=3, comment="",
                    db=db, current_user=pi,
                )
            finally:
                await db.close()

    try:
        r1, r2 = await asyncio.gather(submit(), submit(), return_exceptions=True)
        for r in (r1, r2):
            assert not isinstance(r, Exception), f"a racer raised: {r!r}"

        async with factory() as db:
            count = (await db.execute(
                select(func.count(ProposalReview.id)).where(
                    ProposalReview.thread_decision_id == decision_id,
                    ProposalReview.agent_id == agent_id,
                )
            )).scalar_one()
        assert count == 1
    finally:
        # This test's writes are real commits, not the rolled-back
        # `db_session` other integration tests get — tests/integration/
        # test_harness_smoke.py's test_writes_are_rolled_back_part1/part2
        # assert the ENTIRE simulation_runs table is empty before their own
        # insert, so a leftover row here would break an unrelated test that
        # merely happens to run later in the same session. Deleting the
        # SimulationRun and User cascades (DB-level ON DELETE) to the
        # ThreadDecision/ProposalReview and ProposalReview.user_id rows
        # respectively; AgentRegistry.user_id is ON DELETE SET NULL, not
        # cascaded, so it needs its own delete.
        async with factory() as cleanup_db:
            for model, pk in (
                (User, pi_id), (AgentRegistry, agent_pk), (SimulationRun, run_id),
            ):
                obj = await cleanup_db.get(model, pk)
                if obj is not None:
                    await cleanup_db.delete(obj)
            await cleanup_db.commit()
