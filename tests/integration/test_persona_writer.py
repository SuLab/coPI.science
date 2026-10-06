"""The post-commit persona writer under the per-PI lock (spec 2026-10-05 §4.3)."""
import asyncio
import inspect
import logging
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import AgentRegistry, PiOrcidFunding, ProfileRevision, ResearcherProfile, User
from src.services import orcid_fundings, profile_export, profile_publish, user_deletion
from src.services.advisory_locks import lock_agent_persona
from src.services.grant_sections import EMPTY_GRANT_SECTIONS
from src.services.profile_publish import (
    export_and_record,
    lock_persona_writer,
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
    """Before: the revision purge could run before a revision transaction committed, which
    left a persona snapshot of a deleted account. Deletion now waits for the writer's
    locks before it purges."""
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


async def test_deletion_waits_for_a_writer_holding_the_persona_locks_without_deadlock(
    factory, public,
):
    """Before: deletion deleted (row-locking the PI's child rows) and only then took the
    persona lock, so a writer holding lock_persona_writer that went on to write a child
    row deadlocked with it."""
    user_id, slug = await _seed(factory)
    funding = orcid_fundings.OrcidFunding(
        "hash:pw", "Award", "NIH", "grant", 2020, None, 2030, None, ())
    try:
        async with factory() as w:
            await write_persona_files(w, user_id)
        assert (public / f"{slug}.md").exists()
        async with factory() as writer:
            await lock_persona_writer(writer, user_id)
            async with factory() as d:
                task = asyncio.create_task(delete_user_account(d, await d.get(User, user_id)))
                await asyncio.sleep(0.5)
                assert not task.done(), "deletion waits for the writer's locks"
                await orcid_fundings.store_orcid_fundings(writer, user_id, [funding])
                assert await reexport_persona(writer, user_id, mechanism="web")
                await writer.commit()
                report = await asyncio.wait_for(task, timeout=30)
        assert report.revisions_deleted == 1
        assert not (public / f"{slug}.md").exists()
        async with factory() as s:
            agent_pk = await s.scalar(select(AgentRegistry.id).where(AgentRegistry.agent_id == slug))
            assert await s.scalar(select(func.count()).select_from(ProfileRevision).where(
                ProfileRevision.agent_registry_id == agent_pk)) == 0
            assert await s.scalar(select(func.count()).select_from(PiOrcidFunding).where(
                PiOrcidFunding.user_id == user_id)) == 0
            assert await s.get(User, user_id) is None
    finally:
        await _drop(factory, user_id, slug)


def test_deletion_locks_before_it_writes():
    src = inspect.getsource(user_deletion._delete_in_transaction)
    row_lock_at = src.index("with_for_update()")
    lock_at = src.index("lock_agent_persona(")
    assert row_lock_at < lock_at < src.index("sa_delete(ProfileRevision)")
    assert lock_at < src.index("db.delete(user)")


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


async def test_a_crlf_summary_matches_its_own_file(db_session, public):
    """Before: the file was read with newline translation, so a summary holding \\r\\n
    never matched and every sweep rewrote it and recorded a revision."""
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, research_summary="a\r\nb")
    agent = await factories.make_agent(db_session, user=user, agent_id="earec5")
    assert await reexport_persona(
        db_session, user.id, mechanism="reexport", skip_if_file_matches=True
    ) is not None
    await write_persona_files(db_session, user.id)
    assert await reexport_persona(
        db_session, user.id, mechanism="reexport", skip_if_file_matches=True
    ) is None
    assert len(await _revisions(db_session, agent.id)) == 1


async def test_a_waiting_reexport_renders_what_the_lock_holder_committed(factory, public):
    """Before: reexport_persona loaded the profile before taking the lock, so a call that
    waited rendered the pre-commit snapshot."""
    user_id, slug = await _seed(factory)
    try:
        async with factory() as holder:
            await lock_agent_persona(holder, user_id)
            async with factory() as writer:
                task = asyncio.create_task(reexport_persona(writer, user_id, mechanism=None))
                await asyncio.sleep(0.5)
                assert not task.done(), "the re-export must wait for the per-PI lock"
                await holder.execute(
                    text("UPDATE researcher_profiles SET research_summary = 'After.' "
                         "WHERE user_id = :u"),
                    {"u": user_id},
                )
                await holder.commit()
                rendered = await asyncio.wait_for(task, timeout=30)
                await writer.rollback()
        assert "After." in rendered and "Before." not in rendered
    finally:
        await _drop(factory, user_id, slug)


async def test_a_failed_post_commit_write_leaves_the_callers_session_usable(
    factory, public, monkeypatch, caplog,
):
    """Before: the failure rolled back the caller's session, expiring its objects, so the
    next attribute read (onboarding's current_user.id) raised MissingGreenlet."""
    user_id, slug = await _seed(factory)

    async def broken_companies_export(db, uid):
        raise RuntimeError("companies export failed")

    monkeypatch.setattr(profile_publish, "export_companies_file", broken_companies_export)
    try:
        async with factory() as caller:
            user = await caller.get(User, user_id)
            with caplog.at_level(logging.ERROR):
                assert await write_persona_files(caller, user_id) is None
            assert user.id == user_id and user.orcid
        assert "Persona write failed after commit" in caplog.text
    finally:
        await _drop(factory, user_id, slug)


async def test_a_post_commit_write_refuses_while_the_caller_holds_the_persona_lock(
    factory, public,
):
    """Before: the write's own connection waited forever on the caller's advisory lock."""
    user_id, slug = await _seed(factory)
    try:
        async with factory() as caller:
            await lock_persona_writer(caller, user_id)
            with pytest.raises(RuntimeError, match="commit it first"):
                await write_persona_files(caller, user_id)
            await caller.commit()
            await caller.execute(select(User.id).where(User.id == user_id))  # read-only autobegin
            assert await asyncio.wait_for(write_persona_files(caller, user_id), timeout=30)
            await lock_persona_writer(caller, user_id)
            await caller.rollback()
            assert await asyncio.wait_for(write_persona_files(caller, user_id), timeout=30)
    finally:
        await _drop(factory, user_id, slug)
