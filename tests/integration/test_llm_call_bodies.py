"""C-16: the LLM-calls list never carries prompt or response bodies; one call's
bodies load through an admin-only fragment. C-19, C-21, C-22 ride along."""

import uuid

import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _seed(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    log = await factories.make_llm_call_log(
        db_session, run=run, phase="thread_reply", system_prompt="SYSTEM-PROMPT-BODY",
        messages_json=[{"role": "user", "content": "USER-MESSAGE-BODY"}],
        response_text="RESPONSE-BODY", latency_ms=900.0,
        call_stats=[{"latency_ms": 100.0}, {"latency_ms": 300.0}],
    )
    return admin, run, log


async def test_the_list_defers_the_bodies(client, db_session, engine):
    admin, run, log = await _seed(db_session)
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        html = (await client.get(f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id))).text
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    for body in ("SYSTEM-PROMPT-BODY", "USER-MESSAGE-BODY", "RESPONSE-BODY"):
        assert body not in html
    assert f'href="/admin/activity/{run.id}/llm-calls/{log.id}/bodies"' in html
    assert "data-lazy-fragment" in html
    assert not any("llm_call_logs.system_prompt" in s for s in seen)
    assert not any("llm_call_logs.messages_json" in s for s in seen)


async def test_the_fragment_loads_one_calls_bodies(client, db_session):
    admin, run, log = await _seed(db_session)
    r = await client.get(f"/admin/activity/{run.id}/llm-calls/{log.id}/bodies", headers=auth_headers(admin.id))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    for body in ("SYSTEM-PROMPT-BODY", "USER-MESSAGE-BODY", "RESPONSE-BODY"):
        assert body in r.text
    assert "<html" not in r.text, "a fragment, not a page"


async def test_the_fragment_is_admin_only_and_scoped_to_its_run(client, db_session):
    admin, run, log = await _seed(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    denied = await client.get(f"/admin/activity/{run.id}/llm-calls/{log.id}/bodies", headers=auth_headers(manager.id))
    assert denied.status_code == 403 and "RESPONSE-BODY" not in denied.text
    other = await factories.make_simulation_run(db_session)
    wrong_run = await client.get(f"/admin/activity/{other.id}/llm-calls/{log.id}/bodies", headers=auth_headers(admin.id))
    assert wrong_run.status_code == 404
    unknown = await client.get(f"/admin/activity/{run.id}/llm-calls/{uuid.uuid4()}/bodies", headers=auth_headers(admin.id))
    assert unknown.status_code == 404


async def test_page_is_bounded_and_latency_and_counts_are_labelled(client, db_session):
    admin, run, _log = await _seed(db_session)
    over = await client.get(f"/admin/activity/{run.id}/llm-calls?page=100001", headers=auth_headers(admin.id))
    assert over.status_code == 422
    html = (await client.get(f"/admin/activity/{run.id}/llm-calls", headers=auth_headers(admin.id))).text
    assert "Avg API-call latency" in html and "200.0ms" in html, "mean of call_stats latency_ms"
    assert "900ms last call" in html, "a row without wall_ms says its latency is the last call's"
    assert "Logged turns" in html and "Showing 1 of 1 logged turns" in html
