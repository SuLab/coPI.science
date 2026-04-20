"""Agent activity models: SimulationRun, AgentMessage, AgentChannel, LlmCallLog, ThreadDecision, PrivateChannelMember."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


# Channel visibility classes. See specs/privacy-and-channel-visibility.md.
# 'public' — all bots and PIs; seeded and agent-created thematic channels.
# 'collab_private' — 2 bots + up to 2 PIs; Slack is_private=true.
VISIBILITY_PUBLIC = "public"
VISIBILITY_COLLAB_PRIVATE = "collab_private"


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
        "AgentMessage", back_populates="simulation_run", cascade="all, delete-orphan"
    )
    channels: Mapped[list["AgentChannel"]] = relationship(
        "AgentChannel", back_populates="simulation_run", cascade="all, delete-orphan"
    )
    llm_call_logs: Mapped[list["LlmCallLog"]] = relationship(
        "LlmCallLog", back_populates="simulation_run", cascade="all, delete-orphan"
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
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False)
    message_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message_length: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    thread_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phase: Mapped[str] = mapped_column(String(30), nullable=False)  # scan, prune, thread_reply, new_post, etc.
    visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VISIBILITY_PUBLIC,
    )  # denormalized from agent_channels.visibility; see specs/privacy-and-channel-visibility.md §G1/G2
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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
        "PrivateChannelMember", back_populates="agent_channel", cascade="all, delete-orphan"
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
    )  # drives Phase 5 dedup-context filter; see specs/privacy-and-channel-visibility.md §G3
    refined_in_channel: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
    )  # private channel ID if the thread migrated from a public channel via the reopen flow
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ThreadDecision thread={self.thread_id} outcome={self.outcome}>"


class PrivateChannelMember(Base):
    """Authoritative membership for collab_private channels.

    Each row represents either a bot member (agent_id non-null) or a human PI
    member (user_id non-null). See specs/data-model.md §PrivateChannelMember
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
        ForeignKey("users.id", ondelete="SET NULL"),
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
