"""Account-type changes (spec 2026-10-05 §6.4, D25): the one rule the admin role route and
``cli role:set`` both apply."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, User
from src.models.user import PI_SURFACE_ROLES
from src.services.session_epoch import bump_session_epoch

#: Agent statuses that keep a lab owned for the role rule. A rejected request is
#: ``suspended`` (``admin_reject_agent``), which does not block.
OWNED_AGENT_STATUSES = ("active", "pending", "inactive")


class RoleChangeRefused(ValueError):
    """The new role may not use the PI surfaces while the user still owns a lab agent."""


async def change_user_role(db: AsyncSession, user: User, new_role: str) -> bool:
    """Set ``user.user_role`` to ``new_role``; bump the session epoch when it changed.
    Returns whether it changed.

    Raises ``RoleChangeRefused`` (nothing written) when ``user``'s current role is in
    ``PI_SURFACE_ROLES``, ``new_role`` is not, and the user owns an agent whose status is in
    ``OWNED_AGENT_STATUSES``; that agent row is read ``FOR UPDATE``. ``new_role`` must
    already be one of ``VALID_USER_ROLES``. The caller keeps its own guards (the last admin,
    an admin's own role) and commits."""
    previous = user.user_role
    if previous in PI_SURFACE_ROLES and new_role not in PI_SURFACE_ROLES:
        owned = (await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.status)
            .where(AgentRegistry.user_id == user.id,
                   AgentRegistry.status.in_(OWNED_AGENT_STATUSES))
            .with_for_update()
        )).first()
        if owned is not None:
            raise RoleChangeRefused(
                f"{user.name} owns the lab agent {owned.agent_id} ({owned.status}); suspend it "
                f"on /admin/agents before making this account a {new_role}."
            )
    user.user_role = new_role
    if previous == new_role:
        return False
    await bump_session_epoch(db, user.id)
    return True
