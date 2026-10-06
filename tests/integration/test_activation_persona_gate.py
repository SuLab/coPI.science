"""The activation checks no override waives (spec 2026-10-05 §6.4, D31), the empty-summary
blocker and Retry (P2)."""
import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from src.services.agent_activation import (
    activation_blockers,
    ensure_activation_allowed,
    persona_blockers,
)
from src.services.agent_form import agent_form_version
from src.services.agent_mute import set_agent_mute_state
from src.services.profile_jobs import profile_retry_warranted
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers
from tests.persona_support import write_persona

pytestmark = pytest.mark.integration


async def _lab(db, *, agent_id, owner_role=None, summary="Studies the thing.", status="pending",
               token=None):
    pi = await factories.make_user(db, **({"user_role": owner_role} if owner_role else {}))
    profile = await factories.make_profile(db, user=pi, research_summary=summary,
                                           evidence_pmid_count=10, evidence_pub_count=8)
    agent = await factories.make_agent(db, user=pi, agent_id=agent_id, status=status,
                                       bot_name=f"{agent_id.capitalize()}Bot",
                                       slack_bot_token=token)
    return pi, profile, agent


async def test_the_override_does_not_waive_a_missing_persona_file(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    _pi, _profile, agent = await _lab(db_session, agent_id="nofile")
    page = await client.get(f"/admin/agents/{agent.id}", headers=auth_headers(admin.id))
    assert page.status_code == 200
    assert 'name="activation_override"' in page.text
    assert "it does not waive the persona-file or owner checks" in page.text
    r = await client.post(f"/admin/agents/{agent.id}/approve", data={
        "agent_slug": agent.agent_id, "bot_name": agent.bot_name,
        "form_version": agent_form_version(agent), "activation_override": "1",
    }, headers=auth_headers(admin.id), follow_redirects=False)
    assert "activation_blocked" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.status == "pending"
    assert "persona file profiles/public/nofile.md is missing" in session_flashes(r)[-1]["text"]


async def test_the_override_does_not_waive_a_non_pi_owner(db_session):
    _pi, _profile, agent = await _lab(db_session, agent_id="mgrowned",
                                      owner_role=USER_ROLE_MANAGER)
    write_persona(agent.agent_id)
    blockers = await ensure_activation_allowed(
        db_session, agent, new_role="pi_lab", new_status="active", override=True,
    )
    assert blockers == ["the owner's account (manager) may not use the PI surfaces"]


async def test_a_summary_less_file_is_refused(db_session):
    _pi, _profile, agent = await _lab(db_session, agent_id="nosum")
    write_persona(agent.agent_id, summary=False)
    assert await persona_blockers(db_session, agent) == [
        "persona file profiles/public/nosum.md has no Research Summary"
    ]


async def test_hub_roles_skip_the_persona_checks(db_session):
    hub = await factories.make_agent(db_session, agent_id="hubcheck", role="scout_hub",
                                     status="inactive")
    assert await persona_blockers(db_session, hub) == []
    blockers = await ensure_activation_allowed(
        db_session, hub, new_role="scout_hub", new_status="active",
    )
    # Only the D14 hub limit may refuse a hub (when the test database holds an active one).
    assert all("persona file" not in b and "PI surfaces" not in b for b in blockers)


async def test_unmute_is_refused_without_a_persona_file(db_session):
    _pi, _profile, agent = await _lab(db_session, agent_id="mutefile", status="inactive",
                                      token="xoxb-mutefile")
    actor = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assert await set_agent_mute_state(db_session, agent=agent, muted=False, actor=actor) \
        == "activation_blocked"


async def test_an_empty_summary_blocks_activation_and_warrants_retry(db_session):
    _pi, profile, agent = await _lab(db_session, agent_id="blanked", summary="")
    assert "the profile has no Research Summary" in await activation_blockers(db_session, agent)
    assert profile_retry_warranted(profile, None) is True
