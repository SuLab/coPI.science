from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.services.simulation_control import derive_panel_state, is_finalize_stop

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _row(state, age_s):
    return SimpleNamespace(state=state, updated_at=NOW - timedelta(seconds=age_s))


@pytest.mark.parametrize("alive,row,expected", [
    (False, None, "not_deployed"),
    (False, _row("starting", 5), "starting"),
    (False, _row("starting", 500), "idle"),
    (False, _row("idle", 5), "idle"),
    (False, _row("stopping", 5), "idle"),
    (True, _row("running", 5), "running"),
    (True, _row("stopping", 119), "stopping"),
    (True, _row("starting", 5), "starting"),
    (True, _row("idle", 5), "starting"),
    (True, _row("running", 121), "unresponsive"),
    (True, None, "unresponsive"),
])
def test_the_panel_state_table(alive, row, expected):
    assert derive_panel_state(row, NOW, engine_alive=alive) == expected


def test_not_alive_with_fresh_running_row_is_idle():
    """Review Focus 5: a lock-less (old) or dead engine's fresh row is not a run."""
    assert derive_panel_state(_row("running", 3), NOW, engine_alive=False) == "idle"


def test_without_liveness_the_legacy_answer_is_unchanged():
    assert derive_panel_state(_row("running", 500), NOW) == "stale"
    assert derive_panel_state(_row("running", 5), NOW) == "running"
    assert derive_panel_state(None, NOW) == "not_deployed"


def test_is_finalize_stop():
    assert is_finalize_stop(SimpleNamespace(command="stop", payload={"finalize": True, "run_id": "x"}))
    assert not is_finalize_stop(SimpleNamespace(command="stop", payload={"finalize": True}))
    assert not is_finalize_stop(SimpleNamespace(command="stop", payload={"hold_open": True}))
    assert not is_finalize_stop(SimpleNamespace(command="start", payload={"finalize": True, "run_id": "x"}))
