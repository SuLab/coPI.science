"""Agent activity models: SimulationRun, AgentMessage, AgentChannel."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


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
    phase: Mapped[str] = mapped_column(
        Enum("decide", "respond", name="agent_message_phase_enum"), nullable=False
    )
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
    created_by_agent: Mapped[str] = mapped_column(String(50), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    simulation_run: Mapped["SimulationRun"] = relationship(
        "SimulationRun", back_populates="channels"
    )

    def __repr__(self) -> str:
        return f"<AgentChannel id={self.id} name={self.channel_name} type={self.channel_type}>"
