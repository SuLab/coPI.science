"""Write PI-authored messages into the DB inbox (Slack-independent input path).

The agent simulation ingests these rows via SimulationEngine._poll_inbound_from_db,
so a PI can drive their agent with Slack fully off. This is the DB-native
equivalent of the Slack channel-message path. See specs/local-db-conversations.md.
"""

from __future__ import annotations

import uuid

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.ids import mint_local_ts
from src.models import (
    VISIBILITY_COLLAB_PRIVATE,
    AgentChannel,
    AgentMessage,
    PiDmMessage,
    PrivateChannelMember,
    SimulationRun,
)


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


async def pi_may_post_to_channel(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    channel_name: str,
    user_id: uuid.UUID,
    agent_id: str,
) -> bool:
    """Whether this PI may write into this channel.

    Public channels are open to any PI in the run. ``collab_private`` channels are
    not: membership is held in ``private_channel_members`` and is the only thing
    standing between a PI and another pair's conversation on the DB-only path —
    specs/privacy-and-channel-visibility.md delegates this to Slack ACLs, which
    do not exist here. A PI qualifies either in their own right (``user_id``) or
    through their bot (``agent_id``); ``removed_at`` is honoured so revoking
    membership revokes write access.

    Unknown channel names resolve to public (``_resolve_channel``'s documented
    fallback), so they are allowed and land in a ``local:`` channel — the same
    behaviour as before this check existed.
    """
    row = (await db.execute(
        select(AgentChannel.id, AgentChannel.visibility)
        .where(
            AgentChannel.simulation_run_id == run_id,
            AgentChannel.channel_name == channel_name,
        )
        .limit(1)
    )).first()
    if not row:
        return True
    channel_pk, visibility = row
    if visibility != VISIBILITY_COLLAB_PRIVATE:
        return True

    member = (await db.execute(
        select(PrivateChannelMember.id)
        .where(
            PrivateChannelMember.agent_channel_id == channel_pk,
            PrivateChannelMember.removed_at.is_(None),
            or_(
                PrivateChannelMember.user_id == user_id,
                PrivateChannelMember.agent_id == agent_id,
            ),
        )
        .limit(1)
    )).first()
    return member is not None


async def pi_may_reply_in_thread(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    channel_name: str,
    thread_ts: str,
    agent_id: str,
) -> bool:
    """Whether this PI may address ``thread_ts`` on behalf of ``agent_id``.

    The engine treats a PI message in a thread as authoritative for that thread
    (proposal-review clear, reopen, pi_context), so a free-form ``thread_ts`` from
    the web form must name a thread the PI's own agent actually participates in.
    Requires (a) the thread root to exist in this run and channel and (b) at least
    one message in the thread from ``agent_id``. Unknown threads are refused.

    PI-authored rows carry ``agent_id=NULL`` (``record_pi_message``), so a thread in
    which only the PI has spoken is refused today; that is unreachable via the web
    UI, which always submits ``thread_ts=""``, but a future UI wiring would need the
    participant clause to also accept ``is_bot IS FALSE`` rows whose
    ``sender_name == f"{pi_name} (PI)"``. The participant clause is deliberately
    ts-only (no ``sender_name``/user check) to mirror how ``MessageLog.get_thread_history``
    resolves a thread's membership, but IS scoped to ``channel_name`` (SEC-F5, opus
    review, audit 2026-09-08) — the same scoping the root-existence check above
    uses — so an agent_id that happens to share this ``thread_ts`` value via an
    unrelated thread in a DIFFERENT channel cannot authorize a reply here.
    """
    root = (await db.execute(
        select(AgentMessage.id)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.channel_name == channel_name,
            AgentMessage.message_ts == thread_ts,
        )
        .limit(1)
    )).first()
    if root is None:
        return False
    participant = (await db.execute(
        select(AgentMessage.id)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.channel_name == channel_name,
            AgentMessage.agent_id == agent_id,
            or_(AgentMessage.message_ts == thread_ts, AgentMessage.thread_ts == thread_ts),
        )
        .limit(1)
    )).first()
    return participant is not None


async def record_pi_message(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    channel_name: str,
    content: str,
    sender_name: str,
    sender_user_id: uuid.UUID | None,
    thread_ts: str | None = None,
) -> AgentMessage:
    """Insert a human/PI message (is_bot=False) into agent_messages.

    The engine's inbound poller picks it up on its next tick, appends it to the
    live MessageLog, and routes it through PI handling (proposal-review clear,
    thread reopen, pi_context, @bot tags). Does not commit — the caller owns the
    transaction, and owns the durability of this row with it: it is the only copy
    of what the PI wrote, so a caller that rolls back after calling this loses the
    guidance silently and must re-create the row in its recovery arm. Measured
    open on one caller (``reopen_proposal``'s legacy Slack-off branch, whose lost-
    race arm re-binds ``refined_in_channel`` but not this row — #24 V5 iii).

    Committing here instead would be wrong: the e-mail twin
    (``email_inbound._handle_instruction``) depends on this row riding the same
    commit that retires the notification, so an early commit would let a retried
    S3 delivery write a second guidance row (the #21 COR-19.6 shape).

    ``sender_user_id`` is required (though ``None`` is an accepted value) so no
    caller can silently omit it: it is the ownership carrier
    ``SimulationEngine._handle_pi_inbound_entry`` uses to decide which agent(s)
    this message is allowed to act on (RC-1 / #20 COR-5) — a row written with
    ``None`` gets no ownership-gated side effect at all, only a logged warning,
    so every real caller must pass the acting user's id.

    ``pi_inbound_state`` is stamped ``'pending'`` at insert time (RC-2): the
    durable marker that tells ``_poll_inbound_from_db`` to fetch this row
    regardless of how far its cursor has advanced, so a message written while
    ``agent-run`` is down is not silently skipped once the row ages past the
    poller's lookback window.
    """
    from src.agent.simulation import PI_INBOUND_PENDING

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
        sender_user_id=sender_user_id,
        is_bot=False,
        posted_at=float(ts),
        pi_inbound_state=PI_INBOUND_PENDING,
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
