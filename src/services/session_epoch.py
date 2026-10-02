"""Server-side revocation for the signed session cookie (spec 2026-10-01 §6.7, A-11).

The session (src/main.py) is a signed, 30-day cookie with no server-side store.
``users.session_epoch`` is the server-side handle on it: login copies the account's
epoch into ``session[SESSION_EPOCH_KEY]``, ``get_current_user`` (src/dependencies.py)
refuses a session whose epoch differs, and bumping the epoch therefore ends every
session the account holds, on every device. Bumped by logout (src/routers/auth.py),
access denial (src/routers/admin/access.py) and a role change
(src/routers/admin/users.py, src/cli.py). NULL counts as 0, and so does a session with
no epoch key.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User

#: The session key holding the epoch the session was issued under.
SESSION_EPOCH_KEY = "epoch"


def current_epoch(user: User) -> int:
    """The account's epoch, NULL read as 0."""
    return user.session_epoch or 0


def epoch_in_session(session: Mapping[str, Any]) -> int | None:
    """The epoch a session was issued under: 0 when the key is absent, None when the
    value is not an integer (bool included), which no session this app wrote holds."""
    value = session.get(SESSION_EPOCH_KEY, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def bump_session_epoch(
    db: AsyncSession, user_id: uuid.UUID, *, expected_epoch: int | None = None
) -> None:
    """Increment ``users.session_epoch`` (NULL as 0), ending every session the account
    holds. Does not commit.

    With ``expected_epoch``, bumps only while the stored epoch still equals it, so a
    session that is already stale cannot end the account's newer sessions.
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(session_epoch=func.coalesce(User.session_epoch, 0) + 1)
    )
    if expected_epoch is not None:
        stmt = stmt.where(func.coalesce(User.session_epoch, 0) == expected_epoch)
    await db.execute(stmt.execution_options(synchronize_session="fetch"))
