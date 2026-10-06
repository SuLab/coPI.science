"""The manager PI page's Phase 4 parts (spec 2026-10-05 §6.4): the Edit Profile form only
with a profile, the revision history (D61), provisional tenure (U-9), and the persona
exports after Add-PI, activation and unmute (D31)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, USER_ROLE_REVIEWER, ProfileRevision
from src.services import persona_lifecycle, profile_export
from src.services.jhu_rules import set_provisional_tenure_start
from src.services.persona_lifecycle import export_after_lifecycle
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.persona_support import write_persona

pytestmark = pytest.mark.integration


@pytest.fixture
def public(tmp_path, monkeypatch):
    out = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", out)
    monkeypatch.setattr(persona_lifecycle, "ORPHANED_DIR", tmp_path / "orphaned")
    return out


async def test_manager_edit_form_renders_only_with_a_profile(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    bare = await factories.make_user(db_session)
    body = (await client.get(f"/manager/pis/{bare.id}", headers=auth_headers(mgr.id))).text
    assert f'action="/manager/pis/{bare.id}/profile"' not in body
    assert "The Edit Profile form appears once the profile exists." in body
    await factories.make_profile(db_session, user=bare, profile_version=4)
    body = (await client.get(f"/manager/pis/{bare.id}", headers=auth_headers(mgr.id))).text
    assert f'action="/manager/pis/{bare.id}/profile"' in body
    assert 'name="profile_version" value="4"' in body


async def test_revision_history_is_staff_only_and_lists_revisions(client, db_session, public):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, research_summary="Unique summary words.")
    await factories.make_agent(db_session, user=pi, status="pending")
    await export_after_lifecycle(db_session, pi.id, event="Agent created", actor_id=mgr.id,
                                 replace_leftover=True)
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "Persona revisions" in body and "lifecycle_export" in body
    assert "Agent created" in body and "Unique summary words." in body
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(rev.id))).text
    assert "Persona revisions" not in body


async def test_provisional_tenure_is_labelled(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    await set_provisional_tenure_start(db_session, pi.id, 2016)
    await db_session.flush()
    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(mgr.id))).text
    assert "used 2016 provisionally" in body


async def test_manager_activation_reexports_after_commit(client, db_session, public):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, research_summary="Builds organoid panels.",
                                 evidence_pmid_count=10, evidence_pub_count=8)
    agent = await factories.make_agent(db_session, user=pi, agent_id="mgract",
                                       status="pending", slack_bot_token="xoxb-mgract")
    write_persona(agent.agent_id)   # a stale file: the gate passes, the export rewrites it
    r = await client.post(f"/manager/pis/{pi.id}/activate", headers=auth_headers(mgr.id),
                          follow_redirects=False)
    assert r.headers["location"] == f"/manager/pis/{pi.id}?activated=1"
    assert "Builds organoid panels." in (public / "mgract.md").read_text(encoding="utf-8")
    assert (await db_session.execute(select(ProfileRevision.mechanism).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all() == ["lifecycle_export"]


async def test_manager_activation_refuses_without_a_persona_file(client, db_session, public):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, evidence_pmid_count=10,
                                 evidence_pub_count=8)
    await factories.make_agent(db_session, user=pi, agent_id="mgrnofile", status="pending",
                               slack_bot_token="xoxb-mgrnofile")
    r = await client.post(f"/manager/pis/{pi.id}/activate", headers=auth_headers(mgr.id),
                          follow_redirects=False)
    assert "activation_blocked=1" in r.headers["location"]
    page = await client.get(r.headers["location"], headers=auth_headers(mgr.id))
    assert "persona file profiles/public/mgrnofile.md is missing" in page.text
