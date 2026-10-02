"""RA-06: the simulation page fetches `call_stats` once, caches ended runs, refreshes politely."""
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event

from src.models import USER_ROLE_ADMIN
from src.services import simulation_view
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _count_call_stats_selects(engine):
    """Count the `phase, call_stats` row fetch (not the jsonb_array_elements aggregates)."""
    seen = []

    def before(conn, cursor, statement, params, context, executemany):
        if "llm_call_logs.phase, llm_call_logs.call_stats" in statement:
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    return seen, lambda: event.remove(engine.sync_engine, "before_cursor_execute", before)


async def test_one_call_stats_fetch_per_render(client, db_session, engine):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session, status="stopped", ended_at=datetime.now(UTC))
    await factories.make_llm_call_log(db_session, run=run, call_stats=[{"stop_reason": "end_turn", "latency_ms": 5}])
    simulation_view._ENDED_RUN_STATS.clear()
    seen, stop = _count_call_stats_selects(engine)
    try:
        r = await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
        assert r.status_code == 200
        first = len(seen)
        await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))
    finally:
        stop()
        simulation_view._ENDED_RUN_STATS.clear()
    assert first == 1
    assert len(seen) == 1, "the ended run's stats come from the cache on the second render"


def test_refresh_script_is_polite():
    html = (Path(__file__).resolve().parents[2] / "templates/admin/simulation.html").read_text()
    assert "document.hidden" in html and "inFlight" in html


async def test_a_running_runs_stats_are_cached_for_the_ttl(client, db_session, engine, monkeypatch):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session, status="running")
    await factories.make_llm_call_log(db_session, run=run, call_stats=[{"stop_reason": "end_turn", "latency_ms": 5}])
    clock = [1000.0]
    monkeypatch.setattr(simulation_view, "_clock", lambda: clock[0])
    simulation_view._LIVE_RUN_STATS.clear()
    seen, stop = _count_call_stats_selects(engine)
    url = f"/admin/simulation?run={run.id}"
    try:
        assert (await client.get(url, headers=auth_headers(admin.id))).status_code == 200
        clock[0] += simulation_view.LIVE_RUN_STATS_TTL_SECONDS - 1
        await client.get(url, headers=auth_headers(admin.id))
        within_ttl = len(seen)
        clock[0] += 2
        await client.get(url, headers=auth_headers(admin.id))
    finally:
        stop()
        simulation_view._LIVE_RUN_STATS.clear()
    assert within_ttl == 1, "the second render inside the TTL is served from the cache"
    assert len(seen) == 2, "past the TTL the stats are recomputed"


async def test_a_status_change_is_a_new_cache_key(client, db_session, engine, monkeypatch):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session, status="running")
    await factories.make_llm_call_log(db_session, run=run, call_stats=[{"stop_reason": "end_turn", "latency_ms": 5}])
    monkeypatch.setattr(simulation_view, "_clock", lambda: 1000.0)
    simulation_view._LIVE_RUN_STATS.clear()
    simulation_view._ENDED_RUN_STATS.clear()
    seen, stop = _count_call_stats_selects(engine)
    url = f"/admin/simulation?run={run.id}"
    try:
        await client.get(url, headers=auth_headers(admin.id))
        run.status = "stopped"
        run.ended_at = datetime.now(UTC)
        await db_session.flush()
        await client.get(url, headers=auth_headers(admin.id))
    finally:
        stop()
        simulation_view._LIVE_RUN_STATS.clear()
        simulation_view._ENDED_RUN_STATS.clear()
    assert len(seen) == 2, "the stopped run is not served the running run's cached figures"


async def test_the_burn_chart_hub_is_deterministic(db_session):
    from sqlalchemy import select

    from src.agent.role_capabilities import hub_role_names
    from src.models import AgentRegistry

    hub_role = hub_role_names()[0]
    await factories.make_agent(db_session, agent_id="zz-live-hub", role=hub_role, status="active")
    await factories.make_agent(db_session, agent_id="aa-parked-hub", role=hub_role, status="inactive")
    rows = (
        await db_session.execute(
            select(AgentRegistry.agent_id, AgentRegistry.status).where(
                AgentRegistry.role.in_(hub_role_names())
            )
        )
    ).all()
    expected = sorted(rows, key=lambda r: (r.status != "active", r.agent_id))[0].agent_id
    assert await simulation_view._hub_agent_id(db_session) == expected
    assert expected != "aa-parked-hub", "an active hub outranks an alphabetically earlier parked one"
