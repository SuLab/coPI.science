"""C-18 (jobs filters from the enum, unknown value refused without a 500) and C-20
(start limits are >= 0 on the server)."""

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, Job, SimulationCommand
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_jobs_filters_offer_every_enum_value(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    html = (await client.get("/admin/jobs", headers=auth_headers(admin.id))).text
    for value in (*Job.__table__.c.type.type.enums, *Job.__table__.c.status.type.enums):
        assert f'value="{value}"' in html, value


@pytest.mark.parametrize("query", ["status_filter=nonsense", "type_filter=nonsense"])
async def test_an_unknown_jobs_filter_is_a_400(client, db_session, query):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get(f"/admin/jobs?{query}", headers=auth_headers(admin.id))
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["max_runtime", "max_proposals"])
async def test_a_negative_start_limit_is_refused_server_side(client, db_session, field):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    data = {"max_runtime": "0", "max_proposals": "0", field: "-5"}
    r = await client.post("/admin/simulation/start", data=data, headers=auth_headers(admin.id))
    assert r.status_code == 422
    starts = await db_session.scalar(
        select(func.count()).select_from(SimulationCommand).where(SimulationCommand.command == "start")
    )
    assert starts == 0
