"""C-15: discussions are filtered, counted and paged in SQL; the HTML page refuses
`run_id=all` above DISCUSSIONS_ALL_RUNS_MAX while the export lists everything."""

import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN
from src.services import directory
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_KW = dict(channel_filter=None, status_filter=None, agent_filter=[])


async def _seed(db_session, n):
    run = await factories.make_simulation_run(db_session)
    for i in range(n):
        root = await factories.make_agent_message(
            db_session, run=run, phase="new_post", thread_ts=None,
            message_ts=f"500.{i}", channel_name="paging", agent_id="lab1",
        )
        await factories.make_thread_decision(
            db_session, run=run, thread_id=root.message_ts, outcome="timeout",
            agent_a="lab1", agent_b="hub", channel="paging",
        )
    return run


async def test_only_the_pages_decisions_are_loaded(db_session, engine, monkeypatch):
    monkeypatch.setattr(directory, "DISCUSSIONS_PAGE_SIZE", 2)
    run = await _seed(db_session, 5)
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        if "thread_decisions.summary_text" in statement:
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        view = await directory.build_discussions_view(db_session, run_id=str(run.id), page=2, **_KW)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert [t["message_ts"] for t in view["threads"]] == ["500.2", "500.3"]
    assert view["thread_total"] == 5 and view["page_count"] == 3
    assert seen and all(" IN (" in s for s in seen), "decision rows are loaded for the page only"


async def test_html_refuses_all_runs_above_the_cap_but_export_does_not(client, db_session, monkeypatch):
    monkeypatch.setattr(directory, "DISCUSSIONS_ALL_RUNS_MAX", 2)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await _seed(db_session, 3)

    page = await directory.build_discussions_view(db_session, run_id="all", page=1, **_KW)
    assert page["all_runs_refused"] is True and page["threads"] == []
    export = await directory.build_discussions_view(db_session, run_id="all", page=None, **_KW)
    assert export["all_runs_refused"] is False and len(export["threads"]) >= 3
    one_run = await directory.build_discussions_view(db_session, run_id=str(run.id), page=1, **_KW)
    assert one_run["all_runs_refused"] is False and len(one_run["threads"]) == 3

    html = (await client.get("/workspace/discussions?run_id=all", headers=auth_headers(admin.id))).text
    assert "too many to list on one page" in html
