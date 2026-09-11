"""Integration test: two concurrent profile_version bumps both land.

Pins the atomic SQL-side increment added for `bump_profile_version`
(`src/services/profile_pipeline.py`). This must use TWO independent sessions on
two separate connections, not the shared, rolled-back `db_session` fixture from
`tests/conftest.py` — the bug this guards against (a lost update) only
reproduces across connections, since a single session's read-modify-write never
races itself. `engine` (session-scoped, `tests/conftest.py`) plus a local
`async_sessionmaker` gives each session its own connection and its own
transaction.

RED reasoning (pre-fix code, `profile.profile_version = (profile.profile_version
or 0) + 1` evaluated in Python after loading the row): both sessions load
`profile_version=0`, both compute `1` in Python, and whichever session commits
last silently overwrites the other's write — a fresh SELECT afterward reads
`1`, not `2`. The fix moves the read-modify-write into a single SQL statement
(`UPDATE ... SET profile_version = COALESCE(profile_version, 0) + 1 ...
RETURNING`), whose row lock serializes the two increments: the second UPDATE
blocks until the first commits, then re-evaluates COALESCE against the
already-incremented value. A fresh SELECT afterward must read `2`.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import ResearcherProfile, User
from src.services.profile_pipeline import bump_profile_version

pytestmark = pytest.mark.integration


async def _bump_and_commit(db, profile_id: uuid.UUID) -> None:
    """Load the row, bump it via the atomic helper, and commit — on `db`'s own
    connection/transaction, distinct from whatever other session is doing the
    same thing concurrently."""
    profile = (
        await db.execute(select(ResearcherProfile).where(ResearcherProfile.id == profile_id))
    ).scalar_one()
    profile.profile_version = await bump_profile_version(db, profile.id)
    await db.commit()


async def test_two_concurrent_savers_both_land_their_profile_version_increment(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    async with factory() as db:
        db.add(User(
            id=user_id,
            name="Race Test User",
            orcid=f"RACE-{user_id.hex[:20]}",
        ))
        db.add(ResearcherProfile(id=profile_id, user_id=user_id, profile_version=0))
        await db.commit()

    try:
        s1 = factory()
        s2 = factory()
        try:
            await asyncio.gather(
                _bump_and_commit(s1, profile_id),
                _bump_and_commit(s2, profile_id),
            )
        finally:
            await s1.close()
            await s2.close()

        async with factory() as db:
            version = await db.scalar(
                select(ResearcherProfile.profile_version).where(ResearcherProfile.id == profile_id)
            )
        assert version == 2, (
            f"expected both concurrent bumps to land (2), got {version} — a lost "
            "update means the increment is happening as a Python read-modify-write "
            "again instead of the atomic SQL-side COALESCE(...) + 1"
        )
    finally:
        async with factory() as cleanup_db:
            await cleanup_db.execute(
                text("DELETE FROM researcher_profiles WHERE id = :p"), {"p": profile_id}
            )
            await cleanup_db.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            await cleanup_db.commit()
