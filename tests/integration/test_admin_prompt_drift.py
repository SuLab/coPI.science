from datetime import UTC, datetime

import pytest

from src.models import USER_ROLE_ADMIN, SimulationProcessStatus
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_drift_banner_shows_three_rubric_hashes(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="drift-a@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="running", updated_at=datetime.now(UTC), detail={
        "prompt_drift": {"scout_hub": {"loaded": "aaaaaaaaaaaa", "on_disk": "bbbbbbbbbbbb"}},
        "rubric": {"loaded": "cccccccccccc", "on_disk": "dddddddddddd"},
    }))
    await db_session.commit()
    resp = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    assert "Prompt set changed on disk — restart to apply" in resp.text
    for h in (RUBRIC_CONTENT_HASH, "cccccccccccc", "dddddddddddd", "scout_hub"):
        assert h in resp.text


async def test_no_banner_without_drift(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="drift-b@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="running", updated_at=datetime.now(UTC),
                                           detail={"prompt_drift": {}}))
    await db_session.commit()
    resp = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    assert "restart to apply" not in resp.text
