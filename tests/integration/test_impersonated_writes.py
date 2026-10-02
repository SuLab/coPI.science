"""A-10: every write made while impersonating carries an impersonation note — a
column where the table has one (Tasks 2A-6, 2A-7), and always the central log line
from get_current_user (this task) naming the real admin and the worn account."""

import logging

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    AdminAuditEvent,
    ProfileRevision,
    SimulationCommand,
    SlackAppProvision,
)
from src.services import profile_export
from tests import factories
from tests.integration._webui_helpers import impersonation_headers
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_manager_slack_provisioning import _pending_pi

pytestmark = pytest.mark.integration


async def test_a_write_under_impersonation_is_logged_with_both_identities(client, db_session, caplog):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    caplog.set_level(logging.WARNING, logger="src.dependencies")
    r = await client.post(
        "/admin/access-allowlist/add", data={"orcid": "0000-0004-0000-0001", "note": ""},
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert (
        f"Write POST /admin/access-allowlist/add by admin {real.id} while impersonating {worn.id}"
        in caplog.messages
    )


async def test_a_read_under_impersonation_is_not_logged_as_a_write(client, db_session, caplog):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    caplog.set_level(logging.WARNING, logger="src.dependencies")
    r = await client.get("/admin/access-requests", headers=impersonation_headers(real.id, worn.id))
    assert r.status_code == 200
    assert not [m for m in caplog.messages if m.startswith("Write ")]


async def test_a_write_without_impersonation_logs_no_impersonation_line(client, db_session, caplog):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    caplog.set_level(logging.WARNING, logger="src.dependencies")
    await client.post(
        "/admin/access-allowlist/add", data={"orcid": "0000-0004-0000-0002", "note": ""},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert not [m for m in caplog.messages if "while impersonating" in m]


async def test_the_slack_callback_records_the_impersonation_note(
    client, db_session, caplog, monkeypatch
):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    _pi, agent = await _pending_pi(db_session, agent_id="impcb")
    db_session.add(SlackAppProvision(
        agent_registry_id=agent.id, state="imp-s", client_id="cid", client_secret="secret",
        initiated_by_user_id=manager.id,
    ))
    await db_session.flush()
    monkeypatch.setattr(
        "src.services.admin_provisioning.exchange_code", lambda *a, **k: "xoxb-imp"
    )
    caplog.set_level(logging.INFO, logger="src.routers.admin")
    r = await client.get(
        "/admin/agents/slack/callback?code=c&state=imp-s",
        headers=impersonation_headers(real.id, manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert any(
        "Slack bot token stored for agent impcb" in m and f"impersonated by admin {real.id}" in m
        for m in caplog.messages
    )


async def _latest_revision(db, agent):
    return (await db.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
        .order_by(ProfileRevision.created_at.desc())
    )).scalars().first()


async def test_profile_save_under_impersonation_is_attributed(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        "/profile/save",
        data={"name": pi.name, "email": pi.email, "institution": "", "department": "",
              "research_summary": "Imp profile edit",
              "profile_version": str(profile.profile_version)},
        headers=impersonation_headers(admin.id, pi.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web_impersonated"
    assert f"impersonated by admin {admin.id}" in (rev.change_summary or "")


async def test_profile_save_without_impersonation_stays_web(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        "/profile/save",
        data={"name": pi.name, "email": pi.email, "institution": "", "department": "",
              "research_summary": "Own edit", "profile_version": str(profile.profile_version)},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web"
    assert "impersonated" not in (rev.change_summary or "")


async def test_onboarding_save_under_impersonation_keeps_both_summaries(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, onboarding_complete=False)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        "/onboarding/save-profile",
        data={"email": pi.email, "research_summary": "Onboarding imp",
              "profile_version": str(profile.profile_version)},
        headers=impersonation_headers(admin.id, pi.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web_impersonated"
    assert "Profile saved during onboarding" in rev.change_summary
    assert f"impersonated by admin {admin.id}" in rev.change_summary


async def test_manager_profile_edit_under_impersonation_is_attributed(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "email": pi.email, "research_summary": "Manager imp edit",
              "profile_version": str(profile.profile_version)},
        headers=impersonation_headers(admin.id, manager.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]
    rev = await _latest_revision(db_session, agent)
    assert rev.mechanism == "web_impersonated"
    assert f"impersonated by admin {admin.id}" in (rev.change_summary or "")


async def test_simulation_start_under_impersonation_notes_the_audit_row(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post(
        "/admin/simulation/start", data={"max_runtime": "0", "max_proposals": "0"},
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    assert r.status_code == 302
    event = (await db_session.execute(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "simulation_start_requested")
    )).scalar_one()
    assert event.actor_user_id == worn.id
    assert event.payload["impersonation_note"] == f"impersonated by admin {real.id}"
    cmd = (await db_session.execute(
        select(SimulationCommand).where(SimulationCommand.command == "start")
    )).scalar_one()
    assert "impersonation_note" not in cmd.payload


async def test_simulation_start_without_impersonation_has_no_note(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await client.post(
        "/admin/simulation/start", data={"max_runtime": "0", "max_proposals": "0"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    event = (await db_session.execute(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "simulation_start_requested")
    )).scalar_one()
    assert "impersonation_note" not in event.payload


async def test_announce_settings_under_impersonation_notes_the_audit_row(client, db_session):
    real = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await client.post(
        "/admin/simulation/announce-settings", data={"channels": "", "disable": "true"},
        headers=impersonation_headers(real.id, worn.id), follow_redirects=False,
    )
    event = (await db_session.execute(
        select(AdminAuditEvent).where(
            AdminAuditEvent.action == "simulation_announce_channels_updated"
        )
    )).scalar_one()
    assert event.payload["impersonation_note"] == f"impersonated by admin {real.id}"
    assert event.payload["new"] == ""
