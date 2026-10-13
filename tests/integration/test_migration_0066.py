"""Migration 0066 (spec 2026-10-13 §5.1): user_microsoft_identities."""
import uuid

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.models import User, UserMicrosoftIdentity
from tests import factories

pytestmark = pytest.mark.integration
TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


async def _link(db, user, oid=None, tenant=TENANT):
    row = UserMicrosoftIdentity(user_id=user.id, tenant_id=tenant, object_id=oid or uuid.uuid4())
    db.add(row)
    await db.flush()
    return row


async def _count(db) -> int:
    return await db.scalar(select(func.count()).select_from(UserMicrosoftIdentity))


async def test_a_link_is_stamped_on_insert(db_session):
    row = await _link(db_session, await factories.make_user(db_session))
    assert row.linked_at is not None
    assert row.last_sign_in_at is None
    assert row.sign_in_name is None


async def test_one_microsoft_account_links_to_one_user(db_session):
    a, b = await factories.make_user(db_session), await factories.make_user(db_session)
    oid = uuid.uuid4()
    await _link(db_session, a, oid)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _link(db_session, b, oid)


async def test_one_link_per_user(db_session):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _link(db_session, user)


async def test_the_same_oid_in_another_tenant_is_another_account(db_session):
    a, b = await factories.make_user(db_session), await factories.make_user(db_session)
    oid = uuid.uuid4()
    await _link(db_session, a, oid)
    await _link(db_session, b, oid, tenant=uuid.uuid4())
    assert await _count(db_session) == 2


async def test_deleting_the_user_deletes_the_link_in_the_database(db_session):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    await db_session.execute(sa_delete(User).where(User.id == user.id))
    assert await _count(db_session) == 0


async def test_delete_user_account_removes_the_link_without_loading_it(db_session):
    from src.services.user_deletion import delete_user_account

    user = await factories.make_user(db_session)
    row = await _link(db_session, user)
    db_session.expunge(row)  # the link is never loaded on the deletion path
    await delete_user_account(db_session, user)
    assert await _count(db_session) == 0
