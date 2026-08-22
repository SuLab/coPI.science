"""Mute/unmute a PI's agent — a purpose-built control over the existing
'active'/'inactive' status axis (design decisions D2-D4), not a new status
value. pending/suspended agents are admin-only concerns and are left alone."""
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry

_MUTABLE_STATUSES = ("active", "inactive")


async def set_agent_mute_state(
    db: AsyncSession, *, agent: AgentRegistry, muted: bool, actor_user_id: uuid.UUID,
) -> bool:
    """Returns False (no-op) if agent.status isn't active/inactive; otherwise
    flips status + attribution and commits, returning True."""
    if agent.status not in _MUTABLE_STATUSES:
        return False

    if muted:
        agent.status = "inactive"
        agent.muted_at = datetime.now(UTC)
        agent.muted_by = actor_user_id
    else:
        agent.status = "active"
        agent.muted_at = None
        agent.muted_by = None

    await db.commit()
    return True
