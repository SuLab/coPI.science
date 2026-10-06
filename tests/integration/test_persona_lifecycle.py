"""The persona file across an agent's lifecycle (spec 2026-10-05 §6.4, D31)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, ProfileRevision
from src.services import persona_lifecycle, profile_export
from src.services.agent_form import agent_form_version
from src.services.persona_lifecycle import (
    archive_persona_file,
    export_after_lifecycle,
    persona_has_research_summary,
)
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    public, orphaned = tmp_path / "public", tmp_path / "orphaned"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", public)
    monkeypatch.setattr(persona_lifecycle, "ORPHANED_DIR", orphaned)
    public.mkdir()
    return public, orphaned


@pytest.mark.parametrize(("text", "expected"), [
    ("# X\n\n## Research Summary\n\nWords.\n", True),
    ("# X\n\n## Research Summary\n\n## Keywords\n\na\n", False),
    ("# X\n\n## Keywords\n\na\n", False),
    (None, False),
])
def test_persona_has_research_summary(text, expected):
    assert persona_has_research_summary(text) is expected


def test_archive_moves_the_file_and_ignores_a_missing_or_unsafe_one(dirs):
    public, orphaned = dirs
    (public / "gone.md").write_text("old", encoding="utf-8")
    moved = archive_persona_file("gone")
    assert moved is not None and moved.parent == orphaned and moved.read_text() == "old"
    assert not (public / "gone.md").exists()
    assert archive_persona_file("gone") is None
    assert archive_persona_file("../escape") is None


async def _grounded_lab(db, *, agent_id, status="pending"):
    pi = await factories.make_user(db)
    await factories.make_profile(db, user=pi, research_summary="Studies the thing.",
                                 evidence_pmid_count=10, evidence_pub_count=8)
    agent = await factories.make_agent(db, user=pi, agent_id=agent_id,
                                       bot_name=f"{agent_id.capitalize()}Bot", status=status)
    return pi, agent


async def test_a_lifecycle_export_archives_a_leftover_and_records_a_revision(db_session, dirs):
    public, orphaned = dirs
    pi, agent = await _grounded_lab(db_session, agent_id="lifea")
    (public / "lifea.md").write_text("# Somebody Else\n", encoding="utf-8")
    path = await export_after_lifecycle(
        db_session, pi.id, event="Agent created", actor_id=None, replace_leftover=True,
    )
    assert path == public / "lifea.md"
    assert "Studies the thing." in path.read_text(encoding="utf-8")
    assert len(list(orphaned.glob("lifea.*.md"))) == 1
    rev = (await db_session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
    )).scalar_one()
    assert rev.mechanism == "lifecycle_export" and rev.change_summary == "Agent created"


async def test_an_activation_export_skips_a_file_that_already_matches(db_session, dirs):
    pi, agent = await _grounded_lab(db_session, agent_id="lifeb")
    await export_after_lifecycle(db_session, pi.id, event="Agent created", actor_id=None,
                                 replace_leftover=True)
    assert await export_after_lifecycle(
        db_session, pi.id, event="Agent activated", actor_id=None,
    ) is None
    count = len((await db_session.execute(
        select(ProfileRevision.id).where(ProfileRevision.agent_registry_id == agent.id)
    )).all())
    assert count == 1


async def test_no_profile_means_no_export_but_the_leftover_is_archived(db_session, dirs):
    public, orphaned = dirs
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="lifec", status="pending")
    (public / "lifec.md").write_text("# Somebody Else\n", encoding="utf-8")
    assert await export_after_lifecycle(
        db_session, pi.id, event="Agent created", actor_id=None, replace_leftover=True,
    ) is None
    assert not (public / "lifec.md").exists()
    assert len(list(orphaned.glob("lifec.*.md"))) == 1


def _approve(agent, **extra):
    return {"agent_slug": agent.agent_id, "bot_name": agent.bot_name,
            "form_version": agent_form_version(agent), **extra}


async def test_rename_then_activate_exports_under_the_new_slug_and_activates(
    client, db_session, dirs,
):
    public, _ = dirs
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi, agent = await _grounded_lab(db_session, agent_id="oldslug")
    await export_after_lifecycle(db_session, pi.id, event="Agent created", actor_id=None,
                                 replace_leftover=True)
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data=_approve(agent, agent_slug="newslug"),
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.headers["location"] == "/admin/agents"
    await db_session.refresh(agent)
    assert agent.agent_id == "newslug" and agent.status == "active"
    assert persona_has_research_summary((public / "newslug.md").read_text(encoding="utf-8"))
    assert not (public / "oldslug.md").exists()


async def test_a_refused_activation_keeps_the_rename_and_says_so(client, db_session, dirs):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi, agent_id="noprof1",
                                       bot_name="Noprof1Bot", status="pending")
    r = await client.post(f"/admin/agents/{agent.id}/approve",
                          data=_approve(agent, agent_slug="noprof2", activation_override="1"),
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert "activation_blocked=1" in r.headers["location"]
    await db_session.refresh(agent)
    assert agent.agent_id == "noprof2" and agent.status == "pending"
    text = session_flashes(r)[-1]["text"]
    assert text.startswith("Renamed to noprof2 (saved). Activation refused:")
    assert "persona file profiles/public/noprof2.md is missing" in text


async def test_a_leftover_file_at_the_new_slug_is_archived(client, db_session, dirs):
    public, orphaned = dirs
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi, agent = await _grounded_lab(db_session, agent_id="leftold")
    (public / "leftnew.md").write_text("# Former Lab\n\n## Research Summary\n\nNot ours.\n",
                                       encoding="utf-8")
    await client.post(f"/admin/agents/{agent.id}/approve",
                      data=_approve(agent, agent_slug="leftnew"),
                      headers=auth_headers(admin.id), follow_redirects=False)
    assert "Not ours." not in (public / "leftnew.md").read_text(encoding="utf-8")
    assert len(list(orphaned.glob("leftnew.*.md"))) == 1


async def test_admin_link_publishes_the_new_owners_persona(client, db_session, dirs):
    public, orphaned = dirs
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, agent_id="linkme", status="pending")
    (public / "linkme.md").write_text("# Previous Owner\n", encoding="utf-8")
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi, research_summary="New owner's work.")
    r = await client.post(f"/admin/agents/{agent.id}/link", data={"user_id": str(pi.id)},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302
    assert "New owner's work." in (public / "linkme.md").read_text(encoding="utf-8")
    assert len(list(orphaned.glob("linkme.*.md"))) == 1
