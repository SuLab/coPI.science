"""A-15: users.name / institution / department are String(255). An overlong value
used to reach the INSERT and 500; it is now a form error and nothing is written."""

import re

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, User
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _form(pi, **overrides):
    data = {"name": pi.name, "email": pi.email, "institution": "", "department": "",
            "research_summary": "Studies the thing.",
            "profile_version": "1"}  # R1-a: make_profile's default version, current for every case below
    data.update(overrides)
    return data


@pytest.mark.parametrize("field", ["name", "institution", "department"])
async def test_an_overlong_field_is_a_form_error_not_a_500(client, db_session, field):
    pi = await factories.make_user(db_session, name="Short Name")
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        "/profile/save", data=_form(pi, **{field: "N" * 256}),
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=field_too_long" in r.headers["location"]
    row = (await db_session.execute(select(User).where(User.id == pi.id))).scalar_one()
    await db_session.refresh(row)
    assert row.name == "Short Name"
    page = await client.get(r.headers["location"], headers=auth_headers(pi.id))
    assert "255 characters" in page.text


async def test_exactly_255_characters_is_accepted(client, db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        "/profile/save", data=_form(pi, institution="I" * 255),
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302 and "error=" not in r.headers["location"]


async def test_the_manager_form_reports_the_same_error(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/profile", data={"name": "M" * 300, "research_summary": "x",
                    "profile_version": "1"},  # R1-a: current version of the seeded profile
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=field_too_long" in r.headers["location"]
    page = await client.get(r.headers["location"], headers=auth_headers(manager.id))
    assert "255 characters" in page.text


@pytest.mark.parametrize("field", ["name", "institution", "department"])
async def test_both_forms_cap_the_inputs_at_their_field_limit(client, db_session, field):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    limit = 100 if field == "name" else 255
    pattern = re.compile(
        rf'<input(?=[^>]*\bname="{field}")(?=[^>]*\bmaxlength="{limit}")[^>]*>'
    )
    own = await client.get("/profile/edit", headers=auth_headers(pi.id))
    managed = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))
    assert pattern.search(own.text)
    assert pattern.search(managed.text)
