"""Both deletion pages say the simulation logs keep profile text (spec 2026-10-05 D35)."""
import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_the_self_service_page_mentions_the_logs(client, db_session):
    pi = await factories.make_user(db_session)
    body = (await client.get("/profile/delete-account", headers=auth_headers(pi.id))).text
    assert "model-call logs are also kept" in body


async def test_the_admin_page_mentions_the_logs(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    body = (await client.get(f"/admin/users/{pi.id}", headers=auth_headers(admin.id))).text
    assert "simulation's model-call logs remain" in body
