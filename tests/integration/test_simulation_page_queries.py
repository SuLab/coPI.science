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
