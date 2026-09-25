"""Two concurrent first-time writers race SELECT-then-INSERT on a unique
key. Pre-fix, the loser's commit raises IntegrityError out of the handler
(a 500 in production). The sessions are separate on purpose — one session
would serialize the race away."""
import asyncio
import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentMessage, AgentRegistry, ProposalReview, SimulationRun, User
from tests import factories

pytestmark = pytest.mark.integration


class _ExistenceCheckGate:
    """Two-party barrier: releases both waiters only once EACH has arrived.

    `review_proposal` and `reopen_proposal` (src/routers/agent_page.py) each
    run a pre-insert SELECT for an existing review before they INSERT.
    Two real, unsynchronised racers can interleave so that racer #1's SELECT
    *and commit* both finish before racer #2 even reaches its own SELECT —
    at which point racer #2 correctly finds the row and redirects (D10).
    That is correct behaviour, but it is the wrong race: it never reaches the
    INSERT-level collision
    (uq_proposal_reviews_decision_agent / IntegrityError) this test exists to
    exercise. Gating both racers on this barrier right after their existence
    SELECT returns forces the interleaving the test means to pin: both see
    "no existing row" before either is allowed to proceed to INSERT/commit,
    so the actual conflict is decided at the unique-constraint/IntegrityError
    path inside review_proposal's try/except (rollback + redirect), which is
    the thing under test. (Same for reopen_proposal's try/except.)
    """

    def __init__(self, parties: int):
        self._parties = parties
        self._arrived = 0
        self._condition = asyncio.Condition()

    async def arrive_and_wait(self) -> None:
        async with self._condition:
            self._arrived += 1
            if self._arrived >= self._parties:
                self._condition.notify_all()
            else:
                await self._condition.wait_for(lambda: self._arrived >= self._parties)


def _lost_race_warnings(caplog, decision_id, agent_id) -> list[str]:
    """The route's lost-race WARNINGs that name this decision and agent."""
    return [
        rec.getMessage() for rec in caplog.records
        if rec.name == "src.routers.agent_page"
        and rec.levelno == logging.WARNING
        and "Lost the race on uq_proposal_reviews_decision_agent" in rec.getMessage()
        and str(decision_id) in rec.getMessage()
        and agent_id in rec.getMessage()
    ]


def _is_review_existence_check(statement) -> bool:
    """True only for the `select(ProposalReview).where(...)` existence check —
    the sole Select along either route's call path whose only selected entity
    is ProposalReview (the ThreadDecision lookup, the AgentRegistry lookup in
    get_agent_with_access, reopen's SimulationRun/AgentChannel lookups in
    pi_inbox, and the EmailEngagementTracker/EmailNotification lookups in
    record_engagement / mark_notification_responded all select different
    entities)."""
    try:
        descriptions = statement.column_descriptions
    except AttributeError:
        return False
    return len(descriptions) == 1 and descriptions[0].get("entity") is ProposalReview


