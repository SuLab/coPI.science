"""Unit-tier coverage for the DB probe on /api/health (#27 I2). No real
Postgres — the probe's engine is monkeypatched on src.main, mirroring the
badge_factory override tests/conftest.py's `client` fixture already does
(tests/conftest.py:120-121: monkeypatch.setattr("src.main.get_session_factory", ...)).

The probe uses its own one-connection engine with asyncpg connect/command timeouts
rather than the request pool, because `asyncio.wait_for` cannot interrupt a socket read a
SQLAlchemy greenlet is already parked on: measured against a frozen Postgres
(`docker pause`), the wait_for-only probe returned after 142s, and with both driver
timeouts armed it returns 503 in 3.0s. See the note in src/main.py."""

import asyncio
import inspect

import httpx
from httpx import ASGITransport
from sqlalchemy.exc import DBAPIError

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


class _FakeEngine:
    """Stands in for the probe's engine: `async with engine.connect() as conn`."""

    def __init__(self, session):
        self._session = session

    def connect(self):
        return self._session


async def _get_health(monkeypatch, *, fail: bool):
    monkeypatch.setattr(
        "src.main.get_health_engine", lambda: _FakeEngine(_FakeSession(fail=fail))
    )
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
    monkeypatch.setattr("src.main.get_health_engine", lambda: _FakeEngine(_StalledSession()))
    monkeypatch.setattr("src.main.HEALTH_PROBE_TIMEOUT_SECONDS", 0.05)
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/health")
    assert r.status_code == 503


def test_the_probe_engine_bounds_both_connect_and_command():
    """Both asyncpg timeouts must be armed and must sit under the outer wait_for.

    `command_timeout` alone does not help a probe that opens a fresh connection:
    against a frozen server the TCP handshake completes into the accept backlog and the
    startup exchange hangs, which is the CONNECT timeout's job.
    Measured with only command_timeout set: three consecutive probes each ran past 30s.
    """
    from src import main as m

    assert m.HEALTH_PROBE_CONNECT_TIMEOUT_SECONDS < m.HEALTH_PROBE_TIMEOUT_SECONDS
    assert m.HEALTH_PROBE_COMMAND_TIMEOUT_SECONDS < m.HEALTH_PROBE_TIMEOUT_SECONDS
    src = inspect.getsource(m.get_health_engine)
    assert "pool_size=1" in src and "max_overflow=0" in src, (
        "the probe must have its own tiny pool: off the request pool (its purpose), and "
        "capped at one connection so a public endpoint cannot amplify into Postgres"
    )
    assert "command_timeout" in src and '"timeout"' in src


class _ReapedThenHangingSession:
    """First `execute` burns most of the outer budget then raises the reaped-connection
    error (`connection_invalidated=True`); the retry that follows hangs forever. Only a
    retry that is itself wrapped in a bounded `wait_for` (using what's left of the
    outer deadline, not a fresh full budget) can make the route answer in time (#27 I2:
    `engine.connect()` and the second attempt sit outside the bound today, so this
    retry would hang for the full HEALTH_PROBE_TIMEOUT_SECONDS on top of the time
    already spent)."""

    def __init__(self, *, first_probe_seconds: float):
        self._first_probe_seconds = first_probe_seconds
        self._calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, *args, **kwargs):
        self._calls += 1
        if self._calls == 1:
            await asyncio.sleep(self._first_probe_seconds)
            raise DBAPIError("SELECT 1", {}, Exception("conn reset"), connection_invalidated=True)
        await asyncio.Event().wait()  # never set: only a bounded retry escapes this


async def test_health_retry_after_reap_stays_within_the_documented_bound(monkeypatch):
    # Deterministic (audit 2026-09-08 RC-10, REV5): instead of timing the request on
    # the wall clock -- which flaked under a loaded CI host -- record the `timeout=`
    # the route hands to asyncio.wait_for. The first probe burns most of the budget
    # and raises a reaped-connection error; the retry's wait_for must be bounded by
    # what remains of the outer deadline (~0.1s here), never re-armed with a fresh
    # full HEALTH_PROBE_TIMEOUT_SECONDS (the pre-fix behaviour, 0.5s).
    import asyncio as _asyncio

    timeout = 0.5
    first_probe = timeout - 0.1
    monkeypatch.setattr("src.main.HEALTH_PROBE_TIMEOUT_SECONDS", timeout)
    session = _ReapedThenHangingSession(first_probe_seconds=first_probe)
    monkeypatch.setattr("src.main.get_health_engine", lambda: _FakeEngine(session))
    recorded: list[float] = []
    real_wait_for = _asyncio.wait_for

    async def recording_wait_for(aw, timeout=None, **kw):
        recorded.append(timeout)
        return await real_wait_for(aw, timeout=timeout, **kw)

    monkeypatch.setattr("src.main.asyncio.wait_for", recording_wait_for)
    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/health")
    assert r.status_code == 503
    assert len(recorded) == 2, f"expected one probe and one bounded retry, saw {recorded}"
    assert recorded[0] == timeout
    assert recorded[1] <= (timeout - first_probe) + 0.05, (
        f"retry wait_for timeout was {recorded[1]:.3f}s against a remaining budget of "
        f"~{timeout - first_probe:.2f}s -- the retry is re-armed with a fresh full budget "
        "instead of what is left of the outer deadline"
    )

