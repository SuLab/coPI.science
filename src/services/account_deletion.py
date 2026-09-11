"""Shared guard against deleting a user account that would orphan a live agent.

``agents.user_id`` is ``ondelete="SET NULL"``, not ``CASCADE`` -- deleting the
owner of an active (or one-click-from-active ``pending``) agent leaves a Slack
bot on the simulation roster that nobody can deactivate, edit, or answer
proposals for. Both the self-service delete-account flow (``src/routers/
profile.py``) and the admin delete route (``src/routers/admin.py``) must
refuse in exactly this case.
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
AGENT_STATUSES_BLOCKING_ACCOUNT_DELETE = ("active", "pending")


async def agent_blocking_account_delete(
    db: AsyncSession, user: User, *, for_update: bool = True
) -> AgentRegistry | None:
    """The agent this user owns that account deletion would orphan, if any.

    Ownership is ``agents.user_id`` (a UNIQUE column, so at most one row). A
    delegation is not ownership: ``agent_delegates.user_id`` is
    ``ondelete="CASCADE"``, so deleting a delegate removes the delegation and
    leaves the agent's owner alone.

    ``with_for_update()`` (keyed on
    ``user_id`` alone) locks the row this reads
    for the rest of the caller's transaction, so the check and the delete it
    gates cannot race an admin's concurrent ``UPDATE ... SET status='active'``
    on the same row. The status predicate has to be evaluated in Python
    rather than in the WHERE clause: a WHERE that also filters on
    ``status IN (...)`` locks nothing when the agent is currently inactive,
    so a concurrent activation of that same row is free to commit and slip
    past this guard entirely. Locking by owner instead means every delete
    attempt takes the row lock regardless of its current status, which is
    what actually blocks the race.

    ``for_update=False`` is for read-only callers (the GET confirmation page)
    that must not hold a row lock outside a delete transaction.

    Locking only the ``agents`` row is not enough --
    when the user owns no agent yet, that SELECT returns no rows, so it locks
    nothing. A concurrent self-service signup inserting a brand-new ``agents``
    row with ``user_id`` pointing at this same user (the FK the delete is
    trying to protect) can still commit between this guard's read and the
    caller's ``DELETE``. Locking the ``users`` row itself makes that INSERT's
    FK reference block on (or fail against) this transaction instead of
    racing it.
    """
    query = select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    agent = result.scalar_one_or_none()
    if for_update:
        await db.execute(select(User).where(User.id == user.id).with_for_update())
    if agent is None or agent.status not in AGENT_STATUSES_BLOCKING_ACCOUNT_DELETE:
        return None
    return agent
