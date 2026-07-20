"""Factories insert against the real migrated schema (incl. the PCM CHECK)."""

import pytest
from sqlalchemy import func, select

from src.models import (
    AgentChannel,
    AgentRegistry,
    PrivateChannelMember,
    ResearcherProfile,
    SimulationRun,
    User,
)
from tests import factories

pytestmark = pytest.mark.integration


async def test_make_user_persists(db_session):
    u = await factories.make_user(db_session)
    assert u.id is not None
    got = await db_session.get(User, u.id)
    assert got.orcid == u.orcid


async def test_make_profile_links_user(db_session):
    p = await factories.make_profile(db_session)
    assert p.user_id is not None
    assert await db_session.get(ResearcherProfile, p.id) is not None
    assert await db_session.get(User, p.user_id) is not None


async def test_make_agent_with_user(db_session):
    u = await factories.make_user(db_session)
    a = await factories.make_agent(db_session, user=u)
    assert await db_session.get(AgentRegistry, a.id) is not None
    assert a.user_id == u.id


async def test_make_run_and_channel(db_session):
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    assert await db_session.get(AgentChannel, ch.id) is not None
    assert await db_session.get(SimulationRun, ch.simulation_run_id) is not None


async def test_make_private_channel_member_satisfies_check(db_session):
    m = await factories.make_private_channel_member(db_session)
    got = await db_session.get(PrivateChannelMember, m.id)
    assert got.agent_id is not None
    assert got.user_id is None


async def test_unique_orcids_across_builds(db_session):
    a = await factories.make_user(db_session)
    b = await factories.make_user(db_session)
    assert a.orcid != b.orcid
    n = await db_session.scalar(select(func.count()).select_from(User))
    assert n >= 2
