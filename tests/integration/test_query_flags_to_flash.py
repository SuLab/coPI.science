# tests/integration/test_query_flags_to_flash.py
"""Status flags carried in query strings (`saved`, `slack_ok`, `msg`) were either
never rendered (D-09, D-10) or could be spoofed by any URL (A-14). They are flash
messages now: set by the handler that did the work, shown once on the next page."""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, SlackAppProvision
from src.services import profile_export
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_finalize_run_route import _stopped_run
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_manager_slack_provisioning import _pending_pi

pytestmark = pytest.mark.integration


async def test_profile_save_flashes_instead_of_a_query_flag(client, db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        "/profile/save",
        data={"name": pi.name, "email": pi.email, "institution": "", "department": "",
              "research_summary": "Saved words"},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/profile"
    page = await follow(client, r)
    assert "Profile saved." in page.text


async def test_public_profile_save_flashes_and_the_query_flag_is_ignored(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save",
        data={"research_summary": "Pub words", "profile_version": str(profile.profile_version)},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/agent/{agent.agent_id}/public-profile"
    page = await follow(client, r)
    assert "Public profile saved and exported." in page.text
    spoof = await client.get(
        f"/agent/{agent.agent_id}/public-profile?saved=1", headers=auth_headers(pi.id)
    )
    assert "Public profile saved and exported." not in spoof.text


async def test_manager_profile_save_flashes(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "email": pi.email, "research_summary": "Manager words"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    page = await follow(client, r)
    assert "Profile saved." in page.text


async def test_an_admin_profile_save_flash_is_shown_once(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_profile(db_session, user=admin)
    r = await client.post(
        "/profile/save",
        data={"name": admin.name, "email": admin.email, "institution": "", "department": "",
              "research_summary": "Admin words"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    first = await follow(client, r)
    assert "Profile saved." in first.text
    again = await client.get("/profile", headers=auth_headers(admin.id))
    assert "Profile saved." not in again.text


async def test_muting_a_pi_without_an_agent_explains_why(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await db_session.commit()
    r = await client.get(f"/manager/pis/{pi.id}?error=no_agent", headers=auth_headers(manager.id))
    assert "This PI has no lab agent yet, so there is nothing to mute or unmute." in r.text
    assert "Something went wrong saving changes." not in r.text


async def _provision(db, agent, initiator, state):
    db.add(SlackAppProvision(
        agent_registry_id=agent.id, state=state, client_id="cid", client_secret="secret",
        initiated_by_user_id=initiator.id,
    ))
    await db.flush()


async def test_the_callback_flashes_success_for_a_manager(client, db_session, monkeypatch):
    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", lambda *a, **k: "xoxb-f1")
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi, agent = await _pending_pi(db_session, agent_id="flashmgr")
    await _provision(db_session, agent, manager, "flash-1")
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=flash-1",
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    page = await follow(client, r)
    assert "Slack bot installed" in page.text


async def test_the_callback_flashes_success_for_an_admin(client, db_session, monkeypatch):
    monkeypatch.setattr("src.services.admin_provisioning.exchange_code", lambda *a, **k: "xoxb-f2")
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    _pi, agent = await _pending_pi(db_session, agent_id="flashadm")
    await _provision(db_session, agent, admin, "flash-2")
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=flash-2",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/agents/{agent.id}"
    page = await follow(client, r)
    assert "Slack bot provisioned" in page.text


async def test_a_slack_ok_query_flag_renders_nothing(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="pending")
    r = await client.get(f"/admin/agents/{agent.id}?slack_ok=1", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert "Slack bot provisioned" not in r.text


async def test_a_callback_error_reaches_the_admin_agents_page(client, db_session):
    """C-03: the callback's admin-surface error used to land on
    /admin/agents?slack_error=…, which never rendered it. Slack's ``error`` value is
    mapped to fixed text, never echoed (Phase 1)."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(
        "/admin/agents/slack/callback?error=access_denied",
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/admin/agents"
    page = await follow(client, r)
    assert "Slack provisioning failed: the installation was cancelled in Slack" in page.text


async def test_stop_flashes_and_a_msg_query_flag_renders_nothing(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _alive(db):
        return True

    monkeypatch.setattr(sim_routes, "engine_alive", _alive)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        "/admin/simulation/stop", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/admin/simulation"
    page = await follow(client, r)
    assert "Stop requested." in page.text
    spoof = await client.get(
        "/admin/simulation?msg=Spoofed-banner-text", headers=auth_headers(admin.id)
    )
    assert "Spoofed-banner-text" not in spoof.text


async def test_finalize_requested_flashes_on_the_run_page(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _stopped_run(db_session)
    r = await client.post(
        "/admin/simulation/finalize-run",
        data={"run_id": str(run.id), "confirm_run": str(run.id)[:8]},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/admin/activity/{run.id}"
    page = await follow(client, r)
    assert "Finalize run requested." in page.text
    spoof = await client.get(
        f"/admin/activity/{run.id}?msg=Spoofed-run-text", headers=auth_headers(admin.id)
    )
    assert "Spoofed-run-text" not in spoof.text
