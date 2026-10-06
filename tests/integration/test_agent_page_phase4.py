"""The agent page's Phase 4 changes (spec 2026-10-05 §6.4): writes need the PI surfaces
(D25), and /agent/request publishes the persona after its commit (D31)."""
import pytest

from src.models import USER_ROLE_MANAGER
from src.services import persona_lifecycle, profile_export
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def public(tmp_path, monkeypatch):
    out = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", out)
    monkeypatch.setattr(persona_lifecycle, "ORPHANED_DIR", tmp_path / "orphaned")
    return out


async def test_an_owner_who_may_not_use_pi_surfaces_cannot_save(client, db_session, public):
    owner = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_profile(db_session, user=owner)
    agent = await factories.make_agent(db_session, user=owner, status="active")
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save",
        data={"research_summary": "x", "profile_version": "1"},
        headers=auth_headers(owner.id), follow_redirects=False,
    )
    assert r.status_code == 403
    r = await client.post(
        f"/agent/{agent.agent_id}/delegates/invite", data={"emails": "a@example.edu"},
        headers=auth_headers(owner.id), follow_redirects=False,
    )
    assert r.status_code == 403


async def test_agent_request_exports_the_persona_and_archives_a_leftover(
    client, db_session, public, tmp_path,
):
    pi = await factories.make_user(db_session, name="Ada Quillfeather", onboarding_complete=True)
    await factories.make_profile(db_session, user=pi, research_summary="Engines and notes.")
    public.mkdir(parents=True)
    leftover = public / "quillfeather.md"
    leftover.write_text("# Someone Else Lab\n", encoding="utf-8")
    r = await client.post("/agent/request", headers=auth_headers(pi.id), follow_redirects=False)
    assert r.status_code == 302
    text = leftover.read_text(encoding="utf-8")
    assert "## Research Summary" in text and "Engines and notes." in text
    assert len(list((tmp_path / "orphaned").glob("quillfeather.*.md"))) == 1
