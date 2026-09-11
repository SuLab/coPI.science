"""The worker's shutdown path shuts the Slack I/O executor down — see the
identical reasoning in tests/unit/test_main_lifespan.py for the web app's side.

`run_worker()` itself needs Postgres and runs forever, so this drives
`_shutdown_worker`, the factored-out tail of the loop, directly — mirroring
tests/unit/test_worker_writer_slot.py's "no DB, no real asyncio.run" approach.
"""

import pytest

import src.worker.main as worker_main
from src.worker.main import _shutdown_worker


class _FakeEngine:
    def __init__(self):
        self.disposed = False

    async def dispose(self):
        self.disposed = True


@pytest.mark.asyncio
async def test_shutdown_worker_shuts_down_the_slack_executor_before_disposing_the_engine(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        worker_main, "shutdown_slack_executor", lambda: calls.append("slack_executor")
    )
    engine = _FakeEngine()

    async def _dispose():
        calls.append("engine")
        engine.disposed = True

    engine.dispose = _dispose

    await _shutdown_worker(engine)

    assert calls == ["slack_executor", "engine"], (
        "the Slack executor must be shut down before the DB engine is disposed"
    )
    assert engine.disposed
