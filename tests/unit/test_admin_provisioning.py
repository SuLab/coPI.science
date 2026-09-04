"""Tests for the Slack config-token caching/rotation logic (SEC-10).

The refresh token is single-use, so the provisioning flow must reuse a cached
access token until it is about to expire and only rotate when necessary.
"""

import asyncio
import inspect
import time
import types
import uuid

import pytest

import src.services.admin_provisioning as ap


class _FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def kv(monkeypatch):
    store: dict[str, str] = {}

    async def fake_get(_db, key):
        return store.get(key)

    async def fake_set(_db, key, value):
        store[key] = value

    monkeypatch.setattr(ap, "_kv_get", fake_get)
    monkeypatch.setattr(ap, "_kv_set", fake_set)
    monkeypatch.setattr(
        ap,
        "get_settings",
        lambda: types.SimpleNamespace(
            slack_config_refresh_token="seed_refresh", slack_config_token=""
        ),
    )
    return store


def _fake_rotate(calls, ttl=3600):
    def rotate(refresh):
        calls.append(refresh)
        n = len(calls)
        return (f"access{n}", f"refresh{n}", int(time.time()) + ttl)
    return rotate


@pytest.mark.asyncio
async def test_first_use_rotates_then_reuses_cache(kv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token", _fake_rotate(calls)
    )
    db = _FakeDB()

    # No cache yet -> rotate using the seed refresh.
    assert await ap._config_token(db) == "access1"
    assert calls == ["seed_refresh"]

    # Cached token is still valid -> no rotation on subsequent calls.
    assert await ap._config_token(db) == "access1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_force_rotate_uses_rotated_refresh(kv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token", _fake_rotate(calls)
    )
    db = _FakeDB()

    await ap._config_token(db)  # -> access1 / refresh1 stored
    assert await ap._config_token(db, force_rotate=True) == "access2"
    # Second rotation used the refresh persisted by the first.
    assert calls == ["seed_refresh", "refresh1"]


@pytest.mark.asyncio
async def test_expired_cache_triggers_rotation(kv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token", _fake_rotate(calls)
    )
    db = _FakeDB()

    # Seed an already-expired cached token.
    kv[ap._KEY_TOKEN] = "stale"
    kv[ap._KEY_TOKEN_EXP] = str(int(time.time()) - 5)
    kv[ap._KEY_REFRESH] = "stored_refresh"

    assert await ap._config_token(db) == "access1"
    assert calls == ["stored_refresh"]


# --- issue #24 I1 (SEC-10): shield the rotate-and-persist unit from cancellation -----


@pytest.mark.asyncio
async def test_rotation_survives_cancellation_of_the_awaiting_task(kv, monkeypatch):
    """If the request task is cancelled while the worker thread is mid-httpx.post
    (e.g. uvicorn's graceful shutdown during a deploy), Slack has already consumed
    the single-use refresh token. asyncio.shield must let the persist finish
    anyway, or both the old and new refresh token become unusable."""
    started = asyncio.Event()

    async def _slow_rotate_async(refresh):
        started.set()
        await asyncio.sleep(0.05)  # stand-in for the in-flight httpx.post
        return ("access-new", "refresh-new", int(time.time()) + 3600)

    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token_async", _slow_rotate_async
    )
    db = _FakeDB()

    task = asyncio.ensure_future(ap._config_token(db))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Give the shielded background coroutine a moment to finish persisting.
    await asyncio.sleep(0.1)

    assert kv[ap._KEY_TOKEN] == "access-new"
    assert kv[ap._KEY_REFRESH] == "refresh-new"
    assert int(kv[ap._KEY_TOKEN_EXP]) > time.time()


# --- issue #24 C2-7: release the connection before the blocking Slack call -----------


class _EventResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


async def test_start_provisioning_releases_the_connection_before_create_app(monkeypatch):
    """The config-token SELECTs (and any rotate) must not hold the pooled connection
    across the create_app round trip. If start_provisioning commits before the
    (now-async) Slack call, 'db.commit' precedes 'create_app_async' below; in the
    pre-fix shape the commit only happens afterward, well past the network call."""
    events: list[str] = []

    class _EventDB:
        async def execute(self, _stmt):
            events.append("db.execute")
            return _EventResult(None)

        def add(self, _obj):
            events.append("db.add")

        async def commit(self):
            events.append("db.commit")

    async def _fake_config_token(_db, force_rotate=False):
        events.append("config_token")
        return "cfg-token"

    async def _fake_create_app_async(**_kw):
        events.append("create_app_async")
        return {"client_id": "c", "client_secret": "s", "app_id": "A1",
                "oauth_url": "https://slack.example/install"}

    async def _fake_get_any_bot_token(_db):
        events.append("get_any_bot_token")
        return "xoxb-t"

    async def _fake_lookup_team_id_async(_token):
        events.append("lookup_team_id_async")
        return "T123"

    monkeypatch.setattr(ap, "_config_token", _fake_config_token)
    monkeypatch.setattr(ap, "create_app_async", _fake_create_app_async)
    monkeypatch.setattr(ap, "lookup_team_id_async", _fake_lookup_team_id_async)
    monkeypatch.setattr(
        "src.services.slack_tokens.get_any_bot_token", _fake_get_any_bot_token
    )

    agent = types.SimpleNamespace(
        id=uuid.uuid4(), agent_id="su", bot_name="SuBot", pi_name="PI Su",
    )
    await ap.start_provisioning(_EventDB(), agent)

    # (#24 Minor 5) Check adjacency in the *raw* event list, not a filtered one: the
    # filtered form only proves a commit happened somewhere earlier, not that no
    # db.execute/db.add ran between the commit and the blocking call.
    create_idx = events.index("create_app_async")
    assert events[create_idx - 1] == "db.commit", (
        f"event order was {events} -- the config-token SELECTs must be committed "
        "(releasing the pooled connection) before the blocking create_app call"
    )
    lookup_idx = events.index("lookup_team_id_async")
    assert events[lookup_idx - 1] == "db.commit", (
        f"event order was {events} -- get_any_bot_token's SELECT must be committed "
        "(releasing the pooled connection) before the blocking lookup_team_id_async call"
    )


