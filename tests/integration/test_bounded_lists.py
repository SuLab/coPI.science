"""Bounded lists: jobs, run detail and the PI directory (Task 118)."""

import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN, Job
from src.services import directory
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_jobs_filter_and_paginate_in_sql(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    u = await factories.make_user(db_session)
    for i in range(130):
        db_session.add(Job(type="review_feedback_analysis", user_id=u.id, payload={"i": i}, status="completed"))
    db_session.add(Job(type="enrich_grants", user_id=u.id, payload={}, status="dead"))
    await db_session.flush()
    r = await client.get("/admin/jobs?status_filter=completed&page=2", headers=auth_headers(admin.id))
    assert r.status_code == 200
    # Row cells only: the type filter's <option> carries the same label once.
    assert r.text.count(">Review Feedback Analysis</td>") == 130 - directory.JOBS_PAGE_SIZE
    # A row of another type, not the type filter's option list (C-18 lists every type).
    assert ">Enrich Grants</td>" not in r.text
    assert ">130</div>" in r.text  # the status card and the Total card count every completed row


async def test_run_detail_aggregates_in_sql_and_pages(db_session):
    run = await factories.make_simulation_run(db_session)
    for i in range(250):
        await factories.make_agent_message(
            db_session, run=run, agent_id="a" if i % 2 else None,
            channel_name="c1" if i % 3 else "c2", message_length=10,
        )
    detail = await directory.build_run_detail(db_session, run.id, page=2)
    assert detail["message_total"] == 250 and len(detail["messages"]) == 50
    assert detail["page"] == 2 and detail["page_count"] == 2
    assert detail["agent_stats"]["a"] == {"count": 125, "total_length": 1250, "avg_length": 10}
    assert detail["agent_stats"][None]["count"] == 125
    assert detail["channel_stats"]["c1"]["agents"] == {"a"}
    assert not hasattr(detail["messages"][0], "content")


async def test_pi_directory_has_no_jobs_selectinload(db_session, engine):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, research_summary="")
    db_session.add(Job(type="generate_profile", user_id=user.id, payload={}, status="pending"))
    await db_session.flush()
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        if "FROM jobs" in statement and "EXISTS" not in statement.upper():
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        rows = await directory.list_pi_directory(db_session)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert seen == []
    assert next(r for r in rows if r["user"].id == user.id)["profile_status"] == "generating"


async def test_run_detail_stats_keep_first_appearance_order(db_session):
    """The per-agent and per-channel tables list keys in the order they first
    posted, as the per-message loop the SQL aggregates replaced did."""
    from datetime import UTC, datetime, timedelta

    run = await factories.make_simulation_run(db_session)
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    order = [("zeta", "c-late"), ("alpha", "c-early"), ("zeta", "c-early"), ("mid", "c-mid"), ("alpha", "c-late")]
    for i, (agent, channel) in enumerate(order):
        await factories.make_agent_message(
            db_session, run=run, agent_id=agent, channel_name=channel,
            message_length=5, created_at=t0 + timedelta(minutes=i),
        )
    detail = await directory.build_run_detail(db_session, run.id)
    assert list(detail["agent_stats"]) == ["zeta", "alpha", "mid"]
    assert list(detail["channel_stats"]) == ["c-late", "c-early", "c-mid"]
