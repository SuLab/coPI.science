"""A profile save that meets a held persona lock (a generation job claimed just after the
in-flight check) refuses with PROFILE_GENERATING within the bound instead of waiting out
the whole run. Committing sessions: the holder must be another connection."""
import asyncio
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import User
from src.services import profile_edit
from src.services.profile_publish import lock_persona_writer
from tests import factories

pytestmark = pytest.mark.integration


async def test_save_refuses_while_the_persona_lock_is_held(engine, monkeypatch):
    monkeypatch.setattr(profile_edit, "PROFILE_INSERT_LOCK_TIMEOUT", "200ms")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        pi = await factories.make_user(s, name="Original Name")
        await s.commit()
    try:
        async with factory() as holder:
            await lock_persona_writer(holder, pi.id)
            async with factory() as s2:
                target = (await s2.execute(select(User).where(User.id == pi.id))).scalar_one()
                started = time.monotonic()
                result = await asyncio.wait_for(profile_edit.apply_profile_edits(
                    s2, target_user=target, changed_by_user_id=pi.id,
                    form={"name": "Changed Name"}, expected_version=None,
                ), timeout=10)
                assert result == profile_edit.PROFILE_GENERATING
                assert time.monotonic() - started < 5
            await holder.rollback()
        async with factory() as s:
            name = (await s.execute(select(User.name).where(User.id == pi.id))).scalar_one()
        assert name == "Original Name"
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": pi.id})
            await s.commit()
