"""B-13: a run_id that parses but names no run falls back to the newest run, so
the selector and the list agree instead of showing an empty list under the
wrong option."""

import uuid

from src.models import SimulationRun
from src.services.directory import _resolve_run_selection


def _runs(n):
    return [SimulationRun(id=uuid.uuid4()) for _ in range(n)]


def test_an_unknown_run_id_falls_back_to_the_newest_run():
    runs = _runs(2)
    assert _resolve_run_selection(runs, str(uuid.uuid4())) == (False, runs[0].id)


def test_a_known_older_run_id_is_kept():
    runs = _runs(2)
    assert _resolve_run_selection(runs, str(runs[1].id)) == (False, runs[1].id)


def test_all_is_kept():
    assert _resolve_run_selection(_runs(1), "all") == (True, "all")


def test_garbage_falls_back_to_the_newest_run():
    runs = _runs(1)
    assert _resolve_run_selection(runs, "nope") == (False, runs[0].id)


def test_no_runs_selects_nothing():
    assert _resolve_run_selection([], str(uuid.uuid4())) == (False, None)


def test_the_discussions_pages_use_the_same_fallback():
    """B-13 on /workspace/discussions and /workspace/discussions (``_select_run``)."""
    from src.services.directory import _select_run

    runs = _runs(2)
    assert _select_run(runs, str(uuid.uuid4())) == runs[0].id
    assert _select_run(runs, str(runs[1].id)) == runs[1].id
    assert _select_run(runs, "all") == "all"
