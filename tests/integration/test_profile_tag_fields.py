"""D-16: each tag is posted as its own field, so a tag with a comma survives a save."""
from pathlib import Path

import pytest
from sqlalchemy import select
from starlette.datastructures import FormData

from src.models import USER_ROLE_MANAGER, ResearcherProfile
from src.services import profile_export
from src.services.profile_edit import apply_profile_edits, list_fields_from_form
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

TAG_JS = Path(__file__).resolve().parents[2] / "static/js/tag_widget.js"
_ALL = ["techniques", "experimental_models", "disease_areas", "key_targets", "keywords"]


@pytest.fixture(autouse=True)
def _exports_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)


async def _profile(db_session, user_id):
    return (await db_session.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )).scalar_one()


def test_list_fields_read_repeated_values_and_the_marker():
    form = FormData([
        ("tag_fields", "techniques"), ("techniques", "1,2-dichloroethane"),
        ("techniques", "cryo-EM"), ("tag_fields", "keywords"),
    ])
    assert list_fields_from_form(form) == {
        "techniques": ["1,2-dichloroethane", "cryo-EM"],
        "experimental_models": None,
        "disease_areas": None,
        "key_targets": None,
        "keywords": [],
    }


async def test_a_comma_string_is_refused_by_the_writer(db_session):
    user = await factories.make_user(db_session)
    with pytest.raises(TypeError):
        await apply_profile_edits(
            db_session, target_user=user, changed_by_user_id=user.id,
            form={"techniques": "a, b"}, expected_version=None,
        )


async def test_a_comma_inside_a_tag_survives_the_save(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, techniques=["old"])
    r = await client.post(
        "/profile/save",
        data={
            "name": user.name, "email": user.email, "research_summary": "s",
            "tag_fields": _ALL,
            "techniques": ["1,2-dichloroethane", " cryo-EM ", ""],
            "keywords": ["kinase"],
        },
        headers=auth_headers(user.id), follow_redirects=False,
    )
    assert r.headers["location"] == "/profile?saved=1"
    profile = await _profile(db_session, user.id)
    await db_session.refresh(profile)
    assert profile.techniques == ["1,2-dichloroethane", "cryo-EM"]
    assert profile.keywords == ["kinase"]
    assert profile.disease_areas == []


async def test_a_form_without_the_widget_leaves_the_tags_alone(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, techniques=["kept, intact"])
    await client.post(
        "/profile/save",
        data={"name": user.name, "email": user.email, "research_summary": "s",
              "techniques": "a, b"},
        headers=auth_headers(user.id),
    )
    profile = await _profile(db_session, user.id)
    await db_session.refresh(profile)
    assert profile.techniques == ["kept, intact"]


async def test_the_manager_form_saves_tags_whole(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile",
        data={"name": pi.name, "email": pi.email, "research_summary": "s",
              "tag_fields": ["key_targets"], "key_targets": ["PD-1, PD-L1 axis"]},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/pis/{pi.id}?saved=1"
    profile = await _profile(db_session, pi.id)
    await db_session.refresh(profile)
    assert profile.key_targets == ["PD-1, PD-L1 axis"]


async def test_the_edit_page_renders_one_hidden_input_per_tag(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, techniques=["1,2-dichloroethane", "cryo-EM"])
    r = await client.get("/profile/edit", headers=auth_headers(user.id))
    assert '<input type="hidden" name="techniques" value="1,2-dichloroethane">' in r.text
    assert '<input type="hidden" name="techniques" value="cryo-EM">' in r.text
    assert '<input type="hidden" name="tag_fields" value="techniques">' in r.text
    assert 'aria-label="Remove 1,2-dichloroethane"' in r.text
    assert '<label for="tag-input-techniques"' in r.text
    assert 'src="/static/js/tag_widget.js"' in r.text
    assert "1,2-dichloroethane, cryo-EM" not in r.text


def test_the_widget_script_adds_a_hidden_input_and_never_joins():
    js = TAG_JS.read_text()
    assert 'hidden.name = name;' in js
    assert 'btn.setAttribute("aria-label", "Remove " + text);' in js
    assert ".join(" not in js
