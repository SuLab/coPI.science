"""A-16: impersonation looks an account up by ORCID and never creates one."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, Job, User
from tests import factories
from tests.integration._webui_helpers import session_from
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_impersonating_an_unknown_orcid_is_a_404_and_creates_nothing(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    fetch = AsyncMock(return_value={"name": "Ghost"})
    with patch("src.services.pi_onboarding.fetch_orcid_profile", new=fetch):
        r = await client.post(
            "/admin/impersonate", data={"orcid": "0000-0003-9999-0001"},
            headers=auth_headers(admin.id), follow_redirects=False,
        )
    assert r.status_code == 404
    fetch.assert_not_awaited()
    assert (await db_session.execute(
        select(User).where(User.orcid == "0000-0003-9999-0001")
    )).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(Job).where(Job.type == "generate_profile")
    )).scalars().all() == []


async def test_impersonating_by_orcid_url_finds_the_existing_user(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    target = await factories.make_user(db_session, name="Url Target", orcid="0000-0003-0000-002X")
    r = await client.post(
        "/admin/impersonate", data={"orcid": "https://orcid.org/0000-0003-0000-002x"},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    page = await client.get("/profile", headers=session_from(r))
    assert page.status_code == 200
    assert f"Viewing as {target.name}" in page.text
