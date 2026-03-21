"""Channel management — creation, archival, and lifecycle tracking."""

import logging
import re
import uuid
from datetime import datetime, timezone

from src.models import AgentChannel

logger = logging.getLogger(__name__)

# Default seeded channels that exist at workspace setup
SEEDED_CHANNELS = [
    "general",
    "drug-repurposing",
    "structural-biology",
    "aging-and-longevity",
    "single-cell-omics",
    "chemical-biology",
]


def normalize_channel_name(name: str) -> str:
    """Normalize a channel name to Slack's requirements (lowercase, hyphens, max 80 chars)."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip("-")
    return name[:80]


def make_collaboration_channel_name(lab_ids: list[str], topic: str = "") -> str:
    """Generate a collaboration channel name for two or more labs."""
    labs = "-".join(sorted(lab_ids))
    if topic:
        topic_slug = normalize_channel_name(topic)[:30]
        return f"collab-{labs}-{topic_slug}"
    return f"collab-{labs}"


def is_seeded_channel(channel_name: str) -> bool:
    """Check if a channel is one of the seeded (pre-existing) channels."""
    return channel_name in SEEDED_CHANNELS


async def record_channel_created(
    db,
    simulation_run_id: uuid.UUID,
    channel_id: str,
    channel_name: str,
    channel_type: str,
    created_by_agent: str,
) -> AgentChannel:
    """Record a newly created channel in the database."""
    record = AgentChannel(
        simulation_run_id=simulation_run_id,
        channel_id=channel_id,
        channel_name=channel_name,
        channel_type=channel_type,
        created_by_agent=created_by_agent,
    )
    db.add(record)
    await db.flush()
    return record


async def record_channel_archived(
    db,
    simulation_run_id: uuid.UUID,
    channel_id: str,
) -> None:
    """Record that a channel was archived."""
    from sqlalchemy import select
    result = await db.execute(
        select(AgentChannel).where(
            AgentChannel.simulation_run_id == simulation_run_id,
            AgentChannel.channel_id == channel_id,
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.archived_at = datetime.now(timezone.utc)
        await db.flush()
