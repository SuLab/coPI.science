"""Integration test for reconciling stale `running` SimulationRun rows at agent
startup (issue #25 D2). A crash / OOM-kill / `docker kill` leaves a run's status
stuck at "running" forever: neither --fresh (inserts a new row) nor resume
(repairs only the single latest row by started_at) ever touches an older
stale row.
"""

import inspect

import pytest

from src.agent import main as _main_module
from src.agent.main import _reconcile_stale_runs
from tests import factories

pytestmark = pytest.mark.integration


def test_run_simulation_actually_calls_the_reconciler():
    """#25 I2: measured — restoring _reconcile_stale_runs' body but deleting its call
    site from _run_simulation leaves the entire suite (190 + 94 tests, everything)
    green, because nothing exercises _run_simulation's `if not no_db:` block end to
    end. A fake-session harness for that whole block is out of proportion; a source
    pin is the honest instrument so a future refactor of this block cannot silently
    drop the wiring without a single test noticing.
    """
    src = inspect.getsource(_main_module._run_simulation)
    assert "_reconcile_stale_runs(session_factory, simulation_run_id)" in src


class _FixtureSessionFactory:
    """Route _reconcile_stale_runs' self-opened session at the rolled-back test
    session. __aexit__ must NOT close the fixture-owned session."""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_reconcile_stops_every_other_running_run(db_session):
    stale = await factories.make_simulation_run(db_session, status="running")
    already_stopped = await factories.make_simulation_run(db_session, status="stopped")
    current = await factories.make_simulation_run(db_session, status="running")

    await _reconcile_stale_runs(_FixtureSessionFactory(db_session), current.id)

    await db_session.refresh(stale)
    await db_session.refresh(already_stopped)
    await db_session.refresh(current)
    assert stale.status == "stopped"
    assert stale.ended_at is not None
    assert already_stopped.status == "stopped"  # untouched, was already stopped
    assert current.status == "running"
    assert current.ended_at is None


async def test_reconcile_is_idempotent_on_a_second_call(db_session):
    """D2 idempotency: a second call must change nothing.

    The first call already proves a stale 'running' row is stopped. This test proves
    a *repeat* call does not re-stamp it: a mutant that drops the
    `SimulationRun.status == "running"` half of the WHERE clause (updating every row
    with `id != current_run_id` regardless of its current status) would still pass
    the single-call test above — the row ends up 'stopped' either way — but on a
    second call it would move `ended_at` forward again, because the row still
    matches `id != current_run_id`. Calling twice and diffing `ended_at` is the only
    way to see that.
    """
    stale = await factories.make_simulation_run(db_session, status="running")
    current = await factories.make_simulation_run(db_session, status="running")

    await _reconcile_stale_runs(_FixtureSessionFactory(db_session), current.id)
    await db_session.refresh(stale)
    await db_session.refresh(current)
    assert stale.status == "stopped"
    first_ended_at = stale.ended_at
    assert first_ended_at is not None

    await _reconcile_stale_runs(_FixtureSessionFactory(db_session), current.id)
    await db_session.refresh(stale)
    await db_session.refresh(current)

    assert stale.status == "stopped"
    assert stale.ended_at == first_ended_at, (
        "a second reconcile call moved ended_at forward — the status == 'running' "
        "guard in the WHERE clause was dropped, so an already-stopped row keeps "
        "getting re-stamped on every call"
    )
    assert current.status == "running"
    assert current.ended_at is None
