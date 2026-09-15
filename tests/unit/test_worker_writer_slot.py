"""The worker process must claim its own canonical-id writer slot at entry,
before anything can mint (R1) — see src/agent/ids.py and specs/local-db-conversations.md.
Pure unit test: no DB, no asyncio.run of the real worker loop (that needs Postgres and
runs forever)."""

from src.agent.ids import WRITER_WORKER
from src.worker import main as worker_main


def test_main_claims_the_worker_writer_slot(monkeypatch):
    claimed = []
    monkeypatch.setattr(worker_main, "set_default_writer_id", lambda wid: claimed.append(wid))
    # main() also installs SIGTERM/SIGINT handlers and calls asyncio.run(run_worker()) — neither
    # of those is this test's subject, and actually running them would hang or mutate global
    # process signal state. Neuter both, and close the coroutine main() built so pytest doesn't
    # warn "coroutine was never awaited".
    monkeypatch.setattr(worker_main.signal, "signal", lambda *a, **k: None)

    def _fake_run(coro):
        coro.close()

    monkeypatch.setattr(worker_main.asyncio, "run", _fake_run)

    worker_main.main()

    assert claimed == [WRITER_WORKER], (
        f"src.worker.main.main() did not claim WRITER_WORKER before starting the loop: {claimed}"
    )
