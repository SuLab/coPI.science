# tests/integration/test_admin_agent_form.py
import pytest

from src.models import USER_ROLE_ADMIN
from src.services.agent_form import agent_form_version
from src.services.pi_companies import export_companies_file
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers
from tests.pi_company_support import companies_dir, seed_company

pytestmark = pytest.mark.integration


async def _setup(db_session, **agent_kw):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, **agent_kw)
    return admin, agent


async def test_stale_form_is_refused(client, db_session):
    admin, agent = await _setup(db_session, status="inactive", slack_bot_token="xoxb-old")
    stale = agent_form_version(agent)
    agent.bot_name = "RenamedElsewhereBot"
    await db_session.flush()
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data={"bot_name": "MineBot", "agent_status": "inactive", "form_version": stale},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302 and "error=stale_form" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.bot_name == "RenamedElsewhereBot"


async def test_token_not_rendered_and_blank_keeps_it(client, db_session):
    admin, agent = await _setup(db_session, status="inactive", slack_bot_token="xoxb-secret-123")
    page = await client.get(f"/admin/agents/{agent.id}", headers=auth_headers(admin.id))
    assert "xoxb-secret-123" not in page.text
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data={"bot_name": agent.bot_name, "agent_status": "inactive",
                                "form_version": agent_form_version(agent), "replace_slack_bot_token": ""},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302
    await db_session.refresh(agent)
    assert agent.slack_bot_token == "xoxb-secret-123"


async def test_slug_is_read_only_after_creation(client, db_session):
    admin, agent = await _setup(db_session, status="inactive", agent_id="fixedslug")
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data={"agent_slug": "newslug", "bot_name": agent.bot_name, "agent_status": "inactive",
                                "form_version": agent_form_version(agent)},
                          headers=auth_headers(admin.id), follow_redirects=False)
    await db_session.refresh(agent)
    assert agent.agent_id == "fixedslug" and "error=slug_read_only" in r.headers["location"]


async def test_pending_slug_collision_is_a_form_error(client, db_session):
    admin, agent = await _setup(db_session, status="pending", agent_id="pendingone")
    await factories.make_agent(db_session, agent_id="taken")
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data={"agent_slug": "taken", "bot_name": agent.bot_name,
                                "form_version": agent_form_version(agent), "activation_override": "1"},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302 and "error=slug_taken" in r.headers["location"]


async def test_provision_error_is_quoted(client, db_session, monkeypatch):
    admin, agent = await _setup(db_session, status="pending")
    from src.services import admin_provisioning

    async def boom(*a, **k):
        raise admin_provisioning.ProvisioningError("bad & worse #fragment")

    monkeypatch.setattr(admin_provisioning, "start_provisioning", boom)
    r = await client.post(f"/admin/agents/{agent.id}/slack/provision", headers=auth_headers(admin.id),
                          follow_redirects=False)
    assert r.headers["location"] == f"/admin/agents/{agent.id}"
    assert session_flashes(r) == [
        {"text": "Slack provisioning failed: bad & worse #fragment", "kind": "error"}
    ]


@pytest.mark.parametrize("slug", ["../escape", "has space", "x" * 51, "dot.slug"])
async def test_pending_slug_outside_the_safe_charset_is_refused(client, db_session, slug):
    """The slug names profiles/public/<slug>.md and must pass user_deletion's rule."""
    admin, agent = await _setup(db_session, status="pending", agent_id="pendingtwo")
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data={"agent_slug": slug, "bot_name": agent.bot_name,
                                "form_version": agent_form_version(agent), "activation_override": "1"},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302 and "error=invalid_slug" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.agent_id == "pendingtwo"


async def test_renaming_a_pending_agent_moves_its_companies_file(client, db_session, monkeypatch, tmp_path):
    """The hub reads profiles/private/companies/<agent_id>.md: a rename removes the old
    id's file and writes the confirmed rows under the new id."""
    target = companies_dir(monkeypatch, tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, status="pending", agent_id="oldslug")
    await seed_company(db_session, pi, status="confirmed")
    await export_companies_file(db_session, pi.id)
    assert (target / "oldslug.md").exists()

    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data={"agent_slug": "newslug", "bot_name": agent.bot_name,
                                "form_version": agent_form_version(agent), "activation_override": "1"},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/agents"
    await db_session.refresh(agent)
    assert agent.agent_id == "newslug"
    assert not (target / "oldslug.md").exists()
    assert "- DELFI Diagnostics: founder" in (target / "newslug.md").read_text()


async def test_page_parameters_are_bounded(client, db_session):
    """A huge ?page= is a 422, never an OFFSET overflow."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get("/admin/jobs?page=99999999999999999999", headers=auth_headers(admin.id))
    assert r.status_code == 422
