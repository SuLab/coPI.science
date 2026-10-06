"""A profile save that meets a held persona lock (a generation job claimed just after the
in-flight check) refuses with PROFILE_GENERATING within the bound instead of waiting out
the whole run. Committing sessions: the holder must be another connection."""
import asyncio
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import ProfileRevision, ResearcherProfile, User
from src.services import profile_edit
from src.services.profile_publish import lock_persona_writer, reexport_persona, write_persona_files
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


async def test_edit_revision_keeps_the_lock_and_orders_before_a_competing_writer(
    engine, monkeypatch, tmp_path,
):
    """Commit/revision must not admit a competing profile writer between them."""
    from src.services import profile_export

    monkeypatch.setattr(profile_export, 'PROFILES_DIR', tmp_path)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        pi = await factories.make_user(db, name='Locked Scientist')
        await factories.make_profile(db, user=pi, profile_version=1)
        agent = await factories.make_agent(db, user=pi, agent_id='atomic-edit-revision')
        await db.commit()
    before_revision = asyncio.Event()
    contender_started = asyncio.Event()
    contender_acquired = asyncio.Event()
    recorder = profile_edit.export_and_record

    async def paused_recorder(db, **kw):
        before_revision.set()
        await contender_started.wait()
        # This wait tests actual exclusion, not a mock of lock acquisition.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(contender_acquired.wait(), timeout=.2)
        return await recorder(db, **kw)

    async def contender():
        await before_revision.wait()
        async with factory() as db:
            contender_started.set()
            await lock_persona_writer(db, pi.id)
            contender_acquired.set()
            profile = (await db.execute(select(ResearcherProfile).where(
                ResearcherProfile.user_id == pi.id))).scalar_one()
            profile.research_summary = 'Newer competing summary.'
            profile.profile_version += 1
            await db.flush()
            await reexport_persona(db, pi.id, mechanism='pipeline')
            await db.commit()
            await write_persona_files(db, pi.id)

    monkeypatch.setattr(profile_edit, 'export_and_record', paused_recorder)
    task = asyncio.create_task(contender())
    try:
        async with factory() as db:
            target = (await db.execute(select(User).where(User.id == pi.id))).scalar_one()
            assert await profile_edit.apply_profile_edits(
                db, target_user=target, changed_by_user_id=pi.id, expected_version=1,
                form={'research_summary': 'The human edit summary.'},
            ) is None
        await asyncio.wait_for(task, timeout=10)
        async with factory() as db:
            revisions = list((await db.execute(select(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent.id).order_by(
                    ProfileRevision.created_at, ProfileRevision.id))).scalars())
        assert [r.mechanism for r in revisions] == ['web', 'pipeline']
        assert 'The human edit summary.' in revisions[0].content
        assert 'Newer competing summary.' in revisions[-1].content
        assert (tmp_path/'atomic-edit-revision.md').read_text() == revisions[-1].content
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        async with factory() as db:
            await db.execute(text('DELETE FROM users WHERE id=:uid'), {'uid': pi.id})
            await db.commit()
