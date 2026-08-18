"""Seeding against a real Postgres.

Idempotence is the property that matters: this script will be run more than once
against production, and the second run must be a no-op rather than a duplicate-key
crash or a silent double-insert. The audit assertions exist because blackbird's 62
cohorts were seeded by direct SQL and have no `created`/`agent_added` rows at all —
that is the failure this task is written to avoid.
"""

import pytest
from sqlalchemy import func, select

from src.models import AgentRegistry, Cohort, CohortAuditEvent, CohortMembership
from src.services.cohort_seed import apply_plan, plan_seed
from src.services.cohorts import compute_gates
from tests import factories

pytestmark = pytest.mark.integration

MANIFEST = {
    "cohorts": {
        "alpha": {"description": "A", "source": "src-a", "members": ["su", "wiseman"]},
        "beta": {"description": "B", "source": "src-b", "members": ["wiseman", "cravatt"]},
    }
}


@pytest.fixture
async def roster(db_session):
    for aid, bot in (("su", "SuBot"), ("wiseman", "WisemanBot"), ("cravatt", "CravattBot")):
        user = await factories.make_user(db_session, email=f"{aid}@example.org")
        await factories.make_agent(
            db_session, user=user, agent_id=aid, bot_name=bot,
            pi_name=f"PI {aid}", status="active",
        )
    await db_session.flush()


async def _state(db):
    cohorts = {n for (n,) in await db.execute(select(Cohort.name))}
    rows = (await db.execute(
        select(Cohort.name, CohortMembership.agent_id)
        .join(CohortMembership, CohortMembership.cohort_id == Cohort.id)
    )).all()
    return cohorts, {(n, a) for n, a in rows}


async def _seed(db, manifest=MANIFEST, prune=False):
    cohorts, memberships = await _state(db)
    plan = plan_seed(manifest, cohorts, memberships)
    await apply_plan(db, manifest, plan, prune=prune)
    return plan


class TestApplyPlan:
    async def test_creates_cohorts_and_memberships(self, db_session, roster):
        await _seed(db_session)
        cohorts, memberships = await _state(db_session)
        assert cohorts == {"alpha", "beta"}
        assert memberships == {
            ("alpha", "su"), ("alpha", "wiseman"),
            ("beta", "wiseman"), ("beta", "cravatt"),
        }

    async def test_description_is_written(self, db_session, roster):
        await _seed(db_session)
        c = (await db_session.execute(
            select(Cohort).where(Cohort.name == "alpha")
        )).scalar_one()
        assert c.description == "A"

    async def test_second_run_is_a_noop(self, db_session, roster):
        await _seed(db_session)
        before = (await db_session.execute(
            select(func.count()).select_from(CohortMembership)
        )).scalar_one()

        plan = await _seed(db_session)

        assert plan.is_noop is True
        after = (await db_session.execute(
            select(func.count()).select_from(CohortMembership)
        )).scalar_one()
        assert after == before == 4

    async def test_agent_in_two_cohorts_gets_two_rows(self, db_session, roster):
        """wiseman is in both alpha and beta — overlap is the point, not a bug."""
        await _seed(db_session)
        rows = (await db_session.execute(
            select(func.count()).select_from(CohortMembership)
            .where(CohortMembership.agent_id == "wiseman")
        )).scalar_one()
        assert rows == 2

    async def test_writes_created_and_agent_added_audit_events(self, db_session, roster):
        await _seed(db_session)
        events = (await db_session.execute(select(CohortAuditEvent))).scalars().all()
        created = [e for e in events if e.action == "created"]
        added = [e for e in events if e.action == "agent_added"]
        assert {e.cohort_name for e in created} == {"alpha", "beta"}
        assert len(added) == 4
        assert all(e.cohort_id is not None for e in created + added)
        assert {(e.cohort_name, e.agent_id) for e in added} == {
            ("alpha", "su"), ("alpha", "wiseman"),
            ("beta", "wiseman"), ("beta", "cravatt"),
        }

    async def test_noop_run_writes_no_further_audit_events(self, db_session, roster):
        await _seed(db_session)
        before = (await db_session.execute(
            select(func.count()).select_from(CohortAuditEvent)
        )).scalar_one()
        await _seed(db_session)
        after = (await db_session.execute(
            select(func.count()).select_from(CohortAuditEvent)
        )).scalar_one()
        assert after == before

    async def test_prune_deletes_extras_and_audits_the_removal(self, db_session, roster):
        await _seed(db_session)
        c = (await db_session.execute(
            select(Cohort).where(Cohort.name == "alpha")
        )).scalar_one()
        db_session.add(CohortMembership(cohort_id=c.id, agent_id="cravatt"))
        await db_session.flush()

        plan = await _seed(db_session, prune=True)

        assert plan.extra_memberships == (("alpha", "cravatt"),)
        _, memberships = await _state(db_session)
        assert ("alpha", "cravatt") not in memberships
        removed = (await db_session.execute(
            select(CohortAuditEvent).where(CohortAuditEvent.action == "agent_removed")
        )).scalars().all()
        assert [(e.cohort_name, e.agent_id) for e in removed] == [("alpha", "cravatt")]

    async def test_without_prune_extras_survive(self, db_session, roster):
        await _seed(db_session)
        c = (await db_session.execute(
            select(Cohort).where(Cohort.name == "alpha")
        )).scalar_one()
        db_session.add(CohortMembership(cohort_id=c.id, agent_id="cravatt"))
        await db_session.flush()

        await _seed(db_session, prune=False)

        _, memberships = await _state(db_session)
        assert ("alpha", "cravatt") in memberships


class TestGateStaysInert:
    async def test_seeded_topology_gates_nothing_while_isolation_is_off(
        self, db_session, roster
    ):
        """The whole plan rests on this: memberships recorded, nothing enforced."""
        await _seed(db_session)
        _, memberships = await _state(db_session)
        agent_ids = [a for (a,) in await db_session.execute(
            select(AgentRegistry.agent_id).where(AgentRegistry.status == "active")
        )]

        gates, error = compute_gates(
            membership_rows=sorted(memberships),
            agent_ids=agent_ids,
            isolation_enabled=False,
            policy="open",
            cohort_count=2,
        )

        assert error is None
        assert set(gates) == set(agent_ids)
        assert all(g is None for g in gates.values())
