"""Concurrent read-modify-writes on ResearcherProfile.profile_version and
AgentRegistry.delegate_slack_ids. Pre-fix, both racers read the same prior
value and one write is lost.

These tests cannot use the rollback-scoped ``db_session`` fixture: a lost
update only exists between two sessions committing independently, which is
exactly what a single shared transaction hides. They therefore commit for
real against the session-scoped engine — and so each one deletes its own rows
in a ``finally``. Leaving them behind is not harmless: several suites assert
on GLOBAL counts (``test_agent_page.py`` reads ``select(AgentRegistry)`` and
expects exactly 2), so a stray row here fails an unrelated test file.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentRegistry, ResearcherProfile, User

pytestmark = pytest.mark.integration


async def _make_user_with_profile(factory):
    """Create a committed user + profile. Returns (user_id, profile_id)."""
    async with factory() as db:
        user = User(orcid=f"0000-0000-0000-{uuid.uuid4().hex[:4]}",
                    name="Race Test")
        db.add(user)
        await db.flush()
        profile = ResearcherProfile(user_id=user.id, profile_version=0)
        db.add(profile)
        await db.commit()
        return user.id, profile.id


async def _drop_user(factory, user_id):
    """researcher_profiles.user_id is ON DELETE CASCADE, so this takes both."""
    async with factory() as db:
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.mark.asyncio
async def test_concurrent_version_bumps_both_land(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id, profile_id = await _make_user_with_profile(factory)
    gate = asyncio.Barrier(2)

    async def bump():
        async with factory() as db:
            profile = (await db.execute(
                select(ResearcherProfile).where(ResearcherProfile.id == profile_id)
            )).scalar_one()
            await gate.wait()  # both sessions hold the pre-bump row
            # The exact expression each production site now uses:
            profile.profile_version = func.coalesce(
                ResearcherProfile.profile_version, 0
            ) + 1
            await db.commit()

    try:
        await asyncio.gather(bump(), bump())
        async with factory() as db:
            version = (await db.execute(
                select(ResearcherProfile.profile_version).where(
                    ResearcherProfile.id == profile_id
                )
            )).scalar_one()
        assert version == 2, f"a concurrent bump was lost: version={version}"
    finally:
        await _drop_user(factory, user_id)


@pytest.mark.asyncio
async def test_first_ever_profile_still_reaches_version_one(engine):
    """The three router sites bump a profile they may have just created.

    A SQL expression assigned to a *pending* object renders inside the INSERT's
    VALUES clause, where Postgres refuses to resolve the target table ("invalid
    reference to FROM-clause entry for table researcher_profiles"). Those sites
    therefore flush the new row into existence first, exactly as modelled here;
    without that flush a PI's first-ever profile save 500s.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(orcid=f"0000-0000-0002-{uuid.uuid4().hex[:4]}", name="First Save")
        db.add(user)
        await db.flush()
        user_id = user.id
        await db.commit()

    try:
        async with factory() as db:
            profile = ResearcherProfile(user_id=user_id)
            db.add(profile)
            await db.flush()  # the production flush under test
            profile.research_summary = "first save"
            profile.profile_version = func.coalesce(
                ResearcherProfile.profile_version, 0
            ) + 1
            await db.commit()

        async with factory() as db:
            version = (await db.execute(
                select(ResearcherProfile.profile_version).where(
                    ResearcherProfile.user_id == user_id
                )
            )).scalar_one()
        assert version == 1, f"a first-ever profile save did not land: version={version}"
    finally:
        await _drop_user(factory, user_id)


async def _append_delegate(factory, agent_id, sid, gate):
    from sqlalchemy import text as sa_text
    from sqlalchemy import update as sa_update
    async with factory() as db:
        agent = (await db.execute(
            select(AgentRegistry).where(AgentRegistry.id == agent_id)
        )).scalar_one()
        await gate.wait()  # both sessions hold the pre-append row
        await db.execute(
            sa_update(AgentRegistry)
            .where(
                AgentRegistry.id == agent.id,
                sa_text(
                    "NOT (coalesce(delegate_slack_ids, '{}'::varchar[]) @> ARRAY[:sid]::varchar[])"
                ).bindparams(sid=sid),
            )
            .values(
                delegate_slack_ids=sa_text(
                    "array_append(coalesce(delegate_slack_ids, '{}'::varchar[]), :sid2)"
                ).bindparams(sid2=sid)
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_concurrent_delegate_appends_both_land_and_dedup(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        agent = AgentRegistry(
            agent_id=f"race{uuid.uuid4().hex[:6]}", bot_name="RaceBot",
            pi_name="Race PI", status="pending",
        )
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    try:
        gate = asyncio.Barrier(2)
        await asyncio.gather(
            _append_delegate(factory, agent_id, "U1", gate),
            _append_delegate(factory, agent_id, "U2", gate),
        )
        gate = asyncio.Barrier(2)
        await asyncio.gather(
            _append_delegate(factory, agent_id, "U3", gate),
            _append_delegate(factory, agent_id, "U3", gate),
        )
        async with factory() as db:
            ids = (await db.execute(
                select(AgentRegistry.delegate_slack_ids).where(
                    AgentRegistry.id == agent_id
                )
            )).scalar_one()
        assert sorted(ids) == ["U1", "U2", "U3"], (
            f"lost or duplicated a concurrent delegate append: {ids}"
        )
    finally:
        async with factory() as db:
            await db.execute(delete(AgentRegistry).where(AgentRegistry.id == agent_id))
            await db.commit()


@pytest.mark.asyncio
async def test_concurrent_delegate_removal_leaves_the_other_id(engine):
    """The removal site's mirror: array_remove must not clobber a concurrent
    append, and an emptied array must come back as NULL (the shape the column
    has always had — the old code wrote `current_ids if current_ids else None`).
    """
    from sqlalchemy import text as sa_text
    from sqlalchemy import update as sa_update

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        agent = AgentRegistry(
            agent_id=f"race{uuid.uuid4().hex[:6]}", bot_name="RaceBot",
            pi_name="Race PI", status="pending", delegate_slack_ids=["U1", "U2"],
        )
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    async def remove(sid, gate):
        async with factory() as db:
            await db.execute(
                select(AgentRegistry).where(AgentRegistry.id == agent_id)
            )
            await gate.wait()  # both sessions hold the pre-removal row
            await db.execute(
                sa_update(AgentRegistry)
                .where(AgentRegistry.id == agent_id)
                .values(
                    delegate_slack_ids=sa_text(
                        "nullif(array_remove(coalesce(delegate_slack_ids, "
                        "'{}'::varchar[]), :sid), '{}'::varchar[])"
                    ).bindparams(sid=sid)
                )
            )
            await db.commit()

    try:
        gate = asyncio.Barrier(2)
        await asyncio.gather(remove("U1", gate), remove("U2", gate))
        async with factory() as db:
            ids = (await db.execute(
                select(AgentRegistry.delegate_slack_ids).where(
                    AgentRegistry.id == agent_id
                )
            )).scalar_one()
        assert ids is None, f"an emptied delegate list must be NULL, not {ids!r}"
    finally:
        async with factory() as db:
            await db.execute(delete(AgentRegistry).where(AgentRegistry.id == agent_id))
            await db.commit()
