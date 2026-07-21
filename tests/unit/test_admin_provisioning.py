"""Tests for the Slack config-token caching/rotation logic (SEC-10).

The refresh token is single-use, so the provisioning flow must reuse a cached
access token until it is about to expire and only rotate when necessary.
"""

import time
import types

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
