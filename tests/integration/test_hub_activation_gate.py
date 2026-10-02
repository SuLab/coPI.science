"""One active hub at a time (spec §6.8 C-05, decision D14), role changes on an active
agent run the activation gate for the new role, and an approved agent cannot be sent
back to `pending` (C-04)."""
import inspect

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, AgentRegistry
from src.services import agent_activation
from src.services.advisory_locks import HUB_ROSTER_LOCK_KEY, advisory_lock_held
from src.services.agent_activation import ensure_activation_allowed
from src.services.agent_form import agent_form_version
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_LIVE_HUB_BLOCKER = "another hub-role agent (hub-live) is already active; deactivate it first"


async def _admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


async def _live_hub(db_session):
    return await factories.make_agent(
        db_session, agent_id="hub-live", role="scout_hub", status="active"
    )


async def _role_of(db_session, agent):
    return (await db_session.execute(
        select(AgentRegistry.role).where(AgentRegistry.id == agent.id)
    )).scalar_one()


async def test_a_second_active_hub_is_refused(db_session):
    await _live_hub(db_session)
    spare = await factories.make_agent(
        db_session, agent_id="hub-spare", role="scout_hub", status="inactive"
    )
    assert await ensure_activation_allowed(
        db_session, spare, new_role="scout_hub", new_status="active"
    ) == [_LIVE_HUB_BLOCKER]


async def test_the_hub_limit_survives_the_override(db_session):
    await _live_hub(db_session)
    spare = await factories.make_agent(
        db_session, agent_id="hub-spare2", role="scout_hub", status="inactive"
    )
    assert await ensure_activation_allowed(
        db_session, spare, new_role="scout_hub", new_status="active", override=True
    ) == [_LIVE_HUB_BLOCKER]


async def test_a_lone_hub_may_activate_and_the_roster_lock_is_taken(db_session):
    spare = await factories.make_agent(
        db_session, agent_id="hub-only", role="scout_hub", status="inactive"
    )
    assert await ensure_activation_allowed(
        db_session, spare, new_role="scout_hub", new_status="active"
    ) == []
    assert await advisory_lock_held(db_session, HUB_ROSTER_LOCK_KEY)


def test_the_lock_precedes_the_hub_count():
    """Race-proof only if the transaction lock is taken before the count reads."""
    src = inspect.getsource(agent_activation.ensure_activation_allowed)
    assert src.index("pg_advisory_xact_lock") < src.index("hub_role_names()")


async def test_a_status_other_than_active_needs_no_gate(db_session):
    agent = await factories.make_agent(db_session, status="active")
    assert await ensure_activation_allowed(
        db_session, agent, new_role="pi_lab", new_status="inactive"
    ) == []


async def test_an_inactive_agent_may_take_a_hub_role(client, db_session):
    admin = await _admin(db_session)
    await _live_hub(db_session)
    parked = await factories.make_agent(db_session, agent_id="parked", status="inactive")
    r = await client.post(
        f"/admin/agents/{parked.id}/role", data={"role": "scout_hub"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, parked) == "scout_hub"


async def test_a_role_change_runs_the_gate_for_the_new_role(client, db_session):
    """An active hub has no linked user; as a pi_lab it would fail the profile gate."""
    admin = await _admin(db_session)
    hub = await _live_hub(db_session)
    r = await client.post(
        f"/admin/agents/{hub.id}/role", data={"role": "pi_lab"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == f"/admin/agents/{hub.id}"
    assert await _role_of(db_session, hub) == "scout_hub"
    flashes = session_flashes(r)
    assert flashes[0]["kind"] == "error"
    assert flashes[0]["text"].startswith("Role not changed: not linked to a user account")


async def test_an_active_lab_cannot_become_a_second_hub(client, db_session):
    admin = await _admin(db_session)
    await _live_hub(db_session)
    user = await factories.make_user(db_session)
    lab = await factories.make_agent(db_session, user=user, agent_id="lab-x", status="active")
    r = await client.post(
        f"/admin/agents/{lab.id}/role", data={"role": "scout_hub"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _role_of(db_session, lab) == "pi_lab"
    assert session_flashes(r) == [
        {"text": f"Role not changed: {_LIVE_HUB_BLOCKER}", "kind": "error"}
    ]


async def test_the_edit_form_cannot_activate_a_second_hub_even_with_the_override(
    client, db_session
):
    admin = await _admin(db_session)
    await _live_hub(db_session)
    spare = await factories.make_agent(
        db_session, agent_id="hub-spare3", role="scout_hub", status="inactive"
    )
    r = await client.post(
        f"/admin/agents/{spare.id}/approve",
        data={
            "agent_slug": spare.agent_id, "bot_name": spare.bot_name,
            "form_version": agent_form_version(spare), "agent_status": "active",
            "activation_override": "1",
        },
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert "activation_blocked=1" in r.headers["location"]
    await db_session.refresh(spare)
    assert spare.status == "inactive"
    assert session_flashes(r) == [
        {"text": f"Activation refused: {_LIVE_HUB_BLOCKER}", "kind": "error"}
    ]


async def test_the_edit_form_offers_pending_only_to_a_pending_agent(client, db_session):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=user, status="active")
    r = await client.get(f"/admin/agents/{agent.id}", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert '<option value="inactive"' in r.text
    assert '<option value="pending"' not in r.text


async def test_an_approved_agent_cannot_be_sent_back_to_pending(client, db_session):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=user, status="active")
    r = await client.post(
        f"/admin/agents/{agent.id}/approve",
        data={
            "agent_slug": agent.agent_id, "bot_name": agent.bot_name,
            "form_version": agent_form_version(agent), "agent_status": "pending",
        },
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/agents/{agent.id}?error=pending_not_allowed"
    await db_session.refresh(agent)
    assert agent.status == "active"
