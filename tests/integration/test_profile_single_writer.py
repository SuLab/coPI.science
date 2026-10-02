import pytest
from sqlalchemy import select

from src.models import ProfileRevision, ResearcherProfile
from src.services import profile_export
from src.services.profile_edit import PROFILE_FIELDS, apply_profile_edits
from src.services.profile_export import export_profile_to_markdown
from src.services.tenure_scope import scoped_publications_for_export
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_FORM = {"research_summary": "New summary", "techniques": ["a", "b"], "experimental_models": ["m"],
         "disease_areas": ["d"], "key_targets": ["k"], "keywords": ["x", "y"]}
#: What a rendered form posts: the tag lists as repeated fields plus one marker per widget.
_POSTED = {**_FORM, "tag_fields": ["techniques", "experimental_models", "disease_areas",
                                   "key_targets", "keywords"]}


async def test_missing_institution_is_left_unchanged(db_session, tmp_path, monkeypatch):
    """Review Focus 2: the public-profile and onboarding forms carry no institution."""
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await factories.make_user(db_session, institution="Johns Hopkins University", department="Bio")
    await factories.make_profile(db_session, user=user, profile_version=3)
    agent = await factories.make_agent(db_session, user=user, agent_id="keep1")
    err = await apply_profile_edits(db_session, target_user=user, changed_by_user_id=user.id,
                                    form=_FORM, expected_version=3, export_agent=agent)
    assert err is None
    await db_session.refresh(user)
    assert (user.institution, user.department) == ("Johns Hopkins University", "Bio")
    text = (tmp_path / "keep1.md").read_text()
    assert "**Institution:** Johns Hopkins University" in text and "**Department:** Bio" in text


async def test_blank_institution_from_a_form_that_carries_it_clears_it(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await factories.make_user(db_session, institution="X")
    await factories.make_profile(db_session, user=user, profile_version=1)
    await apply_profile_edits(db_session, target_user=user, changed_by_user_id=user.id,
                              form={**_FORM, "institution": ""}, expected_version=1)
    await db_session.refresh(user)
    assert user.institution is None


async def test_stale_version_is_refused(db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, profile_version=5)
    assert await apply_profile_edits(db_session, target_user=user, changed_by_user_id=user.id,
                                     form=_FORM, expected_version=4) == "profile_changed"


async def test_onboarding_requires_an_email(db_session):
    user = await factories.make_user(db_session)
    assert await apply_profile_edits(db_session, target_user=user, changed_by_user_id=user.id,
                                     form={**_FORM, "email": " "}, expected_version=None,
                                     email_required=True) == "email_required"


def test_fields_tuple():
    assert PROFILE_FIELDS == ("research_summary", "techniques", "experimental_models",
                              "disease_areas", "key_targets", "keywords")


def _routes(user, agent):
    full = {"name": user.name, "email": user.email, "institution": "JHU", "department": "Bio", **_POSTED}
    return [
        ("/profile/save", user, full, None),
        ("/onboarding/save-profile", user, {"email": user.email, **_POSTED}, "Profile saved during onboarding"),
        (f"/agent/{agent.agent_id}/public-profile/save", user, dict(_POSTED), None),
    ]


@pytest.mark.parametrize("which", [0, 1, 2, 3])
async def test_each_route_exports_the_same_bytes_as_the_export_function(
    client, db_session, tmp_path, monkeypatch, which
):
    """Per-route byte parity: the file the route leaves behind is what the export
    function renders for the saved profile and the in-tenure publications, and the
    route records one `web` revision with its own change_summary."""
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    user = await factories.make_user(db_session, institution="JHU", department="Bio")
    await factories.make_profile(db_session, user=user, profile_version=1)
    agent = await factories.make_agent(db_session, user=user, agent_id="route1")
    manager = await factories.make_user(db_session, user_role="manager")
    if which < 3:
        route, actor, form, summary = _routes(user, agent)[which]
    else:
        full = {"name": user.name, "email": user.email, "institution": "JHU", "department": "Bio", **_POSTED}
        route, actor, form, summary = f"/manager/pis/{user.id}/profile", manager, full, None
    r = await client.post(route, data={**form, "profile_version": "1"},
                          headers=auth_headers(actor.id), follow_redirects=False)
    assert r.status_code == 302
    written = (tmp_path / "route1.md").read_text()
    profile = (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id))).scalar_one()
    await db_session.refresh(profile)
    pubs = await scoped_publications_for_export(db_session, user.id, "route1")
    assert written == export_profile_to_markdown(user, profile, "route1", publications=pubs).read_text()
    revs = (await db_session.execute(select(ProfileRevision).where(
        ProfileRevision.agent_registry_id == agent.id))).scalars().all()
    assert [(v.mechanism, v.change_summary, v.content) for v in revs] == [("web", summary, written)]
