"""Unit-tier coverage for the DB probe on /api/health (#27 I2). No real
Postgres — get_session_factory is monkeypatched on src.main, mirroring the
badge_factory override tests/conftest.py's `client` fixture already does
(tests/conftest.py:120-121: monkeypatch.setattr("src.main.get_session_factory", ...))."""

import asyncio

import httpx
from httpx import ASGITransport

from src.main import create_app


class _FakeSession:
    """Async-context-manager stand-in for AsyncSession — enough surface for
    the health route's `async with session_factory() as db: await db.execute(...)`."""

    def __init__(self, *, fail: bool):
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        if self._fail:
            raise ConnectionRefusedError("db unreachable")


async def _get_health(monkeypatch, *, fail: bool):
    monkeypatch.setattr("src.main.get_session_factory", lambda: (lambda: _FakeSession(fail=fail)))
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/api/health")


async def test_health_ok_when_db_probe_succeeds(monkeypatch):
    r = await _get_health(monkeypatch, fail=False)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_health_503_when_db_probe_fails(monkeypatch):
    r = await _get_health(monkeypatch, fail=True)
    assert r.status_code == 503


class _StalledSession:
    """Async-context-manager stand-in whose `execute` never returns — mimics a
    stalled Postgres, where the TCP connection is open but no query response
    ever arrives (#27 I2 review: an unbounded probe would pile orphaned
    coroutines/connections against the pool)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        await asyncio.Event().wait()  # never set: hangs forever unless bounded


async def test_health_503_when_db_probe_times_out(monkeypatch):
    monkeypatch.setattr("src.main.get_session_factory", lambda: (lambda: _StalledSession()))
    monkeypatch.setattr("src.main.HEALTH_PROBE_TIMEOUT_SECONDS", 0.05)
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/health")
    assert r.status_code == 503
