"""Staff-accounts plan §5.5: a staff conversion that commits after a caller loaded the
user must not leave a manager or reviewer with a queued profile job, nor be overwritten
by an Add-PI adoption whose ORCID fetch was in flight. Each test commits its rows through
separate sessions (the change must come from another transaction) and deletes them."""
import asyncio
import random

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AgentRegistry,
    Job,
    Publication,
    User,
)
from src.services import pi_onboarding, staff_accounts, user_deletion
from src.services.identifiers import normalize_orcid
from src.services.profile_jobs import enqueue_profile_job_if_absent
from tests import factories

pytestmark = pytest.mark.integration


def _orcid() -> str:
    """A random checksum-valid iD (Add-PI validates the check digit, R2.2), outside the
    factory's 0000-0000- block so committed rows never collide with factory users."""
    base = f"0009{random.randrange(10**11):011d}"
    for check in "0123456789X":
        if (orcid := normalize_orcid(base + check)) is not None:
            return orcid
    raise AssertionError(f"no check character for {base}")


async def _committed_pi(factory, **overrides) -> User:
    async with factory() as s:
        user = await factories.make_user(s, orcid=_orcid(), **overrides)
        await s.commit()
    return user


async def _set_stored(factory, user_id, **values) -> None:
    """Change the row from another transaction; fails fast instead of waiting on a
    row lock the code under test should not be holding."""
    async with factory() as s:
        await s.execute(text("SET LOCAL lock_timeout = '5s'"))
        await s.execute(update(User).where(User.id == user_id).values(**values))
        await s.commit()


async def _job_count(factory, user_id) -> int:
    async with factory() as s:
        return (await s.execute(
            select(func.count()).select_from(Job).where(Job.user_id == user_id)
        )).scalar_one()


async def _delete(factory, user_id) -> None:
    async with factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        await s.commit()


@pytest.mark.parametrize(
    "stored,queued",
    [
        ({"user_role": USER_ROLE_MANAGER}, False),
        ({"user_role": USER_ROLE_REVIEWER}, False),
        ({"access_status": "denied"}, False),
        ({"user_role": USER_ROLE_PI}, True),
    ],
    ids=["manager", "reviewer", "denied", "pi"],
)
async def test_enqueue_decides_on_the_stored_role_not_the_loaded_one(engine, stored, queued):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _committed_pi(factory, access_status="allowed")
    try:
        async with factory() as s:
            user = await s.get(User, seed.id)
            await _set_stored(factory, seed.id, **stored)
            assert user.user_role == USER_ROLE_PI and user.access_status == "allowed"
            job = await enqueue_profile_job_if_absent(s, user)
            assert (job is not None) is queued
            await s.commit()
        assert await _job_count(factory, seed.id) == (1 if queued else 0)
    finally:
        await _delete(factory, seed.id)


