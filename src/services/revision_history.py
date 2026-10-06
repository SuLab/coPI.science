"""The read-only revision history on the manager PI page (spec 2026-10-05 §6.4, D61):
the PI's ``public`` profile revisions, newest first. Staff only (D47); the router decides."""
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, ProfileRevision, User

#: How many revisions the card lists.
HISTORY_LIMIT = 20


@dataclass(frozen=True)
class RevisionRow:
    id: uuid.UUID
    created_at: datetime
    mechanism: str
    actor_name: str | None     # users.name of changed_by_user_id; None for system writes
    change_summary: str | None
    content: str


async def list_public_revisions(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int = HISTORY_LIMIT,
) -> list[RevisionRow]:
    """The newest ``limit`` ``public`` revisions of the PI's agent (``created_at`` then
    ``id`` descending); [] when the PI has no agent. Reads only."""
    rows = (await db.execute(
        select(ProfileRevision, User.name)
        .join(AgentRegistry, AgentRegistry.id == ProfileRevision.agent_registry_id)
        .outerjoin(User, User.id == ProfileRevision.changed_by_user_id)
        .where(AgentRegistry.user_id == user_id, ProfileRevision.profile_type == "public")
        .order_by(ProfileRevision.created_at.desc(), ProfileRevision.id.desc())
        .limit(limit)
    )).all()
    return [
        RevisionRow(id=r.id, created_at=r.created_at, mechanism=r.mechanism, actor_name=name,
                    change_summary=r.change_summary, content=r.content)
        for r, name in rows
    ]
