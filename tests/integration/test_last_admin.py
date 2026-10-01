# tests/integration/test_last_admin.py
import asyncio
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import USER_ROLE_ADMIN, USER_ROLE_PI, User
from src.services.admin_invariant import LastAdminError, ensure_admin_remains

pytestmark = pytest.mark.integration


async def test_concurrent_demotions_leave_an_admin(engine):
    """Review Focus 1: X demotes Y while Y demotes X."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # Only these two admins may exist for the rule to bite; park any others.
        others = (await s.execute(select(User.id).where(User.user_role == USER_ROLE_ADMIN))).scalars().all()
        await s.execute(update(User).where(User.id.in_(others)).values(user_role=USER_ROLE_PI))
        x = User(name="X", orcid=f"0000-0005-{uuid.uuid4().int % 10000:04d}-0001", user_role=USER_ROLE_ADMIN, access_status="allowed")
        y = User(name="Y", orcid=f"0000-0005-{uuid.uuid4().int % 10000:04d}-0002", user_role=USER_ROLE_ADMIN, access_status="allowed")
        s.add_all([x, y])
        await s.commit()

    async def demote(target_id):
        async with factory() as s:
            target = await s.get(User, target_id)
            try:
                await ensure_admin_remains(s, user=target)
            except LastAdminError:
                await s.rollback()
                return "refused"
            await asyncio.sleep(0.2)  # hold the lock across the write
            target.user_role = USER_ROLE_PI
            await s.commit()
            return "demoted"

    try:
        results = sorted(await asyncio.gather(demote(x.id), demote(y.id)))
        assert results == ["demoted", "refused"]
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": x.id, "b": y.id})
            await s.execute(update(User).where(User.id.in_(others)).values(user_role=USER_ROLE_ADMIN))
            await s.commit()
