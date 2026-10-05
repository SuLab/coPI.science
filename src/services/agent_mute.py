"""Mute/unmute a PI's agent: a purpose-built control over the existing
'active'/'inactive' status axis (design decisions D2-D4), not a new status
value. pending/suspended agents are admin-only concerns and are left alone."""
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, User
from src.services.agent_activation import activate_agent
from src.services.slack_tokens import token_for_agent_row
from src.services.star_topology import ensure_lab_spoke

_MUTABLE_STATUSES = ("active", "inactive")


async def set_agent_mute_state(
    db: AsyncSession, *, agent: AgentRegistry, muted: bool, actor: User,
) -> str | None:
    """Mute: active -> inactive with attribution. Unmute: the activation gate
    (activate_agent, no override; the manager surface never overrides) plus the
    Slack-token check, applied as an UPDATE conditional on status still being
    active/inactive, so a concurrent suspend or delete is never overwritten (RA-01).
    An unmute also ensures the lab's hub spoke (``ensure_lab_spoke``) in the same commit.
    Returns None on success (committed), else a refusal code: "agent_not_mutable",
    "no_token", "activation_blocked" or "no_spoke"."""
    if agent.status not in _MUTABLE_STATUSES:
        return "agent_not_mutable"
    # Read now: the unmute path expires `agent` before its UPDATE.
    agent_pk, agent_slug, agent_role = agent.id, agent.agent_id, agent.role
    if muted:
        result = await db.execute(
            update(AgentRegistry)
            .where(AgentRegistry.id == agent_pk, AgentRegistry.status.in_(_MUTABLE_STATUSES))
            .values(status="inactive", muted_at=datetime.now(UTC), muted_by=actor.id)
        )
    else:
        if not token_for_agent_row(agent):
            return "no_token"
        with db.no_autoflush:
            blockers = await activate_agent(db, agent, actor=actor, override=False)
        db.expire(agent)  # discard activate_agent's in-memory flip; the UPDATE below is the write
        if blockers:
            return "activation_blocked"
        result = await db.execute(
            update(AgentRegistry)
            .where(AgentRegistry.id == agent_pk, AgentRegistry.status.in_(_MUTABLE_STATUSES))
            .values(status="active", muted_at=None, muted_by=None)
        )
    if result.rowcount != 1:
        await db.rollback()
        return "agent_not_mutable"
    if not muted and await ensure_lab_spoke(
        db, agent_id=agent_slug, role=agent_role, actor=actor
    ):
        await db.rollback()
        return "no_spoke"
    await db.commit()
    return None
