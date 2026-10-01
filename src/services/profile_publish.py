"""Export a PI's public profile and record the revision, in one place (RB-09).

The caller passes the publications, so each path keeps its own query and order
(FA3-V3): the pipeline's unordered load scoped with its run-local tenure year, the
edit paths' `scoped_publications_for_export` (ORDER BY year DESC NULLS LAST). The
export's stable sort and top-20 cut then see the same sequence as before.

No revision is written when the export returns None (no agent, or the write
failed). Before this helper the public-profile save wrote an empty-content
revision on a failed write; that failure path now writes none. Flushes, never
commits: the caller owns the transaction."""
from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, ResearcherProfile, User
from src.services.profile_export import export_profile_to_markdown
from src.services.profile_versioning import create_revision
from src.services.tenure_scope import TenureScopedPublications


async def export_and_record(
    db: AsyncSession, *, user: User, profile: ResearcherProfile, agent: AgentRegistry | None,
    publications: TenureScopedPublications | None, mechanism: str | None,
    changed_by_user_id: uuid.UUID | None = None, change_summary: str | None = None,
) -> Path | None:
    """Export ``profile`` to ``profiles/public/<agent_id>.md``; when that returns a
    path AND ``agent`` and ``mechanism`` are given, record a ``public`` revision
    of the exported text. ``mechanism=None`` is export only (the veto re-export)."""
    path = export_profile_to_markdown(
        user, profile, agent.agent_id if agent else None, publications=publications
    )
    if path is not None and agent is not None and mechanism is not None:
        await create_revision(
            db, agent_registry_id=agent.id, profile_type="public",
            content=path.read_text(encoding="utf-8"),
            changed_by_user_id=changed_by_user_id, mechanism=mechanism,
            change_summary=change_summary,
        )
    return path