async def test_enqueue_sends_the_callers_pending_edit_first(engine):
    """The column re-read autoflushes: an in-memory demotion to reviewer, not yet
    flushed, already counts."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _committed_pi(factory)
    try:
        async with factory() as s:
            user = await s.get(User, seed.id)
            user.user_role = USER_ROLE_REVIEWER
            assert await enqueue_profile_job_if_absent(s, user) is None
            assert user.user_role == USER_ROLE_REVIEWER
    finally:
        await _delete(factory, seed.id)


@pytest.mark.parametrize(
    "stored",
    [{"user_role": USER_ROLE_MANAGER}, {"user_role": USER_ROLE_REVIEWER},
     {"access_status": "denied"}],
    ids=["manager", "reviewer", "denied"],
)
async def test_adopt_refuses_an_account_converted_during_the_orcid_fetch(
    engine, monkeypatch, stored
):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _committed_pi(factory, access_status="pending")

    async def converting_fetch(orcid):
        await _set_stored(factory, seed.id, **stored)
        return {"orcid": orcid, "name": None, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", converting_fetch)
    try:
        async with factory() as s:
            with pytest.raises(ValueError, match="already exists"):
                await pi_onboarding.adopt_agentless_pi(s, seed.orcid)
            await s.rollback()
        assert await _job_count(factory, seed.id) == 0
    finally:
        await _delete(factory, seed.id)


async def test_enqueue_waits_for_an_uncommitted_conversion(engine):
    """A caller that never updated the user row: its role re-read must wait behind a
    conversion holding the row FOR UPDATE and see the committed reviewer role."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _committed_pi(factory, access_status="allowed")
    s1 = factory()
    s2 = factory()
    task = None
    try:
        user = await s2.get(User, seed.id)  # loaded as pi, before the conversion
        await s2.execute(text("SET LOCAL lock_timeout = '10s'"))
        pid = (await s2.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        await s1.execute(select(User.id).where(User.id == seed.id).with_for_update())
        await s1.execute(
            update(User).where(User.id == seed.id).values(user_role=USER_ROLE_REVIEWER))

        task = asyncio.create_task(enqueue_profile_job_if_absent(s2, user))
        # Wait (bounded) until connection 2 is blocked on a lock, so the commit below
        # lands while it waits rather than before it reads.
        async with factory() as probe:
            for _ in range(200):
                if task.done():
                    break
                waiting = (await probe.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p"),
                    {"p": pid},
                )).scalar_one_or_none()
                if waiting == "Lock":
                    break
                await asyncio.sleep(0.05)
        await s1.commit()

        job = await asyncio.wait_for(task, timeout=15)
        assert job is None
        await s2.commit()
        assert await _job_count(factory, seed.id) == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
        await s1.rollback()
        await s2.rollback()
        await s1.close()
        await s2.close()
        await _delete(factory, seed.id)


