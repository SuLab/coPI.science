"""delete_user_account, DB phase (audit F1/F4/F5, decisions D1/D2/D4/D5)."""
import pytest
from sqlalchemy import select

from src.models import (
    AccessAllowlist,
    AgentRegistry,
    AppSetting,
    PiDmMessage,
    ProfileRevision,
    User,
)
from src.services.jhu_rules import TENURE_KEY_PREFIX
from src.services.user_deletion import delete_user_account
from tests import factories

pytestmark = pytest.mark.asyncio


async def _seed_pi_with_agent(db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, status="active")
    db_session.add(
        ProfileRevision(
            agent_registry_id=agent.id,
            profile_type="public",
            content="full profile snapshot",
            mechanism="pipeline",
        )
    )
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        PiDmMessage(
            simulation_run_id=run.id,
            agent_id=agent.agent_id,
            pi_user_id=f"local:{user.id}",
            direction="inbound",
            content="my unpublished idea",
            ts="1.0",
            posted_at=1.0,
        )
    )
    db_session.add(
        AppSetting(key=f"{TENURE_KEY_PREFIX}{user.id}", value='{"year": 2019}')
    )
    await db_session.flush()
    return user, agent


async def test_teardown_suspends_agent_and_purges(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("src.services.user_deletion._PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr("src.services.user_deletion._MEMORY_DIR", tmp_path / "mem")
    user, agent = await _seed_pi_with_agent(db_session)
    user_id, agent_pk, agent_slug = user.id, agent.id, agent.agent_id

    report = await delete_user_account(db_session, user)

    assert (await db_session.get(User, user_id)) is None
    refreshed = await db_session.get(AgentRegistry, agent_pk)
    assert refreshed.status == "suspended"
    assert refreshed.user_id is None
    assert (
        await db_session.scalar(
            select(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent_pk
            )
        )
    ) is None
    assert (
        await db_session.scalar(
            select(PiDmMessage).where(PiDmMessage.agent_id == agent_slug)
        )
    ) is None
    assert (
        await db_session.scalar(
            select(AppSetting).where(
                AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}"
            )
        )
    ) is None
    assert report.agent_id == agent_slug
    assert report.agent_suspended is True
    assert report.revisions_deleted == 1
    assert report.dms_deleted == 1
    assert report.tenure_key_deleted is True


async def test_teardown_without_agent_still_purges_user_keyed_rows(db_session):
    user = await factories.make_user(db_session)
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        PiDmMessage(
            simulation_run_id=run.id,
            agent_id="ghost",
            pi_user_id=f"local:{user.id}",
            direction="inbound",
            content="x",
            ts="1.0",
            posted_at=1.0,
        )
    )
    await db_session.flush()
    user_id = user.id

    report = await delete_user_account(db_session, user)

    assert (await db_session.get(User, user_id)) is None
    assert report.agent_id is None
    assert report.dms_deleted == 1


async def test_allowlist_removed_only_when_asked(db_session):
    u1 = await factories.make_user(db_session)
    u2 = await factories.make_user(db_session)
    db_session.add(AccessAllowlist(orcid=u1.orcid))
    db_session.add(AccessAllowlist(orcid=u2.orcid))
    await db_session.flush()
    o1, o2 = u1.orcid, u2.orcid

    await delete_user_account(db_session, u1)  # default: keep
    await delete_user_account(db_session, u2, remove_from_allowlist=True)

    kept = await db_session.scalar(
        select(AccessAllowlist).where(AccessAllowlist.orcid == o1)
    )
    gone = await db_session.scalar(
        select(AccessAllowlist).where(AccessAllowlist.orcid == o2)
    )
    assert kept is not None
    assert gone is None
