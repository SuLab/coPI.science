"""One authorship-claim check (migration 0070; the 2026-10-15 publication-handling design,
§11 R6.8, contract C7).

Every check the engine makes is a row, ``no_claim`` included, because the rows are the
owner's denominators when deciding whether to switch enforcement on. ``thread_key`` is
the canonical thread id of a reply, ``"phase5"`` for a pitch, or ``"memory:public"`` /
``"memory:<channel_id>"`` for a memory sweep; ``phase`` is ``thread_reply``, ``new_post``
or ``memory``. ``reasons`` is a JSON array of ``{"code", "text"}``. ``replay`` is admitted
by the CHECK but written by nothing: the replay script writes a report only.

No relationship is declared on ``SimulationRun``: deleting a run removes its rows through
the database's ON DELETE CASCADE, as ``help_intro_dismissals`` relies on for users.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

MODE_LOG = "log"
MODE_ENFORCE = "enforce"
MODE_REPLAY = "replay"
MODES = (MODE_LOG, MODE_ENFORCE, MODE_REPLAY)

OUTCOME_NO_CLAIM = "no_claim"
OUTCOME_GROUNDED = "grounded"
OUTCOME_REJECTED = "rejected"
OUTCOMES = (OUTCOME_NO_CLAIM, OUTCOME_GROUNDED, OUTCOME_REJECTED)

EXCERPT_MAX_CHARS = 2000


class AuthorshipClaimCheck(Base):
    __tablename__ = "authorship_claim_checks"
    __table_args__ = (
        CheckConstraint("mode IN ('log', 'enforce', 'replay')", name="ck_authorship_claim_checks_mode"),
        CheckConstraint(
            "outcome IN ('no_claim', 'grounded', 'rejected')", name="ck_authorship_claim_checks_outcome"
        ),
        CheckConstraint(
            "char_length(excerpt) <= 2000", name="ck_authorship_claim_checks_excerpt_length"
        ),
        # Led by simulation_run_id: serves the run cascade and the per-run review SQL
        # (tests/unit/test_run_scoped_tables.py).
        Index("ix_authorship_claim_checks_run_agent", "simulation_run_id", "agent_id"),
        Index("ix_authorship_claim_checks_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL is admitted for a replay row (C7); the engine always writes its run id.
    simulation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=True
    )
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    thread_key: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
