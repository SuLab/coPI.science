"""RA-07: the admin agents page reads users once, not once per agent."""
import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_user_names_come_from_one_query(client, db_session, engine):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    for _ in range(5):
        await factories.make_agent(db_session, user=await factories.make_user(db_session))
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "FROM users" in statement:
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        r = await client.get("/admin/agents", headers=auth_headers(admin.id))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert r.status_code == 200
    user_list_queries = [s for s in seen if "WHERE users.id =" not in s]
    per_agent_lookups = [s for s in seen if "WHERE users.id =" in s]
    assert len(per_agent_lookups) <= 1  # the session/auth lookup of the admin
    assert len(user_list_queries) >= 1
