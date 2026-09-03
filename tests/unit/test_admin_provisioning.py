"""Tests for the Slack config-token caching/rotation logic (SEC-10).

The refresh token is single-use, so the provisioning flow must reuse a cached
access token until it is about to expire and only rotate when necessary.
"""

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

    ordered = [
        e for e in events
        if e in ("db.commit", "create_app_async", "lookup_team_id_async")
    ]
    assert ordered[0] == "db.commit", (
        f"event order was {events} -- the config-token SELECTs must be committed "
        "(releasing the pooled connection) before the blocking create_app call"
    )
    lookup_idx = ordered.index("lookup_team_id_async")
    assert ordered[lookup_idx - 1] == "db.commit", (
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
    ordered = [e for e in events if e in ("db.commit", "exchange_code_async")]
    assert ordered[0] == "db.commit", (
        f"event order was {events} -- the state/agent SELECTs must be committed "
        "before the blocking exchange_code call"
    )
