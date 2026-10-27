"""Control plane for the simulation: explicit one-shot commands, a single
heartbeat/status row, and a generic admin audit trail.

Commands are NEVER desired-state: a `pending` row is a request that exactly
one consumer (the supervisor for `start`, the running engine for `stop`)
claims once and marks done/failed. The supervisor marks all pending rows
`stale` at boot — the never-auto-start invariant — so a reboot can never
replay a request from before the process came up.
"""
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from src.database import Base


class SimulationCommand(Base):
    __tablename__ = "simulation_commands"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    command: Mapped[str] = mapped_column(
        Enum("start", "stop", "system", name="sim_command_enum"), nullable=False
    )
    #: start (claimed by the supervisor, no engine alive): {"fresh": bool, "systems":
    #: {"interviews"|"projects": {"count": int, "runtime_minutes": int}}}; the older
    #: {"fresh", "max_runtime", "max_proposals"} is still read as interviews
    #: (src.agent.sessions.systems_from_start_payload). stop: None for the default Stop
    #: ("Stop everything"), {"hold_open": true} for its hold variant, or the Finalize run
    #: shape. system (migration 0074; claimed by the live engine, staled by the supervisor
    #: when none is alive): {"action": "start"|"wind_down"|"stop_now", "system":
    #: "interviews"|"projects", "run_id": "<uuid>", "limits": {...}}, ``limits`` on a start only.
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("pending", "done", "failed", "stale", name="sim_command_status_enum"),
        nullable=False, server_default="pending",
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Free-text outcome: run id started, error tail, or the stale reason.
    result: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_simulation_commands_pending", "status", "created_at"),
        Index("uq_simulation_commands_one_pending", "command", unique=True, postgresql_where=text("status = 'pending'")),
    )


class SimulationProcessStatus(Base):
    """One row (id=1), upserted: the supervisor writes state transitions, the
    engine overwrites `detail` + `simulation_run_id` on its ~30s poll. The
    page derives 'engine not responding' from `updated_at` staleness."""

    __tablename__ = "simulation_process_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    state: Mapped[str] = mapped_column(String(20), nullable=False, server_default="idle")
    simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    detail: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdminAuditEvent(Base):
    """Append-only audit of admin control actions (start/stop requests,
    announce-setting changes). Deliberately generic — cohort_audit_events is
    cohort-shaped; this is the everything-else trail."""

    __tablename__ = "admin_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
