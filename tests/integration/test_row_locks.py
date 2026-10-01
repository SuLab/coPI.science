"""RA-12 / RA-13: row locks on cohort delete/add and on the grant veto.

Each test holds the row lock in one committed session and drives the real route
function in another, then reads ``pg_stat_activity`` for a backend waiting on a
``FOR UPDATE`` query. Without the lock the route would not wait at all."""
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentRegistry, Cohort, PiGrant, ResearcherProfile
from tests import factories

pytestmark = pytest.mark.integration


async def _is_waiting_on_for_update(factory) -> bool:
    async with factory() as s:
        return (await s.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND datname = current_database() AND query ILIKE '%FOR UPDATE%' "
            "AND pid <> pg_backend_pid()"
        ))).scalar_one() > 0


def _admin():
    return type("A", (), {"id": None, "name": "t"})()


async def test_cohort_add_waits_for_a_concurrent_delete(engine):
    from src.routers.admin import cohorts as routes

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        cohort = Cohort(name=f"lock-{uuid.uuid4().hex[:6]}")
        agent = AgentRegistry(agent_id=f"lk{uuid.uuid4().hex[:6]}", bot_name="B", pi_name="P", status="active")
        s.add_all([cohort, agent])
        await s.commit()
    try:
        async with factory() as s1:
            await s1.execute(text("SELECT 1 FROM cohorts WHERE id = :id FOR UPDATE"), {"id": cohort.id})
            async with factory() as s2:
                task = asyncio.create_task(
                    routes.admin_cohort_add_agent(cohort.id, agent.agent_id, db=s2, current_user=_admin()))
                await asyncio.sleep(0.5)
                assert await _is_waiting_on_for_update(factory)
                await s1.execute(text("DELETE FROM cohorts WHERE id = :id"), {"id": cohort.id})
                await s1.commit()
                with pytest.raises(HTTPException) as exc:  # the cohort is gone once the lock is released
                    await task
                assert exc.value.status_code == 404
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM cohorts WHERE id = :id"), {"id": cohort.id})
            await s.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent.id})
            await s.commit()


async def test_cohort_delete_waits_for_a_concurrent_holder(engine):
    from src.routers.admin import cohorts as routes

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        cohort = Cohort(name=f"lock-{uuid.uuid4().hex[:6]}")
        s.add(cohort)
        await s.commit()
    try:
        async with factory() as s1:
            await s1.execute(text("SELECT 1 FROM cohorts WHERE id = :id FOR UPDATE"), {"id": cohort.id})
            async with factory() as s2:
                task = asyncio.create_task(routes.admin_cohort_delete(cohort.id, db=s2, current_user=_admin()))
                await asyncio.sleep(0.5)
                assert await _is_waiting_on_for_update(factory)
                await s1.rollback()
                await task
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM cohort_audit_events WHERE cohort_id = :id"), {"id": cohort.id})
            await s.execute(text("DELETE FROM cohorts WHERE id = :id"), {"id": cohort.id})
            await s.commit()


async def test_grant_veto_waits_for_a_concurrent_profile_write(engine, tmp_path, monkeypatch):
    from src.routers import manager as routes

    monkeypatch.setattr("src.services.profile_export.PROFILES_DIR", tmp_path / "public")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = await factories.make_user(s)
        await factories.make_profile(s, user=user, grant_titles=["T1", "OLD"])
        grant = PiGrant(user_id=user.id, core_project_num="R01X", title="T1", org_name="Org",
                        tenure_filter_mode="all")
        s.add(grant)
        await s.commit()
    try:
        async with factory() as s1:
            await s1.execute(text("SELECT 1 FROM researcher_profiles WHERE user_id = :u FOR UPDATE"), {"u": user.id})
            async with factory() as s2:
                task = asyncio.create_task(routes.manager_veto_grant(
                    user.id, grant.id, request=None, db=s2, current_user=_admin()))
                await asyncio.sleep(0.5)
                assert await _is_waiting_on_for_update(factory)
                await s1.execute(text("UPDATE researcher_profiles SET grant_titles = :t WHERE user_id = :u"),
                                 {"t": ["T1", "ORCID"], "u": user.id})
                await s1.commit()
                await task
        async with factory() as s:
            titles = (await s.execute(select(ResearcherProfile.grant_titles)
                                      .where(ResearcherProfile.user_id == user.id))).scalar_one()
        assert titles == ["ORCID"]  # computed from the row A committed, not a stale read
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user.id})
            await s.commit()
