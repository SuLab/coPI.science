# tests/integration/test_impersonation_guards.py
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, ProfileRevision, User
from src.services import profile_export
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _imp(admin, target):
    h = auth_headers(admin.id)
    h["Cookie"] += f"; copi-impersonate={target.id}"
    return h


async def test_role_change_is_refused_while_impersonating(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    victim = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.post(f"/admin/users/{victim.id}/role", data={"user_role": USER_ROLE_MANAGER},
                          headers=_imp(admin, other_admin), follow_redirects=False)
    assert r.status_code == 403
    await db_session.refresh(victim)
    assert victim.user_role == USER_ROLE_PI


async def test_public_profile_save_under_impersonation_is_attributed(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi, profile_version=1)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(f"/agent/{agent.agent_id}/public-profile/save",
                          data={"research_summary": "Imp edit", "techniques": "", "experimental_models": "",
                                "disease_areas": "", "key_targets": "", "keywords": "",
                                "profile_version": str(profile.profile_version)},
                          headers=_imp(admin, pi), follow_redirects=False)
    assert r.status_code == 302
    rev = (await db_session.execute(select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent.id)
                                    .order_by(ProfileRevision.created_at.desc()))).scalars().first()
    assert rev.mechanism == "web_impersonated"
    assert f"impersonated by admin {admin.id}" in (rev.change_summary or "")


def test_reviews_uses_the_shared_helpers():
    import src.routers.reviews as reviews
    from src import dependencies
    assert reviews.refuse_impersonation is dependencies.refuse_impersonation
    assert reviews.recorded_by is dependencies.recorded_by
