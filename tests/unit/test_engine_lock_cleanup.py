"""main._run_simulation releases the engine lock on every exit path: a raising
acquire, and a raising heartbeat stop or engine dispose (spec §8.3)."""
import pytest

from src.agent import main as agent_main


class _Recorder:
    def __init__(self):
        self.calls: list[str] = []


def _patch(monkeypatch, rec, *, acquire_raises=False, hb_stop_raises=False, dispose_raises=False):
    class _Lock:
        def __init__(self, url, key):
            pass

        async def acquire(self):
            rec.calls.append("acquire")
            if acquire_raises:
                raise OSError("connection reset during pg_try_advisory_lock")
            return True

        async def release(self):
            rec.calls.append("release")

    class _Heartbeat:
        def __init__(self, **kw):
            self.tick_detail = {}

        def start(self):
            rec.calls.append("hb.start")

        async def stop(self):
            rec.calls.append("hb.stop")
            if hb_stop_raises:
                raise RuntimeError("heartbeat stop failed")

    class _Engine:
        async def dispose(self):
            rec.calls.append("dispose")
            if dispose_raises:
                raise RuntimeError("dispose failed")

    async def _locked(*a, **kw):
        rec.calls.append("run")

    monkeypatch.setattr(agent_main, "validate_engine_settings", lambda settings: None)
    monkeypatch.setattr(agent_main, "SessionAdvisoryLock", _Lock)
    monkeypatch.setattr(agent_main, "EngineHeartbeat", _Heartbeat)
    monkeypatch.setattr(agent_main, "create_async_engine", lambda *a, **kw: _Engine())
    monkeypatch.setattr(agent_main, "async_sessionmaker", lambda *a, **kw: None)
    monkeypatch.setattr(agent_main, "_run_simulation_locked", _locked)


@pytest.mark.asyncio
async def test_a_raising_acquire_releases_the_lock_connection(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, acquire_raises=True)
    with pytest.raises(OSError):
        await agent_main._run_simulation(0, 0, False, False, False)
    assert rec.calls == ["acquire", "release"]


@pytest.mark.asyncio
async def test_a_raising_dispose_still_releases_the_lock(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, dispose_raises=True)
    with pytest.raises(RuntimeError, match="dispose failed"):
        await agent_main._run_simulation(0, 0, False, False, False)
    assert rec.calls[-3:] == ["hb.stop", "dispose", "release"]


@pytest.mark.asyncio
async def test_a_raising_heartbeat_stop_still_disposes_and_releases(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, hb_stop_raises=True)
    with pytest.raises(RuntimeError, match="heartbeat stop failed"):
        await agent_main._run_simulation(0, 0, False, False, False)
    assert rec.calls[-3:] == ["hb.stop", "dispose", "release"]
