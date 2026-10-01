"""At least one admin who can log in must remain (RA-09).

Both doors out of adminhood, a role change and a self-deletion, call this inside
their transaction. The advisory transaction lock serializes them, so two
concurrent demotions can no longer both read "2 admins" and leave none. The count
rule is unchanged: admins whose access_status is 'allowed' (a denied/pending admin
cannot log in, so is not a way back)."""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_ADMIN, User
from src.services.advisory_locks import ADMIN_INVARIANT_LOCK_KEY


class LastAdminError(ValueError):
    """Removing `user` from adminhood would leave no admin able to log in."""


async def ensure_admin_remains(db: AsyncSession, *, user: User) -> None:
    """Take the admin-invariant lock (held to the end of the caller's transaction),
    then raise LastAdminError when `user` is an admin and at most one allowed
    admin exists. The caller must write the change in the same transaction."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADMIN_INVARIANT_LOCK_KEY})
    if user.user_role != USER_ROLE_ADMIN:
        return
    count = await db.scalar(
        select(func.count(User.id)).where(
            User.user_role == USER_ROLE_ADMIN, User.access_status == "allowed"
        )
    )
    if (count or 0) <= 1:
        raise LastAdminError("Cannot remove the last remaining admin")