async def test_adopt_returns_none_for_an_account_deleted_during_the_orcid_fetch(
    engine, monkeypatch
):
    """Add-PI then falls through to creating the account (find_or_create_pi_by_orcid)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _committed_pi(factory, access_status="pending")

    async def deleting_fetch(orcid):
        await _delete(factory, seed.id)
        return {"orcid": orcid, "name": None, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", deleting_fetch)
    try:
        async with factory() as s:
            assert await pi_onboarding.adopt_agentless_pi(s, seed.orcid) is None
            await s.commit()
        async with factory() as s:
            assert (await s.execute(
                select(func.count()).select_from(User).where(User.orcid == seed.orcid)
            )).scalar_one() == 0
        assert await _job_count(factory, seed.id) == 0
    finally:
        await _delete(factory, seed.id)


async def test_adopt_still_adopts_an_agentless_pi(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seed = await _committed_pi(factory, access_status="pending")

    async def fetch(orcid):
        return {"orcid": orcid, "name": None, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", fetch)
    try:
        async with factory() as s:
            got = await pi_onboarding.adopt_agentless_pi(s, seed.orcid)
            assert got is not None and got.id == seed.id
            await s.commit()
        assert await _job_count(factory, seed.id) == 1
    finally:
        await _delete(factory, seed.id)


async def test_deleting_a_reviewer_waits_for_a_persona_writer_without_deadlock(
    engine, tmp_path, monkeypatch,
):
    """A converted-PI reviewer's persona writer holds the agents row FOR KEY SHARE and then
    inserts a row whose FK check needs FOR KEY SHARE on the users row. An admin's delete in
    between must wait on the agents row, not hold the users row and deadlock."""
    for name in ("_PUBLIC_DIR", "_MEMORY_DIR", "_COMPANIES_DIR", "_ORPHANED_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setattr(user_deletion, name, tmp_path / name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _committed_pi(factory, user_role=USER_ROLE_ADMIN, access_status="allowed")
    reviewer = await _committed_pi(factory, user_role=USER_ROLE_REVIEWER, access_status="allowed")
    async with factory() as s:
        await factories.make_agent(s, user_id=reviewer.id, status="suspended")
        await s.commit()
    try:
        async with factory() as writer:
            await writer.execute(text("SET LOCAL lock_timeout = '5s'"))
            await writer.execute(
                select(AgentRegistry.id).where(AgentRegistry.user_id == reviewer.id)
                .with_for_update(read=True, key_share=True)
            )

            async def delete():
                async with factory() as s:
                    await s.execute(text("SET LOCAL lock_timeout = '10s'"))
                    actor = await s.get(User, admin.id)
                    return await staff_accounts.delete_reviewer(
                        s, actor=actor, user_id=reviewer.id)

            task = asyncio.create_task(delete())
            await asyncio.sleep(0.5)
            assert not task.done()  # waiting on the writer's agents lock
            writer.add(Publication(user_id=reviewer.id, pmid="31000003", title="Mid-delete"))
            await writer.flush()  # the FK check: FOR KEY SHARE on the users row
            await writer.commit()
            await asyncio.wait_for(task, 20)
        async with factory() as s:
            assert await s.get(User, reviewer.id) is None
    finally:
        async with factory() as s:  # the deletion's audit row outlives both users
            await s.execute(text("DELETE FROM admin_audit_events WHERE payload->>'user_id' = :u"),
                            {"u": str(reviewer.id)})
            await s.commit()
        await _delete(factory, reviewer.id)
        await _delete(factory, admin.id)


async def test_self_delete_waits_for_a_first_ingest_then_refuses(engine):
    """A converted PI's first-ingest pipeline holds the persona lock and is still inserting
    publications (each FK check takes FOR KEY SHARE on the users row). The self-delete must
    wait on the persona lock without holding the users row FOR UPDATE, then see the
    committed rows and refuse (F18), not deadlock."""
    from types import SimpleNamespace

    from src.routers.profile import delete_account
    from src.services.advisory_locks import lock_agent_persona

    factory = async_sessionmaker(engine, expire_on_commit=False)
    manager = await _committed_pi(factory, user_role=USER_ROLE_MANAGER, access_status="allowed")
    try:
        async with factory() as pipeline:
            await pipeline.execute(text("SET LOCAL lock_timeout = '5s'"))
            await lock_agent_persona(pipeline, manager.id)

            async def self_delete():
                async with factory() as s:
                    await s.execute(text("SET LOCAL lock_timeout = '10s'"))
                    user = await s.get(User, manager.id)
                    return await delete_account(
                        SimpleNamespace(session={}), confirm="delete", db=s, current_user=user)

            task = asyncio.create_task(self_delete())
            await asyncio.sleep(0.5)
            assert not task.done()  # waiting on the persona lock
            pipeline.add(Publication(user_id=manager.id, pmid="31000004", title="First ingest"))
            await pipeline.flush()  # the FK check: FOR KEY SHARE on the users row
            await pipeline.commit()
            response = await asyncio.wait_for(task, 20)
        assert response.headers["location"] == "/profile/delete-account?error=research_data"
        async with factory() as s:
            assert await s.get(User, manager.id) is not None
    finally:
        await _delete(factory, manager.id)


async def test_self_delete_and_a_role_change_take_the_admin_lock_first(engine, tmp_path,
                                                                         monkeypatch):
    """The admin role route takes the admin-invariant lock, then updates the users row. A
    self-delete must take the same lock before it locks its own row, or the two deadlock."""
    from types import SimpleNamespace

    from src.routers.profile import delete_account
    from src.services.admin_invariant import lock_admin_invariant

    for name in ("_PUBLIC_DIR", "_MEMORY_DIR", "_COMPANIES_DIR", "_ORPHANED_DIR"):
        (tmp_path / name).mkdir()
        monkeypatch.setattr(user_deletion, name, tmp_path / name)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    pi = await _committed_pi(factory, access_status="allowed")
    try:
        async with factory() as role_route:
            await role_route.execute(text("SET LOCAL lock_timeout = '5s'"))
            await lock_admin_invariant(role_route)

            async def self_delete():
                async with factory() as s:
                    await s.execute(text("SET LOCAL lock_timeout = '10s'"))
                    user = await s.get(User, pi.id)
                    return await delete_account(
                        SimpleNamespace(session={}), confirm="delete", db=s, current_user=user)

            task = asyncio.create_task(self_delete())
            await asyncio.sleep(0.5)
            await role_route.execute(
                update(User).where(User.id == pi.id).values(user_role=USER_ROLE_PI))
            await role_route.commit()
            response = await asyncio.wait_for(task, 20)
        assert response.headers["location"] == "/login?deleted=1"
    finally:
        await _delete(factory, pi.id)
