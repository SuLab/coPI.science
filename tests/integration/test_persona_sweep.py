"""The gated daily persona sweep (spec 2026-10-05 §6.1, D54)."""
import contextlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AppSetting, PiGrantIdentity, ProfileRevision
from src.services import profile_export
from src.services.persona_sweep import (
    PERSONA_SWEEP_SETTING,
    enable_persona_sweep,
    run_persona_sweep,
    sweep_enabled,
)
from src.services.profile_publish import write_persona_files
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture
async def on_test_connection(db_session):
    conn = await db_session.connection()

    @contextlib.asynccontextmanager
    async def _factory():
        s = AsyncSession(bind=conn, expire_on_commit=False,
                         join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            await s.close()

    return _factory


async def _pi(db_session, slug, *, identity):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, agent_id=slug)
    if identity:
        db_session.add(PiGrantIdentity(user_id=user.id, status="no_match"))
    await db_session.flush()
    return user, agent


async def test_disabled_sweep_does_nothing(db_session, on_test_connection):
    assert await sweep_enabled(db_session) is False
    assert await run_persona_sweep(on_test_connection) is None


async def test_sweep_rewrites_only_stale_personas_of_pis_with_an_identity_row(
    db_session, on_test_connection, tmp_path, monkeypatch,
):
    public = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", public)
    stale, stale_agent = await _pi(db_session, "sweepstale", identity=True)
    fresh, _fresh_agent = await _pi(db_session, "sweepfresh", identity=True)
    none, _ = await _pi(db_session, "sweepnone", identity=False)
    await enable_persona_sweep(db_session)
    for uid in (stale.id, fresh.id, none.id):
        await write_persona_files(db_session, uid)
    (public / "sweepstale.md").write_text("stale")
    (public / "sweepnone.md").write_text("untouched")
    assert await run_persona_sweep(on_test_connection) == 1
    assert "Lab — Public Profile" in (public / "sweepstale.md").read_text()
    assert (public / "sweepnone.md").read_text() == "untouched"
    mechanisms = (await db_session.execute(
        select(ProfileRevision.agent_registry_id, ProfileRevision.mechanism)
    )).all()
    assert [(a, m) for a, m in mechanisms if m == "persona_sweep"] == [
        (stale_agent.id, "persona_sweep")
    ]
    assert (await db_session.execute(select(AppSetting.value).where(
        AppSetting.key == PERSONA_SWEEP_SETTING))).scalar_one() == "true"
