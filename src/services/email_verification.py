"""Verifying ``users.email`` (spec 2026-10-01 §6.6, decision D7).

An administrator may verify any user's address and a manager a PI's; there is no
verification email. ``users.email_verified_at`` is cleared by
``src/services/user_email.py::assign_user_email`` whenever the address changes, and
delegate-invitation acceptance (src/routers/invite.py) requires it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AdminAuditEvent, User

#: ``admin_audit_events.action`` recorded for a verification.
VERIFY_EMAIL_ACTION = "verify_email"


async def mark_email_verified(
    db: AsyncSession, *, target: User, actor: User, shown_email: str
) -> str | None:
    """Stamp ``target.email_verified_at``, record who vouched for it, and commit.

    ``shown_email`` is the address the verifier's page displayed, posted back with the
    form. Returns ``"no_email"`` when the target has no address and ``"email_changed"``
    when the address it holds now is not the one shown (compared case-insensitively):
    an address changed between page load and click is never stamped unseen. Both write
    nothing. Verifying an already verified address re-stamps it and records another
    event. The payload names the address, because it is that address that was vouched
    for and a later change clears the stamp.
    """
    if not target.email:
        return "no_email"
    if shown_email.strip().lower() != target.email.strip().lower():
        return "email_changed"
    target.email_verified_at = datetime.now(UTC)
    db.add(AdminAuditEvent(
        action=VERIFY_EMAIL_ACTION,
        actor_user_id=actor.id,
        payload={"user_id": str(target.id), "email": target.email},
    ))
    await db.commit()
    return None
