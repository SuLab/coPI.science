"""The Phase 4 edit rules (spec 2026-10-05 §6.4, §7): the version rule, absent fields, D24
caps, D60 names and the pi_name sync, D48 identical resubmission and the empty-profile
guard, through the four profile forms."""
import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_MANAGER, ProfileRevision, ResearcherProfile
from src.services import profile_export
from src.services.person_names import NAME_MAX_CHARS
from src.services.profile_limits import (
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_WORDS,
    TAG_LIST_MAX_ITEMS,
    TAG_MAX_CHARS,
)
from src.web.templating import make_templates
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_TAGS = ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"]
_SUMMARY = "Generated summary of the lab."


@pytest.fixture
def public(tmp_path, monkeypatch):
    out = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", out)
    return out


async def _lab(db, **profile_kw):
    pi = await factories.make_user(db, name="Jane Wang", institution="Johns Hopkins University")
    profile = await factories.make_profile(
        db, user=pi, research_summary=_SUMMARY, profile_version=1, **profile_kw,
    )
    agent = await factories.make_agent(db, user=pi, pi_name="Jane Wang", status="active")
    return pi, profile, agent


async def _post(client, db, kind, pi, agent, data):
    """POST one of the four profile forms as the actor that route admits."""
    if kind == "manager":
        manager = await factories.make_user(db, user_role=USER_ROLE_MANAGER)
        url, actor = f"/manager/pis/{pi.id}/profile", manager.id
        base = {"name": pi.name, "email": pi.email, "institution": pi.institution or "",
                "department": "", "jhu_tenure_start": ""}
    elif kind == "profile":
        url, actor = "/profile/save", pi.id
        base = {"name": pi.name, "email": pi.email, "institution": pi.institution or "",
                "department": ""}
    elif kind == "public":
        url, actor, base = f"/agent/{agent.agent_id}/public-profile/save", pi.id, {}
    else:
        url, actor, base = "/onboarding/save-profile", pi.id, {"email": pi.email}
    return await client.post(
        url, data={**base, **data}, headers=auth_headers(actor), follow_redirects=False,
    )


async def _revisions(db, agent) -> int:
    return await db.scalar(
        select(func.count()).select_from(ProfileRevision)
        .where(ProfileRevision.agent_registry_id == agent.id)
    )


@pytest.mark.parametrize("kind", ["manager", "profile", "public", "onboarding"])
async def test_an_empty_version_over_an_existing_profile_is_refused_on_every_route(
    client, db_session, public, kind,
):
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, kind, pi, agent, {
        "research_summary": "", "profile_version": "", "tag_fields": _TAGS,
    })
    assert r.status_code == 302
    assert "error=profile_version_missing" in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.research_summary == _SUMMARY
    assert profile.techniques == ["technique-a"]
    assert profile.profile_version == 1
    assert await _revisions(db_session, agent) == 0
    assert not (public / f"{agent.agent_id}.md").exists()


async def test_an_absent_summary_field_leaves_the_summary(client, db_session, public):
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, "profile", pi, agent, {"profile_version": "1"})
    assert r.headers["location"] == "/profile"
    await db_session.refresh(profile)
    assert profile.research_summary == _SUMMARY


@pytest.mark.parametrize(("data", "code"), [
    ({"research_summary": "word " * (SUMMARY_MAX_WORDS + 1)}, "summary_too_long"),
    ({"research_summary": "x" * (SUMMARY_MAX_CHARS + 1)}, "summary_too_long"),
    ({"tag_fields": ["techniques"],
      "techniques": [f"t{i}" for i in range(TAG_LIST_MAX_ITEMS + 1)]}, "too_many_tags"),
    ({"tag_fields": ["techniques"], "techniques": ["x" * (TAG_MAX_CHARS + 1)]}, "tag_too_long"),
    ({"tag_fields": ["techniques"], "techniques": ["# forged heading"]}, "tag_invalid"),
    ({"tag_fields": ["techniques"], "techniques": ["line\nbreak"]}, "tag_invalid"),
])
async def test_an_edit_over_the_caps_is_refused_and_writes_nothing(
    client, db_session, public, data, code,
):
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, "manager", pi, agent, {"profile_version": "1", **data})
    assert f"error={code}" in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.profile_version == 1 and profile.research_summary == _SUMMARY
    assert profile.techniques == ["technique-a"]


async def test_an_existing_over_long_tag_can_be_saved_again(client, db_session, public):
    long_tag = "y" * (TAG_MAX_CHARS + 50)
    pi, profile, agent = await _lab(db_session, techniques=[long_tag])
    r = await _post(client, db_session, "manager", pi, agent, {
        "profile_version": "1", "tag_fields": ["techniques"], "techniques": [long_tag, "base editing"],
    })
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    await db_session.refresh(profile)
    assert profile.techniques == [long_tag, "base editing"]