@pytest.mark.asyncio
async def test_concurrent_proposal_reviews_do_not_500(engine, caplog):
    """Same race, on review_proposal's uq_proposal_reviews_decision_agent.

    Two tabs / a double-click submit the same agent's review for the same
    proposal at once. Pre-fix, the loser's commit raises IntegrityError out
    of the handler; post-fix it rolls back and redirects to the dashboard
    like the winner, so both responses are that redirect and exactly one
    review row exists. Asserting both responses is what catches a loser that
    returns anything else without raising (RCA mutation R2). The loser also
    logs exactly one WARNING naming the decision and agent, since its rating
    is discarded.

    The two racers are pinned on `_ExistenceCheckGate` (see its docstring) so
    both are guaranteed to pass the pre-insert existence check before either
    is allowed to proceed to INSERT/commit — otherwise nothing forces that
    interleaving, and the loser can instead (correctly, but besides the
    point) redirect from its own existence check after the winner's commit
    has already landed.
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

    gate = _ExistenceCheckGate(parties=2)
    caplog.set_level(logging.WARNING, logger="src.routers.agent_page")

    async def submit():
        async with factory() as db:
            real_execute = db.execute

            async def gated_execute(statement, *args, **kwargs):
                result = await real_execute(statement, *args, **kwargs)
                if _is_review_existence_check(statement):
                    await gate.arrive_and_wait()
                return result

            db.execute = gated_execute
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
        assert sorted([r1.status_code, r2.status_code]) == [302, 302]
        for r in (r1, r2):
            assert r.headers["location"] == f"/agent/{agent_id}/dashboard"

        async with factory() as db:
            count = (await db.execute(
                select(func.count(ProposalReview.id)).where(
                    ProposalReview.thread_decision_id == decision_id,
                    ProposalReview.agent_id == agent_id,
                )
            )).scalar_one()
        assert count == 1
        warnings = _lost_race_warnings(caplog, decision_id, agent_id)
        assert len(warnings) == 1, warnings
        assert "rating was discarded" in warnings[0]
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


@pytest.mark.asyncio
async def test_concurrent_reopens_do_not_500(engine, caplog):
    """Same race, on reopen_proposal: a double-click submits the same agent's
    reopen twice at once. Both pass the pre-insert existence check (pinned by
    `_ExistenceCheckGate`), both write guidance to the inbox and add a rating=0
    review, and the loser's commit collides on uq_proposal_reviews_decision_agent.
    Its rollback must take its inbox row with it, so exactly one review and one
    PI inbox message survive, and both racers get the dashboard redirect. The
    discarded guidance is recorded by exactly one WARNING naming the decision
    and agent.
    """
    from src.routers.agent_page import reopen_proposal

    factory = async_sessionmaker(engine, expire_on_commit=False)

    class _Req:  # unused by reopen_proposal, but the signature asks for one
        headers: dict = {}
        client = None
        session: dict = {}

    # Factory-counter ids and explicit cleanup, for the reasons given in
    # test_concurrent_proposal_reviews_do_not_500.
    async with factory() as setup_db:
        run = await factories.make_simulation_run(setup_db)
        pi = await factories.make_user(setup_db)
        agent = await factories.make_agent(setup_db, user=pi, status="active")
        decision = await factories.make_thread_decision(
            setup_db, run=run, agent_a=agent.agent_id, agent_b="counterpart",
        )
        await setup_db.commit()
        agent_id, decision_id, thread_ts = agent.agent_id, decision.id, decision.thread_id
        run_id, agent_pk, pi_id = run.id, agent.id, pi.id

    gate = _ExistenceCheckGate(parties=2)
    caplog.set_level(logging.WARNING, logger="src.routers.agent_page")

    async def submit():
        async with factory() as db:
            real_execute = db.execute

            async def gated_execute(statement, *args, **kwargs):
                result = await real_execute(statement, *args, **kwargs)
                if _is_review_existence_check(statement):
                    await gate.arrive_and_wait()
                return result

            db.execute = gated_execute
            try:
                return await reopen_proposal(
                    agent_id, decision_id, _Req(), guidance="Refine the readout.",
                    db=db, current_user=pi,
                )
            finally:
                await db.close()

    try:
        r1, r2 = await asyncio.gather(submit(), submit(), return_exceptions=True)
        for r in (r1, r2):
            assert not isinstance(r, Exception), f"a racer raised: {r!r}"
        assert sorted([r1.status_code, r2.status_code]) == [302, 302]
        for r in (r1, r2):
            assert r.headers["location"] == f"/agent/{agent_id}/dashboard"

        async with factory() as db:
            reviews = (await db.execute(
                select(func.count(ProposalReview.id)).where(
                    ProposalReview.thread_decision_id == decision_id,
                    ProposalReview.agent_id == agent_id,
                )
            )).scalar_one()
            inbox = (await db.execute(
                select(func.count(AgentMessage.id)).where(
                    AgentMessage.simulation_run_id == run_id,
                    AgentMessage.thread_ts == thread_ts,
                    AgentMessage.agent_id.is_(None),
                )
            )).scalar_one()
        assert reviews == 1
        assert inbox == 1, f"the losing reopen's guidance survived: {inbox} inbox rows"
        warnings = _lost_race_warnings(caplog, decision_id, agent_id)
        assert len(warnings) == 1, warnings
        assert "reopen guidance was discarded" in warnings[0]
    finally:
        # As above, plus the surviving inbox row: AgentMessage cascades from
        # the SimulationRun delete.
        async with factory() as cleanup_db:
            for model, pk in (
                (User, pi_id), (AgentRegistry, agent_pk), (SimulationRun, run_id),
            ):
                obj = await cleanup_db.get(model, pk)
                if obj is not None:
                    await cleanup_db.delete(obj)
            await cleanup_db.commit()
