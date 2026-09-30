"""/settings is the account page only."""

import pytest

from src.models import EmailNotificationPreference
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_the_settings_page_is_the_account_section_only(client, db_session):
    u = await factories.make_user(db_session)
    # A user who saved preferences before the retirement still gets the page.
    db_session.add(EmailNotificationPreference(
        user_id=u.id, category="status_overview", enabled=True, frequency="weekly",
    ))
    await db_session.flush()
    r = await client.get("/settings", headers=auth_headers(u.id))
    assert r.status_code == 200
    assert "Delete account" in r.text
    assert "Email Notifications" not in r.text
    assert 'action="/settings/save"' not in r.text


async def test_the_notification_routes_are_gone(client, db_session):
    u = await factories.make_user(db_session)
    await db_session.flush()
    assert (await client.post("/settings/save", headers=auth_headers(u.id))).status_code in (404, 405)
    assert (await client.get("/settings/unsubscribe/anything")).status_code == 404
