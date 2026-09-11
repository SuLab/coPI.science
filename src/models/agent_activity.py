"""Agent activity models: SimulationRun, AgentMessage, AgentChannel, LlmCallLog, ThreadDecision, PrivateChannelMember."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


# Channel visibility classes. See specs/privacy-and-channel-visibility.md.
# 'public' — all bots and PIs; seeded and agent-created thematic channels.
# 'collab_private' — 2 bots + up to 2 PIs; Slack is_private=true.
#
# Defined in src/visibility.py (dependency-free) and re-exported here so the
# in-memory message log can use them without importing the ORM, while every
# existing `from src.models.agent_activity import VISIBILITY_*` keeps working.
from src.visibility import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC  # noqa: E402


class SimulationRun(Base):
    __tablename__ = "simulation_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("running", "completed", "stopped", name="sim_run_status_enum"),
        default="running",
        nullable=False,
    )
    total_messages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_api_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Relationships
    messages: Mapped[list["AgentMessage"]] = relationship(
        "AgentMessage", back_populates="simulation_run", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    channels: Mapped[list["AgentChannel"]] = relationship(
        "AgentChannel", back_populates="simulation_run", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    llm_call_logs: Mapped[list["LlmCallLog"]] = relationship(
        "LlmCallLog", back_populates="simulation_run", cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<SimulationRun id={self.id} status={self.status}>"


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable: the sender's agent_id, or NULL for human/PI messages
    # (mirrors LogEntry.sender_agent_id). Every reader filters for a specific
    # agent_id, so NULL rows are naturally excluded. See specs/local-db-conversations.md.
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    channel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Canonical message id: a locally-minted ts-shaped string (Slack-off) or the
    # Slack ts (Slack-on). Unique within a run.
    message_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    thread_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)  # scan, prune, thread_reply, new_post, etc.
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VISIBILITY_PUBLIC,
    )  # denormalized from agent_channels.visibility; see specs/privacy-and-channel-visibility.md
    # Content columns (DB is now the primary conversation store, not Slack).
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sender_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    posted_at: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Slack mirror mapping (NULL when Slack is off / message is DB-origin).
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    slack_channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Inbound-poller handled-marker, written only by
    # SimulationEngine._poll_inbound_from_db and only for is_bot=False rows:
    # 'ingested' (text is in the MessageLog, side effects not yet confirmed) then
    # 'handled' (_handle_pi_inbound_entry returned). NULL means "unknown — no
    # inbound poller has claimed this row", which covers every pre-0029 row and
    # every row another path (the Slack poller, the engine's own append) put in
    # the log; readers must treat NULL as today's behaviour, i.e. dedup on
    # MessageLog presence. See migration 0029.
    pi_inbound_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The PI/user who actually wrote this row — NULL for every bot-authored row
    # and for a pre-0030 human/web row (migration 0030 adds no backfill for
    # existing rows: there is no way to recover who wrote them). This is the
    # ownership carrier that inbound-message handling uses in place of thread membership:
    # SimulationEngine._agent_ids_owned_by_user(sender_user_id) resolves the
    # set of agents this user actually owns (AgentRegistry.user_id, or an
    # AgentDelegate row), and _handle_pi_inbound_entry restricts every
    # ownership-gated side effect (proposal-review clear, reopen, pi_context,
    # @bot tag) to that set — never to "whoever else happens to share this
    # thread". ON DELETE SET NULL rather than CASCADE: deleting a PI's account
    # must not delete the historical record of what was said in a shared
    # thread. See migration 0030.
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("simulation_run_id", "message_ts", name="uq_agent_messages_run_ts"),
        Index("ix_agent_messages_run_posted", "simulation_run_id", "posted_at"),
        # Backs the inbound poller's cursor, which pages over created_at (the DB
        # server's clock) rather than the writer-clock-derived posted_at. See
        # SimulationEngine._poll_inbound_from_db / PI_INBOX_LOOKBACK_S (R3).
        Index("ix_agent_messages_run_created", "simulation_run_id", "created_at"),
        Index(
            "ix_agent_messages_run_channel_posted",
            "simulation_run_id", "channel_name", "posted_at",
        ),
        Index(
            "ix_agent_messages_run_slack_ts",
            "simulation_run_id", "slack_ts",
            postgresql_where=text("slack_ts IS NOT NULL"),
        ),
        Index("ix_agent_messages_sender_user_id", "sender_user_id"),
    )

    # Relationships
    simulation_run: Mapped["SimulationRun"] = relationship(
        "SimulationRun", back_populates="messages"
    )

    def __repr__(self) -> str:
        return f"<AgentMessage id={self.id} agent={self.agent_id} channel={self.channel_name}>"


class AgentChannel(Base):
    __tablename__ = "agent_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_type: Mapped[str] = mapped_column(
        Enum("thematic", "collaboration", name="channel_type_enum"), nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VISIBILITY_PUBLIC,
    )  # 'public' or 'collab_private'; see specs/privacy-and-channel-visibility.md
    created_by_agent: Mapped[str] = mapped_column(String(50), nullable=False)
    migrated_from_channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    simulation_run: Mapped["SimulationRun"] = relationship(
        "SimulationRun", back_populates="channels"
    )
    private_members: Mapped[list["PrivateChannelMember"]] = relationship(
        "PrivateChannelMember", back_populates="agent_channel", cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<AgentChannel id={self.id} name={self.channel_name} visibility={self.visibility}>"


class LlmCallLog(Base):
    __tablename__ = "llm_call_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)  # decide, respond, kickstart, memory
    channel: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    messages_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    simulation_run: Mapped["SimulationRun"] = relationship(
        "SimulationRun", back_populates="llm_call_logs"
    )

    def __repr__(self) -> str:
        return f"<LlmCallLog id={self.id} agent={self.agent_id} phase={self.phase} model={self.model}>"


class ThreadDecision(Base):
    __tablename__ = "thread_decisions"
    __table_args__ = (
        Index("ix_thread_decisions_agent_a_outcome", "agent_a", "outcome"),
        Index("ix_thread_decisions_agent_b_outcome", "agent_b", "outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_id: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_a: Mapped[str] = mapped_column(String(50), nullable=False)
    agent_b: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(
        Enum("proposal", "no_proposal", "timeout", name="thread_outcome_enum"),
        nullable=False,
    )
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VISIBILITY_PUBLIC,
    )  # drives the spontaneous-post dedup-context filter; see specs/privacy-and-channel-visibility.md
    refined_in_channel: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
    )  # private channel ID if the thread migrated from a public channel via the reopen flow
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )  # set when a PI reopens a decided thread (Slack-native or web rating=0 guidance)
    # Durable record that the owning PI engaged with this thread, i.e. that the
    # pending-proposal block was cleared. NULL means "no engagement recorded" —
    # exactly what every pre-0029 row has always meant, so the rebuild's re-block
    # is unchanged for them. This carries the implicit review INSTEAD of a
    # ProposalReview row because proposal_reviews.user_id is ondelete="CASCADE"
    # to users, so deleting a PI would erase the engine's own block-clearing
    # markers. See migration 0029.
    pi_engaged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    def __repr__(self) -> str:
        return f"<ThreadDecision thread={self.thread_id} outcome={self.outcome}>"


class PrivateChannelMember(Base):
    """Authoritative membership for collab_private channels.

    Each row represents either a bot member (agent_id non-null) or a human PI
    member (user_id non-null). See specs/data-model.md
    and specs/privacy-and-channel-visibility.md.
    """

    __tablename__ = "private_channel_members"
    __table_args__ = (
        CheckConstraint(
            "(agent_id IS NULL) != (user_id IS NULL)",
            name="pcm_exactly_one_of_agent_or_user",
        ),
        Index(
            "ix_pcm_channel_agent",
            "agent_channel_id", "agent_id",
            unique=True,
            postgresql_where="agent_id IS NOT NULL",
        ),
        Index(
            "ix_pcm_channel_user",
            "agent_channel_id", "user_id",
            unique=True,
            postgresql_where="user_id IS NOT NULL",
        ),
        Index("ix_private_channel_members_user_id", "user_id"),
        Index("ix_private_channel_members_added_by_user_id", "added_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)  # 'bot', 'pi', 'delegate'
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_channel: Mapped["AgentChannel"] = relationship(
        "AgentChannel", back_populates="private_members"
    )

    def __repr__(self) -> str:
        who = f"agent={self.agent_id}" if self.agent_id else f"user={self.user_id}"
        return f"<PrivateChannelMember channel={self.agent_channel_id} {who} role={self.role}>"


class PiDmMessage(Base):
    """A direct message between a PI (human) and their agent's bot.

    DMs never enter the shared MessageLog, so they get their own durable home
    here (the DB is the primary store, not Slack). Inbound rows (direction=
    'inbound') are written by the Slack DM poller or the PI web interface and
    ingested by SimulationEngine._poll_pi_dms_from_db; outbound rows record
    what the bot sent back. See specs/local-db-conversations.md.
    """

    __tablename__ = "pi_dm_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    # PI identity: Slack user id (Slack-on) or "local:<users.id>" (Slack-off).
    pi_user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(
        Enum("inbound", "outbound", name="pi_dm_direction_enum"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sender_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    ts: Mapped[str] = mapped_column(String(50), nullable=False)  # canonical id
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    posted_at: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Durable at-least-once marker for inbound rows: set once, after
    # SimulationEngine._poll_pi_dms_from_db's PIHandler.handle_dm call returns
    # (successfully or not — one attempt, mirroring agent_messages.pi_inbound_state's
    # INGESTED->HANDLED shape with a single timestamp rather than two states, since a
    # DM has no ordering/idempotency requirement across a crash mid-handler for the
    # extra state to protect). NULL means "not yet processed" — the durable signal
    # that used to be inferred from the in-memory `_pi_dm_seen` set, which a process
    # restart or a down `agent-run` silently reset to empty, losing every side effect
    # of a DM written while the sim was down. Migration 0030 backfills every existing
    # inbound row to its own created_at (already handled or unrecoverable); NULL after
    # that migration means a row no `_poll_pi_dms_from_db` tick has claimed yet.
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_pi_dm_run_agent_posted", "simulation_run_id", "agent_id", "posted_at"),
        Index("ix_pi_dm_run_direction_posted", "simulation_run_id", "direction", "posted_at"),
        # Backs the DM poller's created_at cursor (R3), as above.
        Index("ix_pi_dm_run_direction_created", "simulation_run_id", "direction", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<PiDmMessage agent={self.agent_id} dir={self.direction}>"
