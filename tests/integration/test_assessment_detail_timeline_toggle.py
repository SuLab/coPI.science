"""B-01: clamp toggles are re-measured when any <details> opens."""

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_detail_page_re_measures_clamps_on_toggle(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    seeded = await seed_interview(db_session)
    await db_session.commit()
    html = (await client.get(f"/admin/assessments/{seeded.assessment_id}",
                             headers=auth_headers(admin.id))).text
    assert "document.addEventListener('toggle'" in html
    assert "clampChecks.push" in html
    assert "}, true);" in html[html.index("document.addEventListener('toggle'"):]
