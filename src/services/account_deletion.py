"""Shared guard against deleting a user account that would orphan a live agent.

``agents.user_id`` is ``ondelete="SET NULL"``, not ``CASCADE`` -- deleting the
owner of an active (or one-click-from-active ``pending``) agent leaves a Slack
bot on the simulation roster that nobody can deactivate, edit, or answer
proposals for. Both the self-service delete-account flow (``src/routers/
profile.py``) and the admin delete route (``src/routers/admin.py``) must
refuse in exactly this case (#25 D1; audit 2026-09-08 RC-8 closed the
admin-route gap).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, User

# AgentRegistry.status values that make an owned agent too live to orphan.
# 'active' is on the simulation roster and posting to Slack as the PI;
# 'pending' is one admin click from it, because admin_update_agent promotes a
# pending row straight to 'active' without re-reading user_id. The parked
# states ('inactive', 'suspended') are deliberately absent: "deactivate the
# agent" is the remedy the refusal names, so it has to unblock the delete.
# See docs/plans/2026-09-04-decisions/task-25.md.
AGENT_STATUSES_BLOCKING_ACCOUNT_DELETE = ("active", "pending")


async def agent_blocking_account_delete(
    db: AsyncSession, user: User
) -> AgentRegistry | None:
    """The agent this user owns that account deletion would orphan, if any.

    Ownership is ``agents.user_id`` (a UNIQUE column, so at most one row). A
    delegation is not ownership: ``agent_delegates.user_id`` is
    ``ondelete="CASCADE"``, so deleting a delegate removes the delegation and
    leaves the agent's owner alone.
    """
    result = await db.execute(
        select(AgentRegistry).where(
            AgentRegistry.user_id == user.id,
            AgentRegistry.status.in_(AGENT_STATUSES_BLOCKING_ACCOUNT_DELETE),
        )
    )
    return result.scalar_one_or_none()
