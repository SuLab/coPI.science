"""find_or_create_pi_by_orcid: the shared ORCID-onboarding logic used by the
manager Add-PI route and (Task 2b) admin's impersonate-if-new path."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, Job, User
from src.services.pi_onboarding import find_or_create_pi_by_orcid
from tests import factories

pytestmark = pytest.mark.integration


async def test_creates_a_pi_user_and_enqueues_a_profile_job(db_session):
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(return_value={
            "name": "Ada Lovelace", "email": "ada@example.edu",
            "institution": "Example University", "department": "Computing",
        }),
    ):
        user = await find_or_create_pi_by_orcid(db_session, "0000-0001-2345-6789")
        await db_session.commit()

    assert user.orcid == "0000-0001-2345-6789"
    assert user.name == "Ada Lovelace"
    assert user.user_role == "pi"

    jobs = (await db_session.execute(
        select(Job).where(Job.user_id == user.id, Job.type == "generate_profile")
    )).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].payload == {"user_id": str(user.id), "orcid": "0000-0001-2345-6789"}


async def test_rejects_an_orcid_that_already_exists_regardless_of_role(db_session):
    existing = await factories.make_user(
        db_session, orcid="0000-0009-9999-0001", user_role=USER_ROLE_ADMIN,
    )
    with pytest.raises(ValueError, match="already exists"):
        await find_or_create_pi_by_orcid(db_session, existing.orcid)


async def test_a_fetch_failure_raises_instead_of_creating_a_stub_user(db_session):
    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(side_effect=RuntimeError("ORCID API down")),
    ):
        with pytest.raises(ValueError, match="Could not fetch ORCID profile"):
            await find_or_create_pi_by_orcid(db_session, "0000-0002-0000-0000")

    count = (await db_session.execute(
        select(User).where(User.orcid == "0000-0002-0000-0000")
    )).scalars().all()
    assert count == []


async def test_rejects_a_malformed_orcid_before_any_network_call(db_session):
    fetch = AsyncMock()
    with patch("src.services.pi_onboarding.fetch_orcid_profile", new=fetch):
        with pytest.raises(ValueError, match="Invalid ORCID"):
            await find_or_create_pi_by_orcid(
                db_session, "0000-0001; DROP[Affiliation]"
            )
    fetch.assert_not_awaited()


async def test_creation_persists_an_employment_derived_tenure_start(db_session):
    from src.services.jhu_rules import get_tenure_start

    with patch(
        "src.services.pi_onboarding.fetch_orcid_profile",
        new=AsyncMock(return_value={
            "name": "New Hire", "email": "nh@example.edu",
            "institution": "Johns Hopkins University", "department": "Neuro",
            "employments": [
                {"organization": "Johns Hopkins University",
                 "start_year": 2016, "current": True},
            ],
        }),
    ):
        user = await find_or_create_pi_by_orcid(db_session, "0000-0006-1111-2222")

    assert await get_tenure_start(db_session, user.id) == 2016


async def test_create_pending_agent_for_is_idempotent_and_inert(db_session):
    from src.models import AgentRegistry
    from src.services.pi_onboarding import create_pending_agent_for

    user = await factories.make_user(
        db_session, orcid="0000-0006-3333-4444", name="Mary McCarthy",
    )
    agent = await create_pending_agent_for(db_session, user)
    assert (agent.agent_id, agent.bot_name) == ("mccarthy", "McCarthyBot")
    assert agent.status == "pending"
    assert agent.user_id == user.id
    assert agent.slack_bot_token is None

    again = await create_pending_agent_for(db_session, user)
    assert again.id == agent.id, "a second call must return the existing row"

    rows = (await db_session.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )).scalars().all()
    assert len(rows) == 1
