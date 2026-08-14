"""Profile revision tracking service.

Records every change to public profiles, private profiles, and working memory
with full attribution (who, how, when).
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.profile_revision import ProfileRevision

logger = logging.getLogger(__name__)


async def latest_revision(
    db: AsyncSession,
    *,
    agent_registry_id: uuid.UUID,
    profile_type: str,
) -> ProfileRevision | None:
    """The newest revision for one (agent, profile_type), or None if there is none.

    Backed by ``ix_profile_revision_agent_type_created``, which is already ordered
    ``created_at DESC``.

    Note on ties: ``created_at`` defaults to ``now()``, which in Postgres is the
    *transaction* timestamp, so two revisions for the same key written inside one
    transaction share it and "newest" is then arbitrary between them. Every caller
    writes at most one revision per key per transaction, so this does not arise in
    practice; a caller that needs to write several must commit between them.
    """
    return (
        await db.execute(
            select(ProfileRevision)
            .where(
                ProfileRevision.agent_registry_id == agent_registry_id,
                ProfileRevision.profile_type == profile_type,
            )
            .order_by(ProfileRevision.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_revision(
    db: AsyncSession,
    *,
    agent_registry_id: uuid.UUID,
    profile_type: str,
    content: str,
    changed_by_user_id: uuid.UUID | None = None,
    mechanism: str,
    change_summary: str | None = None,
) -> ProfileRevision:
    """Create a profile revision record, unless it would repeat the previous one.

    Args:
        db: Database session.
        agent_registry_id: The AgentRegistry UUID.
        profile_type: One of "public", "private", "memory".
        content: Full markdown content after the change.
        changed_by_user_id: The user who initiated the change (None for agent/system).
        mechanism: One of "web", "slack_dm", "agent", "pipeline", "monthly_refresh".
        change_summary: Optional short description of what changed.

    Returns:
        The created ProfileRevision, or the existing newest one when ``content`` is
        byte-identical to it — see below.
    """
    # A revision whose content repeats its predecessor's is not history, it is noise:
    # it records that something was written, not that anything changed. This appended
    # unconditionally, so re-running `backfill-profile-revisions` doubled every row
    # with a byte-identical twin. Keyed on content alone, deliberately — a differing
    # `mechanism` or `change_summary` over the same body is still the same profile,
    # and the point of the history is what the profile said.
    #
    # Only the *newest* revision is compared, so a genuine edit always lands, and so
    # does a revert back to an older body. See the two halves pinned in
    # tests/integration/test_cli.py: test_backfill_run_twice_does_not_duplicate_any_
    # revision and test_a_changed_profile_body_still_creates_a_new_revision.
    previous = await latest_revision(
        db, agent_registry_id=agent_registry_id, profile_type=profile_type
    )
    if previous is not None and previous.content == content:
        logger.debug(
            "Skipped unchanged %s profile revision for agent %s (via %s)",
            profile_type, agent_registry_id, mechanism,
        )
        return previous

    revision = ProfileRevision(
        agent_registry_id=agent_registry_id,
        profile_type=profile_type,
        content=content,
        changed_by_user_id=changed_by_user_id,
        mechanism=mechanism,
        change_summary=change_summary,
    )
    db.add(revision)
    await db.flush()
    logger.debug(
        "Created %s profile revision for agent %s via %s",
        profile_type, agent_registry_id, mechanism,
    )
    return revision


async def get_revision_history(
    db: AsyncSession,
    *,
    agent_registry_id: uuid.UUID,
    profile_type: str,
    limit: int = 50,
) -> list[ProfileRevision]:
    """Get revision history for a profile, most recent first.

    Args:
        db: Database session.
        agent_registry_id: The AgentRegistry UUID.
        profile_type: One of "public", "private", "memory".
        limit: Maximum number of revisions to return.

    Returns:
        List of ProfileRevision ordered by created_at descending.
    """
    result = await db.execute(
        select(ProfileRevision)
        .where(
            ProfileRevision.agent_registry_id == agent_registry_id,
            ProfileRevision.profile_type == profile_type,
        )
        .order_by(ProfileRevision.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
