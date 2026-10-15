"""create_revision stamps the agent's owner and dedupes per owner (spec 2026-10-15 §10 R5.18;
C6)."""
import pytest
from sqlalchemy import select

from src.models import ProfileRevision
from src.services.profile_edit import apply_profile_edits
from src.services.profile_versioning import create_revision
from tests import factories

pytestmark = pytest.mark.integration


async def _public(db, agent_id):
    return (await db.execute(select(ProfileRevision).where(
        ProfileRevision.agent_registry_id == agent_id, ProfileRevision.profile_type == "public",
    ))).scalars().all()


async def test_export_revision_is_stamped_with_the_owner(db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, research_summary="Old summary.",
                                 profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    assert await apply_profile_edits(
        db_session, target_user=pi, changed_by_user_id=pi.id,
        form={"research_summary": "New summary."}, expected_version=1) is None
    [revision] = await _public(db_session, agent.id)
    assert revision.owner_user_id == pi.id


async def test_memory_revision_is_stamped(db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    hub = await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    lab_rev = await create_revision(db_session, agent_registry_id=agent.id,
                                    profile_type="memory", content="m", mechanism="agent")
    hub_rev = await create_revision(db_session, agent_registry_id=hub.id,
                                    profile_type="memory", content="m", mechanism="agent")
    assert lab_rev.owner_user_id == pi.id
    assert hub_rev.owner_user_id is None


async def test_identical_body_from_a_new_owner_is_recorded(db_session):
    first_owner = await factories.make_user(db_session)
    second_owner = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=first_owner)
    first = await create_revision(db_session, agent_registry_id=agent.id,
                                  profile_type="public", content="X", mechanism="pipeline")
    agent.user_id = second_owner.id
    await db_session.flush()
    second = await create_revision(db_session, agent_registry_id=agent.id,
                                   profile_type="public", content="X", mechanism="pipeline")
    assert second.id != first.id and second.owner_user_id == second_owner.id
    await db_session.refresh(first)
    assert first.owner_user_id == first_owner.id and first.content == "X"


async def test_identical_body_same_owner_is_deduped(db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    first = await create_revision(db_session, agent_registry_id=agent.id,
                                  profile_type="public", content="X", mechanism="pipeline")
    again = await create_revision(db_session, agent_registry_id=agent.id,
                                  profile_type="public", content="X", mechanism="web")
    assert again.id == first.id
    assert len(await _public(db_session, agent.id)) == 1