async def test_complete_provisioning_releases_the_connection_before_exchange_code(
    monkeypatch,
):
    """Same property as above, on the callback side: the state/agent SELECTs must
    be committed before the blocking exchange_code round trip."""
    events: list[str] = []

    prov = types.SimpleNamespace(
        id=uuid.uuid4(), agent_registry_id=uuid.uuid4(),
        client_id="cid", client_secret="csec",
    )
    agent = types.SimpleNamespace(
        id=prov.agent_registry_id, agent_id="su", slack_bot_token=None,
    )

    class _EventDB:
        def __init__(self):
            self._results = [prov, agent]

        async def execute(self, _stmt):
            events.append("db.execute")
            value = self._results.pop(0) if self._results else None
            return _EventResult(value)

        async def commit(self):
            events.append("db.commit")

        async def delete(self, _obj):
            events.append("db.delete")

    async def _fake_exchange_code_async(*_a, **_kw):
        events.append("exchange_code_async")
        return "xoxb-good"

    monkeypatch.setattr(ap, "exchange_code_async", _fake_exchange_code_async)

    result = await ap.complete_provisioning(_EventDB(), state="s", code="c")

    assert result.slack_bot_token == "xoxb-good"
    # (#24 Minor 5) Adjacency in the raw event list, not merely "a commit happened
    # somewhere before" -- see the comment in the create_app test above.
    exchange_idx = events.index("exchange_code_async")
    assert events[exchange_idx - 1] == "db.commit", (
        f"event order was {events} -- the state/agent SELECTs must be committed "
        "before the blocking exchange_code call"
    )


# --- issue #24 Minors 3-4: pin the async config-token twin + connection release ------


def test_config_token_calls_the_async_rotate_twin():
    """(#24 Minor 3) The four rotation tests in test_slack_provisioning.py
    (test_rotation_persists_the_whole_triple and its three siblings) patch the
    SYNC `rotate_config_token`, which BOTH the sync and async variants reach
    (`rotate_config_token_async` runs it via `asyncio.to_thread`) -- so none of
    them can tell whether the rotate call actually reaches the async twin
    (C2-6) or regressed to the old blocking call. Pin the call site directly.
    The actual call lives in `_rotate_and_persist` (extracted from
    `_config_token` in #24 I1 so the caller can `asyncio.shield` it) -- inspect
    that, not just `_config_token`, whose body only names it in a comment."""
    source = inspect.getsource(ap._rotate_and_persist)
    assert "await rotate_config_token_async(" in source


@pytest.mark.asyncio
async def test_config_token_releases_the_connection_before_the_real_rotate(monkeypatch):
    """(#24 Minor 4) Both ordering tests above replace `_config_token` wholesale
    with a fake, so neither exercises the release-before-rotate property inside
    the real function. Drive the real `_config_token` with a recording session
    and a stubbed async rotate, and assert the last event before the rotate call
    is `db.commit` (Minor 5 form: adjacency in the raw event list)."""
    events: list[str] = []

    class _EventDB:
        async def execute(self, _stmt):
            events.append("db.execute")
            return _EventResult(None)

        def add(self, _obj):
            events.append("db.add")

        async def commit(self):
            events.append("db.commit")

    async def _fake_rotate_async(refresh):
        events.append("rotate_config_token_async")
        return ("access1", "refresh1", int(time.time()) + 3600)

    monkeypatch.setattr(
        ap, "get_settings",
        lambda: types.SimpleNamespace(
            slack_config_refresh_token="seed_refresh", slack_config_token=""
        ),
    )
    monkeypatch.setattr(
        "src.services.slack_provisioning.rotate_config_token_async", _fake_rotate_async
    )

    token = await ap._config_token(_EventDB())

    assert token == "access1"
    rotate_idx = events.index("rotate_config_token_async")
    assert events[rotate_idx - 1] == "db.commit", (
        f"event order was {events} -- the config-token SELECTs must be committed "
        "(releasing the pooled connection) before the blocking rotate call"
    )
