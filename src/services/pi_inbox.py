"""Write PI-authored messages into the DB inbox (Slack-independent input path).

The agent simulation ingests these rows via SimulationEngine._poll_inbound_from_db,
so a PI can drive their agent with Slack fully off. This is the DB-native
equivalent of the Slack channel-message path. See specs/local-db-conversations.md.
"""

from __future__ import annotations

import uuid

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.ids import mint_local_ts
from src.models import AgentChannel, AgentMessage, PiDmMessage, SimulationRun


async def get_latest_run_id(db: AsyncSession) -> uuid.UUID | None:
    """Return the most recent SimulationRun id, or None if there are no runs."""
    return (await db.execute(
        select(SimulationRun.id).order_by(desc(SimulationRun.started_at)).limit(1)
    )).scalar_one_or_none()


async def _resolve_channel(db: AsyncSession, run_id: uuid.UUID, channel_name: str) -> tuple[str, str]:
    """Return (channel_id, visibility) for a channel name in a run.

    Falls back to a local: id / public visibility when the channel has no
    agent_channels row yet (e.g. a seeded channel not persisted on an old run).
    """
    row = (await db.execute(
        select(AgentChannel.channel_id, AgentChannel.visibility)
        .where(
            AgentChannel.simulation_run_id == run_id,
            AgentChannel.channel_name == channel_name,
        )
        .limit(1)
    )).first()
    if row:
        return row[0], row[1]
    return f"local:{channel_name}", "public"


async def record_pi_message(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    channel_name: str,
    content: str,
    sender_name: str,
    thread_ts: str | None = None,
) -> AgentMessage:
    """Insert a human/PI message (is_bot=False) into agent_messages.

    The engine's inbound poller picks it up on its next tick, appends it to the
    live MessageLog, and routes it through PI handling (proposal-review clear,
    thread reopen, pi_context, @bot tags). Does not commit — the caller owns the
    transaction.
    """
    channel_id, visibility = await _resolve_channel(db, run_id, channel_name)
    ts = mint_local_ts()
    msg = AgentMessage(
        simulation_run_id=run_id,
        agent_id=None,               # human/PI sender
        channel_id=channel_id,
        channel_name=channel_name,
        message_ts=ts,
        thread_ts=thread_ts,
        phase="thread_reply" if thread_ts else "new_post",
        visibility=visibility,
        content=content,
        sender_name=sender_name,
        is_bot=False,
        posted_at=float(ts),
    )
    db.add(msg)
    return msg


async def record_pi_dm(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    agent_id: str,
    pi_user_id: str,
    direction: str,               # 'inbound' (PI→bot) or 'outbound' (bot→PI)
    content: str,
    sender_name: str = "",
    slack_ts: str | None = None,
) -> PiDmMessage:
    """Persist a PI<->bot direct message. Does not commit."""
    ts = mint_local_ts()
    dm = PiDmMessage(
        simulation_run_id=run_id,
        agent_id=agent_id,
        pi_user_id=pi_user_id,
        direction=direction,
        content=content,
        sender_name=sender_name,
        ts=ts,
        slack_ts=slack_ts,
        posted_at=float(ts),
    )
    db.add(dm)
    return dm


def web_pi_user_id(user_id: uuid.UUID) -> str:
    """Stable pi_user_id for a web (Slack-off) PI: ``local:<users.id>``."""
    return f"local:{user_id}"