async def test_an_invalid_new_name_is_refused(client, db_session, public):
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, "manager", pi, agent, {
        "profile_version": "1", "name": "Jane <b>Wang</b>",
    })
    assert "error=invalid_name" in r.headers["location"]
    await db_session.refresh(pi)
    assert pi.name == "Jane Wang"


async def test_a_name_edit_syncs_the_agent_pi_name(client, db_session, public):
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, "manager", pi, agent, {
        "profile_version": "1", "name": "Jane Q. Wang-Ó",
    })
    assert r.headers["location"] == f"/manager/pis/{pi.id}"
    await db_session.refresh(pi)
    await db_session.refresh(agent)
    assert pi.name == "Jane Q. Wang-Ó"
    assert agent.pi_name == "Jane Q. Wang-Ó"


async def test_an_unchanged_legacy_name_is_not_revalidated(client, db_session, public):
    pi, profile, agent = await _lab(db_session)
    pi.name = "0000-0002-1825-0097"  # outside D60; saved before validation existed
    await db_session.flush()
    r = await _post(client, db_session, "manager", pi, agent, {
        "profile_version": "1", "research_summary": "An edited summary.",
    })
    assert r.headers["location"] == f"/manager/pis/{pi.id}"


async def test_an_identical_resubmission_shows_success_and_writes_nothing(
    client, db_session, public,
):
    pi, profile, agent = await _lab(db_session)
    form = {"profile_version": "1", "research_summary": "An edited summary.",
            "tag_fields": _TAGS, "techniques": ["base editing"]}
    first = await _post(client, db_session, "manager", pi, agent, form)
    assert first.headers["location"] == f"/manager/pis/{pi.id}"
    second = await _post(client, db_session, "manager", pi, agent, form)
    assert second.headers["location"] == f"/manager/pis/{pi.id}"
    assert session_flashes(second)[-1]["text"] == "Profile saved."
    await db_session.refresh(profile)
    assert profile.profile_version == 2
    assert await _revisions(db_session, agent) == 1


async def test_a_different_resubmission_after_a_clash_is_still_refused(
    client, db_session, public,
):
    pi, profile, agent = await _lab(db_session)
    await _post(client, db_session, "manager", pi, agent,
                {"profile_version": "1", "research_summary": "First edit."})
    r = await _post(client, db_session, "manager", pi, agent,
                    {"profile_version": "1", "research_summary": "Second edit."})
    assert "error=profile_changed" in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.research_summary == "First edit." and profile.profile_version == 2


async def test_a_user_field_save_without_a_profile_creates_no_profile(client, db_session):
    pi = await factories.make_user(db_session, name="Jane Wang")
    r = await client.post("/profile/save", data={
        "name": "Jane Q. Wang", "email": pi.email, "institution": "", "department": "",
    }, headers=auth_headers(pi.id), follow_redirects=False)
    assert r.headers["location"] == "/profile"
    await db_session.refresh(pi)
    assert pi.name == "Jane Q. Wang"
    assert await db_session.scalar(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == pi.id)
    ) is None


async def test_public_profile_save_reports_the_export(client, db_session, public):
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, "public", pi, agent,
                    {"profile_version": "1", "research_summary": "New words."})
    assert session_flashes(r)[-1]["text"] == "Public profile saved and exported."
    assert "New words." in (public / f"{agent.agent_id}.md").read_text(encoding="utf-8")


async def test_public_profile_save_reports_a_failed_export(
    client, db_session, tmp_path, monkeypatch,
):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(profile_export, "PROFILES_DIR", blocker / "public")
    pi, profile, agent = await _lab(db_session)
    r = await _post(client, db_session, "public", pi, agent,
                    {"profile_version": "1", "research_summary": "New words."})
    flash = session_flashes(r)[-1]
    assert flash["kind"] == "error"
    assert flash["text"].startswith("Public profile saved, but the persona file is out of date")


def test_the_messages_state_the_limits():
    macro = make_templates().env.get_template("_profile_errors.html").module.profile_error_text
    assert str(SUMMARY_MAX_WORDS) in macro("summary_too_long")
    assert str(TAG_LIST_MAX_ITEMS) in macro("too_many_tags")
    assert str(TAG_MAX_CHARS) in macro("tag_too_long")
    assert str(NAME_MAX_CHARS) in macro("invalid_name")
    assert str(macro("not_a_code")) == ""


async def test_agent_pi_name_is_untouched_by_a_blank_name(client, db_session, public):
    pi, profile, agent = await _lab(db_session)
    await _post(client, db_session, "manager", pi, agent, {"profile_version": "1", "name": ""})
    await db_session.refresh(agent)
    assert agent.pi_name == "Jane Wang"
