"""A save carries the profile_version its form was rendered with; a regeneration
or another edit that saved first makes it refuse, not overwrite."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, ResearcherProfile
from src.services import profile_export
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_FIELDS = {
    "research_summary": "Edited summary.", "techniques": ["a", "b"], "experimental_models": ["m"],
    "disease_areas": ["d"], "key_targets": ["k"], "keywords": ["w"],
    "tag_fields": ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"],
}


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    out = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", out)
    return out


async def _pi(db_session, n):
    user = await factories.make_user(db_session, email=f"p015-{n}@example.org")
    await factories.make_profile(db_session, user=user, research_summary="Stored.", profile_version=3)
    agent = await factories.make_agent(db_session, user=user, agent_id=f"p015agent{n}")
    # Committed (a savepoint release in the test session) so a refused save's
    # rollback in the route discards only the save, not the seeded rows.
    await db_session.commit()
    await db_session.refresh(user)
    await db_session.refresh(agent)
    return user, agent


async def _profile(db_session, user_id):
    return (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _manager(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)


def _routes(user, agent):
    common = {"name": user.name, "email": user.email, "institution": "", "department": ""}
    return {
        "profile_save": ("/profile/save", {**_FIELDS, **common}, user.id),
        "public_profile": (f"/agent/{agent.agent_id}/public-profile/save", dict(_FIELDS), user.id),
        "onboarding": ("/onboarding/save-profile", {**_FIELDS, "email": user.email}, user.id),
    }


@pytest.mark.parametrize("route", ["profile_save", "public_profile", "onboarding", "manager"])
async def test_a_stale_form_is_refused(client, db_session, export_dir, route):
    user, agent = await _pi(db_session, route)
    if route == "manager":
        manager = await _manager(db_session)
        path, data, actor = (
            f"/manager/pis/{user.id}/profile",
            {**_FIELDS, "name": user.name, "email": user.email, "institution": "",
             "department": "", "jhu_tenure_start": ""},
            manager.id,
        )
    else:
        path, data, actor = _routes(user, agent)[route]
    user_id = user.id  # the refused save's rollback expires the shared session's objects
    resp = await client.post(path, data={**data, "profile_version": "2"}, headers=auth_headers(actor))
    assert resp.status_code == 302 and "profile_changed" in resp.headers["location"]
    profile = await _profile(db_session, user_id)
    assert (profile.research_summary, profile.profile_version) == ("Stored.", 3)


async def test_a_current_form_saves_and_bumps_the_version(client, db_session, export_dir):
    user, agent = await _pi(db_session, "current")
    path, data, actor = _routes(user, agent)["profile_save"]
    resp = await client.post(path, data={**data, "profile_version": "3"}, headers=auth_headers(actor))
    assert resp.headers["location"] == "/profile"
    profile = await _profile(db_session, user.id)
    assert (profile.research_summary, profile.profile_version) == ("Edited summary.", 4)


async def test_the_guarded_save_exports_the_same_bytes_as_the_legacy_save(
    client, db_session, export_dir,
):
    """The forms now always carry the field, so the guarded write path IS the
    normal path: it must export exactly what the unguarded one does."""
    a_user, a_agent = await _pi(db_session, "parity-a")
    b_user, b_agent = await _pi(db_session, "parity-b")
    for user, agent, extra in ((a_user, a_agent, {}), (b_user, b_agent, {"profile_version": "3"})):
        path, data, actor = _routes(user, agent)["profile_save"]
        data = {**data, "name": "Same Name", **extra}
        await client.post(path, data=data, headers=auth_headers(actor))
    a = (export_dir / f"{a_agent.agent_id}.md").read_text(encoding="utf-8")
    b = (export_dir / f"{b_agent.agent_id}.md").read_text(encoding="utf-8")
    assert a == b
