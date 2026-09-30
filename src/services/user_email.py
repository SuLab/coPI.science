"""The one writer of ``users.email``.

``users.email`` is a plain, case-sensitive UNIQUE column that delegate-invitation
acceptance binds to and ``users.lookupByEmail`` reads. A login whose ORCID
address another account already held used to 500 on the unique violation, and a
case variant was silently stored as a second account's address.

``assign_user_email`` stores the address EXACTLY as its caller hands it — each
caller keeps its own normalisation (profile edits and onboarding strip and
lowercase; the ORCID login and the CLI store ORCID's value as-is), so no stored
address and no ``lookupByEmail`` input changes. It refuses an address another
user holds in ANY case, and confines a racing unique violation to a savepoint.
tests/unit/test_email_writer_tripwire.py fails on any other ``users.email``
write in src/.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User

logger = logging.getLogger(__name__)


async def assign_user_email(db: AsyncSession, user: User, email: str | None) -> bool:
    """Set ``user.email``; False — nothing changed — when another user holds it.

    ``None`` clears the address and always succeeds. ``user`` must already be
    flushed (it needs an id). Never raises on a conflict: the login path must be
    able to proceed without an email.
    """
    if email is None:
        user.email = None
        return True
    holder = await db.scalar(
        select(User.id)
        .where(func.lower(User.email) == email.lower(), User.id != user.id)
        .limit(1)
    )
    if holder is not None:
        logger.info(
            "Refused an email for user %s: user %s already holds it (case-insensitively)",
            user.id, holder,
        )
        return False
    # Flush everything else pending OUTSIDE the savepoint, so a rollback below
    # discards only this assignment.
    await db.flush()
    # Read before the savepoint: its rollback expires the user's attributes, and a
    # lazy load of an expired attribute is not allowed under asyncio.
    user_id = user.id
    try:
        async with db.begin_nested():
            user.email = email
            await db.flush()
    except IntegrityError:
        logger.warning(
            "Refused an email for user %s: another session took the address first", user_id,
        )
        # The savepoint rollback expired the user's attributes; reload them all so
        # callers (and a retry) can read the object without a lazy load.
        await db.refresh(user)
        return False
    return True
