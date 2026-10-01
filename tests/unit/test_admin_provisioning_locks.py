"""Slack provisioning concurrency (RA-15, PS-17): a per-agent advisory lock, the
superseded app deleted, and the single-use refresh token read under a row lock.

Nothing here reaches Slack: `create_app`, `delete_app`, `lookup_team_id` and
`rotate_config_token` are all patched, and the refresh token is single-use, so a
real `tooling.tokens.rotate` must never be called from a test."""
import asyncio
import threading
import time
import types

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AppSetting, SlackAppProvision
from src.services import admin_provisioning as ap
from tests import factories

pytestmark = pytest.mark.integration

_NEW_APP = {"client_id": "c", "client_secret": "s", "app_id": "A_NEW", "oauth_url": "https://x/?a=1"}


async def _token(db, *, force_rotate=False):
    return "cfg"


async def test_second_provisioning_for_one_agent_is_refused(db_session, engine, monkeypatch):
    agent = await factories.make_agent(db_session, status="pending")
    await db_session.commit()
    gate = threading.Event()

    def slow_create(**kw):
        while not gate.is_set():
            time.sleep(0.05)
        return dict(_NEW_APP)

    monkeypatch.setattr(ap, "create_app", slow_create)
    monkeypatch.setattr(ap, "_config_token", _token)
    monkeypatch.setattr(ap, "lookup_team_id", lambda token: None)
    monkeypatch.setattr(ap, "_lock_session_factory", lambda: async_sessionmaker(engine))
    first = asyncio.create_task(ap.start_provisioning(db_session, agent, initiated_by=None))
    await asyncio.sleep(0.5)
    try:
        async with async_sessionmaker(engine)() as other:
            with pytest.raises(ap.ProvisioningError, match="already in progress"):
                await ap.start_provisioning(other, agent, initiated_by=None)
    finally:
        gate.set()
        await first


async def test_superseded_app_is_deleted(db_session, engine, monkeypatch):
    agent = await factories.make_agent(db_session, status="pending")
    db_session.add(SlackAppProvision(agent_registry_id=agent.id, state="old", client_id="c0",
                                     client_secret="s0", app_id="A_OLD"))
    await db_session.flush()
    deleted = []
    monkeypatch.setattr(ap, "delete_app", lambda token, app_id: deleted.append(app_id))
    monkeypatch.setattr(ap, "create_app", lambda **kw: dict(_NEW_APP))
    monkeypatch.setattr(ap, "_config_token", _token)
    monkeypatch.setattr(ap, "lookup_team_id", lambda token: None)
    monkeypatch.setattr(ap, "_lock_session_factory", lambda: async_sessionmaker(engine))
    await ap.start_provisioning(db_session, agent, initiated_by=None)
    assert deleted == ["A_OLD"]
    rows = (await db_session.execute(select(SlackAppProvision).where(
        SlackAppProvision.agent_registry_id == agent.id))).scalars().all()
    assert [r.app_id for r in rows] == ["A_NEW"]


async def test_refresh_token_is_read_from_app_settings_under_lock(engine, monkeypatch):
    """Committed rows, two real sessions. Without the FOR UPDATE the probe inside the
    patched rotate would acquire the row instead of failing with lock_not_available."""
    keys = (ap._KEY_TOKEN, ap._KEY_REFRESH, ap._KEY_TOKEN_EXP)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    seen, probe_errors = [], []

    async def probe():
        async with factory() as other:
            try:
                await other.execute(text(
                    "SELECT 1 FROM app_settings WHERE key = :k FOR UPDATE NOWAIT"), {"k": ap._KEY_REFRESH})
            except DBAPIError as exc:
                probe_errors.append(exc)

    def rotate(refresh):
        seen.append(refresh)
        asyncio.run_coroutine_threadsafe(probe(), loop).result(timeout=15)
        return "xoxe.xoxp-new", "xoxe-1-next", 9999999999

    monkeypatch.setattr("src.services.slack_provisioning.rotate_config_token", rotate)
    monkeypatch.setattr(ap, "get_settings", lambda: types.SimpleNamespace(
        slack_config_refresh_token="xoxe-1-env", slack_config_token=""))
    try:
        async with factory() as s:
            s.add(AppSetting(key=ap._KEY_REFRESH, value="xoxe-1-db"))
            await s.commit()
        async with factory() as s:
            assert await ap._config_token(s, force_rotate=True) == "xoxe.xoxp-new"
        assert seen == ["xoxe-1-db"]
        assert len(probe_errors) == 1, "the refresh row was not locked during the rotation"
        async with factory() as s:
            assert await ap._kv_get(s, ap._KEY_REFRESH) == "xoxe-1-next"
    finally:
        async with factory() as s:
            await s.execute(delete(AppSetting).where(AppSetting.key.in_(keys)))
            await s.commit()
