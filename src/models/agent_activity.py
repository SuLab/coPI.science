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
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
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
    #: As of 2026-08-22, this counts REAL API CALLS — every tool round and
    #: truncation retry booked via `Agent.record_api_call` /
    #: `SimulationEngine._unbooked_calls` — not turns, which is what it counted
    #: before that date. It is NOT comparable across the boundary: 78.6% of
    #: stored `thread_reply` rows are 2+ calls, so the number roughly doubles for
    #: reasons that have nothing to do with the run. The old per-turn figure is
    #: still recoverable for any run as `SELECT COUNT(*) FROM llm_call_logs
    #: WHERE simulation_run_id = <run>` — one `LlmCallLog` row is written per
    #: turn regardless of how many real API calls that turn made. See
    #: `src/agent/main.py`'s `API_CALL_UNITS_NOTE` (surfaced in the startup
    #: banner) and the comment above `RUN_STATS_UPDATE_INTERVAL` in
    #: `src/agent/simulation.py` for the engine-side half of this note.
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
    )  # denormalized from agent_channels.visibility; see specs/privacy-and-channel-visibility.md §G1/G2
    # Content columns (DB is now the primary conversation store, not Slack).
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sender_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    posted_at: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    # Slack mirror mapping (NULL when Slack is off / message is DB-origin).
    slack_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
    slack_channel_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    slack_thread_ts: Mapped[str | None] = mapped_column(String(50), nullable=True)
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
    # PER-TURN CUMULATIVE, not per-API-call: one row is 1..8 real API calls at
    # the default `max_tool_rounds=5` (up to max_tool_rounds + 1 tool-capable
    # calls, the terminating/forced-final call, and at most one max_tokens
    # retry), and the two token columns are their sums. Correct as billing
    # totals; useless for "which call truncated", which is what call_stats below
    # exists to answer.
    #
    # latency_ms is the exception and is NOT a sum: src/services/llm.py's
    # generate_with_tools ASSIGNS it per call rather than accumulating it, so a
    # multi-round row carries the last call's latency plus any retry's, not the
    # turn's wall time. Read per-call latency out of call_stats, and the turn's
    # wall time out of `wall_ms` below (0035 — before that column the turn's
    # total really was stored nowhere). Do NOT "fix" latency_ms by summing it —
    # the numbers already in the table would then mean two different things
    # depending on when they were written.
    #
    # Do NOT split this table one row per API call either. THE ORIGINAL REASON
    # NO LONGER HOLDS and must not be quoted: this comment used to say the
    # restart rebuild counted ROWS while live booking counted TURNS, so a split
    # would inflate both rebuilt ledgers. As of 68e35c6 neither half is true —
    # `SimulationEngine._rebuild_state_from_db` sums
    # `COALESCE(jsonb_array_length(call_stats), 1)` (simulation.py's
    # `_CALLS_PER_LOG_ROW`) for BOTH api_call_count and the limiter's
    # call_times, and live booking counts real API calls (`record_api_call`
    # plus `_unbooked_calls` for the tool rounds). That expression is 1 for a
    # single-call row, so a split corpus would in fact rebuild correctly.
    #
    # The prohibition stands anyway, on three different grounds:
    #   * The rows already stored are TURNS and cannot be re-encoded (5,771 as
    #     of 2026-08-22, 4,650 of them without call_stats at all). After a split
    #     every column here means one thing on an old row and another on a new
    #     one — the same objection the latency_ms paragraph above makes — and
    #     the admin LLM-calls page reads these columns PER ROW, not as a SUM.
    #   * system_prompt/messages_json/response_text belong to the turn, not to a
    #     call. A row per call would duplicate the largest column in the table
    #     (system_prompt — assessment_detail._load_tool_turns declines to SELECT
    #     it for exactly that reason) once per round, byte-identical each time.
    #   * call_stats already IS the per-call table, denormalized into the row;
    #     that is what 0032 added it for. `jsonb_array_elements` answers the
    #     per-call questions today, so a split buys nothing that is not already
    #     available.
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: The TURN's wall time, which `latency_ms` above deliberately is not. Added
    #: in 0035 as a new column rather than by redefining `latency_ms`, for exactly
    #: the reason the comment above gives: the values already in that column would
    #: otherwise mean two things depending on when they were written. Measured on
    #: run 8b64a0e0, `latency_ms` equalled the LAST call's latency in 532 of 532
    #: rows, so summing the column understated true LLM wait by 25% (215.9 min
    #: stored against 289.4 min actual). NULL on rows written before 0035.
    wall_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The input tokens Anthropic bills SEPARATELY from `input_tokens` above.
    #: `usage.input_tokens` EXCLUDES anything served from or written to the prompt
    #: cache, so on a cached turn it counts only the uncached tail: 109 of 141 live
    #: rows recorded fewer input tokens than the system prompt alone can account
    #: for, and one recorded **2** for a 30 KB prompt. `cache_read` is a cache hit
    #: (billed at a discount), `cache_creation` is a cache write (billed at a
    #: premium); they are separate columns because they are separately priced, so
    #: collapsing them would make cost unrecoverable from the row.
    #:
    #: Added in 0036 as NEW columns rather than as a correction to `input_tokens`,
    #: for exactly the reason `latency_ms` and `wall_ms` are separate above: fold
    #: them into an existing column and the numbers already in it mean two
    #: different things depending on when they were written.
    #: **Billable input volume for a turn is the SUM of the three** — that is the
    #: reader's job, not this table's.
    #:
    #: NULL means "not recorded": a row written before 0036, or one whose SDK
    #: `usage` carried no cache fields at all (they are `Optional[int]` and arrive
    #: as `None` on a non-cached request). NULL is NOT zero — a `0` here is a
    #: measured cache miss, and the two must stay distinguishable or "is prompt
    #: caching working?" cannot be answered from this table.
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # One object per real API call, in call order:
    #   {seq, kind, max_tokens, input_tokens, output_tokens, thinking_tokens,
    #    stop_reason, latency_ms}
    # kind ∈ round | final | forced_final | retry. Written by
    # src/services/llm.py's _call_stat; NULL on every row logged before 0032,
    # and on any row whose producer did not supply it.
    #
    # JSONB (not the `JSON` this table's messages_json uses) so it is queryable:
    # `jsonb_array_elements(call_stats)` is what turns "which calls truncated"
    # from a text-scraping exercise into SQL. `none_as_null=True` for the reason
    # migration 0031 documents at length: SQLAlchemy's JSON default writes Python
    # None as the JSONB scalar `null`, which is a SECOND physical encoding of
    # "absent" that `WHERE call_stats IS NULL` silently misses.
    call_stats: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
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
        # The badge middleware counts proposals per agent on every
        # authenticated page load; measured ~129x with these (issue #25 P1).
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
    #: The ROLE of whoever's reply ended the interview — `scout_hub`, `pi_lab`, or
    #: NULL for a close nobody's reply triggered (the `max_thread_messages`
    #: timeout). `_check_thread_outcome` tests for ⏸️ on whichever agent just
    #: replied, and ⏸️ is an explicit instruction to BOTH roles, so a lab bot
    #: declining its own pitch closes the hub's screen. Seven interviews ended
    #: that way on run 8b64a0e0, none of them with a verdict, and they were
    #: indistinguishable here from the one genuine timeout — which is what made
    #: the first count of them wrong. Recording the role changes no behaviour;
    #: whether a lab's ⏸️ should end the screen at all is a prompt question.
    closed_by_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
    # CASCADE, not SET NULL (migration 0036). The CheckConstraint above requires
    # exactly one owner, and on a human-member row `agent_id` is already NULL — so
    # SET NULL made the cascade's own UPDATE violate that CHECK, and ANY user
    # delete for a private-channel member raised `pcm_exactly_one_of_agent_or_user`
    # (reproduced; both POST /profile/delete-account and the admin delete 500'd).
    # SET NULL was never coherent here: the row's entire content is the member it
    # names, so a row with no owner is unrepresentable rather than merely
    # degraded. `added_by_user_id` below stays SET NULL — nulling the adder
    # violates nothing and the membership is still meaningful without them.
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
    here (the DB is the primary store, not Slack).

    KEPT per the removal cycle's decision 5 (private-instructions + PI-interaction
    removal, 2026-08-12): the model/table stay, but the engine-side pollers and
    handler that used to ingest inbound rows and act on them
    (SimulationEngine._poll_pi_dms_from_db, _poll_pi_dms, _seed_pi_dm_cursor,
    src/agent/pi_handler.py) are gone. A row written here today (e.g. via the
    web dashboard's DM form, src/routers/agent_page.py) is durable history only
    — nothing in the running simulation reads it. See
    specs/local-db-conversations.md.
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
