"""The post-commit persona writer under the per-PI lock (spec 2026-10-05 §4.3)."""
import asyncio
import inspect
import logging
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import AgentRegistry, ProfileRevision, ResearcherProfile, User
from src.services import profile_export, user_deletion
from src.services.advisory_locks import lock_agent_persona
from src.services.grant_sections import EMPTY_GRANT_SECTIONS
from src.services.profile_publish import (
    export_and_record,
    reexport_persona,
    schedule_persona_write,
    write_persona_files,
)
from src.services.tenure_scope import scoped_publications_for_export
from src.services.user_deletion import delete_user_account
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def public(tmp_path, monkeypatch):
    out = tmp_path / "public"
    monkeypatch.setattr(profile_export, "PROFILES_DIR", out)
    monkeypatch.setattr(user_deletion, "_PUBLIC_DIR", out)
    monkeypatch.setattr(user_deletion, "_MEMORY_DIR", tmp_path / "mem")
    return out


async def _seed(factory, summary="Before."):
    slug = f"pw{uuid.uuid4().hex[:8]}"
    async with factory() as s:
        user = await factories.make_user(s)
        await factories.make_profile(s, user=user, research_summary=summary)
        await factories.make_agent(s, user=user, agent_id=slug)
        await s.commit()
        return user.id, slug


async def _drop(factory, user_id, slug):
    async with factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        await s.execute(text("DELETE FROM agents WHERE agent_id = :a"), {"a": slug})
        await s.commit()


async def _revisions(session, agent_id):
    return (await session.execute(
        select(ProfileRevision).where(ProfileRevision.agent_registry_id == agent_id)
    )).scalars().all()


async def test_export_and_record_records_the_rendered_text_and_writes_no_file(db_session, public):
    user = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, agent_id="earec1")
    pubs = await scoped_publications_for_export(db_session, user.id, agent.agent_id)
    rendered = await export_and_record(
        db_session, user=user, profile=profile, agent=agent, publications=pubs,
        grants=EMPTY_GRANT_SECTIONS, mechanism="web", changed_by_user_id=user.id,
    )
    assert not (public / "earec1.md").exists()
    [rev] = await _revisions(db_session, agent.id)
    assert (rev.content, rev.mechanism) == (rendered, "web")
    await db_session.commit()
    assert (await write_persona_files(db_session, user.id)).read_text() == rendered


async def test_skip_if_file_matches_records_nothing(db_session, public):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    agent = await factories.make_agent(db_session, user=user, agent_id="earec2")
    await write_persona_files(db_session, user.id)
    assert await reexport_persona(
        db_session, user.id, mechanism="reexport", skip_if_file_matches=True
    ) is None
    assert await _revisions(db_session, agent.id) == []


async def test_schedule_appends_with_a_list_and_writes_inline_without(db_session, public):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    await factories.make_agent(db_session, user=user, agent_id="earec3")
    queued = []
    await schedule_persona_write(db_session, user.id, queued)
    assert len(queued) == 1 and not (public / "earec3.md").exists()
    await schedule_persona_write(db_session, user.id, None)
    assert (public / "earec3.md").exists()


async def test_a_waiting_writer_renders_what_the_lock_holder_committed(factory, public):
    user_id, slug = await _seed(factory)
    try:
        async with factory() as holder:
            await lock_agent_persona(holder, user_id)
            async with factory() as writer:
                task = asyncio.create_task(write_persona_files(writer, user_id))
                await asyncio.sleep(0.5)
                assert not task.done(), "the writer must wait for the per-PI lock"
                await holder.execute(
                    text("UPDATE researcher_profiles SET research_summary = 'After.' "
                         "WHERE user_id = :u"),
                    {"u": user_id},
                )
                await holder.commit()
                path = await asyncio.wait_for(task, timeout=30)
        written = path.read_text()
        assert "After." in written and "Before." not in written
    finally:
        await _drop(factory, user_id, slug)


async def test_deletion_waits_for_a_writer_holding_the_lock_and_removes_its_file(factory, public):
    user_id, slug = await _seed(factory)
    try:
        async with factory() as w:
            await write_persona_files(w, user_id)
        assert (public / f"{slug}.md").exists()
        async with factory() as holder:
            await lock_agent_persona(holder, user_id)
            async with factory() as d:
                user = await d.get(User, user_id)
                task = asyncio.create_task(delete_user_account(d, user))
                await asyncio.sleep(0.5)
                assert not task.done(), "deletion takes the persona lock before it commits"
                await holder.commit()
                await asyncio.wait_for(task, timeout=30)
        assert not (public / f"{slug}.md").exists()
        async with factory() as late:
            assert await write_persona_files(late, user_id) is None
        assert not (public / f"{slug}.md").exists()
    finally:
        await _drop(factory, user_id, slug)


async def test_a_revision_committed_during_deletion_is_purged(factory, public):
    """Before: the revision purge ran before db.delete(user), so a revision transaction
    committing in between left a persona snapshot of a deleted account."""
    user_id, slug = await _seed(factory)
    try:
        async with factory() as writer:
            user = await writer.get(User, user_id)
            profile = (await writer.execute(select(ResearcherProfile).where(
                ResearcherProfile.user_id == user_id))).scalar_one()
            agent = (await writer.execute(select(AgentRegistry).where(
                AgentRegistry.user_id == user_id))).scalar_one()
            pubs = await scoped_publications_for_export(writer, user_id, agent.agent_id)
            assert await export_and_record(
                writer, user=user, profile=profile, agent=agent, publications=pubs,
                grants=EMPTY_GRANT_SECTIONS, mechanism="web",
            )
            async with factory() as d:
                task = asyncio.create_task(delete_user_account(d, await d.get(User, user_id)))
                await asyncio.sleep(0.5)
                assert not task.done(), "deletion waits for the open revision transaction"
                await writer.commit()
                report = await asyncio.wait_for(task, timeout=30)
        assert report.revisions_deleted == 1
        async with factory() as s:
            left = await s.scalar(select(func.count()).select_from(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent.id))
        assert left == 0
    finally:
        await _drop(factory, user_id, slug)


def test_deletion_locks_after_the_delete_and_before_the_commit():
    src = inspect.getsource(user_deletion.delete_user_account)
    delete_at = src.index("db.delete(user)")
    lock_at = src.index("lock_agent_persona(")
    assert delete_at < lock_at < src.index("await db.commit()", lock_at)


async def test_a_failed_write_is_logged_and_swallowed(db_session, tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(profile_export, "PROFILES_DIR", blocker / "public")
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    await factories.make_agent(db_session, user=user, agent_id="earec4")
    with caplog.at_level(logging.ERROR):
        assert await write_persona_files(db_session, user.id) is None
    assert "Persona write failed" in caplog.text or "Failed to export profile" in caplog.text
