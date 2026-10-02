"""FN-02: under impersonation the onboarding form must prefill the impersonated
user's email, not the real admin's (saving the admin's address then failed as
"already in use")."""

import pytest

from src.models import USER_ROLE_ADMIN, Job
from tests import factories
from tests.integration._webui_helpers import impersonation_headers

pytestmark = pytest.mark.integration


async def test_onboarding_under_impersonation_prefills_the_worn_users_email(client, db_session):
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="real-admin@example.org"
    )
    pi = await factories.make_user(
        db_session, onboarding_complete=False, email="worn-pi@example.org"
    )
    await factories.make_profile(db_session, user=pi)
    db_session.add(Job(type="generate_profile", status="completed", user_id=pi.id, payload={}))
    await db_session.flush()
    r = await client.get("/onboarding", headers=impersonation_headers(admin.id, pi.id))
    assert r.status_code == 200
    assert 'value="worn-pi@example.org"' in r.text
    assert 'value="real-admin@example.org"' not in r.text
