"""Durable record of one specialist-panel consult.

Before this table the panel existed only in engine memory
(``SimulationEngine._specialist_consults``) and in unlinked ``llm_call_logs``
rows (``phase='consult_<domain>'``, ``channel`` NULL) — so a restart made the
specialist floor unverifiable (the ``missing_domains=[]`` state on
``OpportunityAssessment``), and nothing could show WHO was consulted about an
interview, or what they said.

One row per SUCCESSFUL consult, written from the same success path that fires
``on_consult`` — the callback that satisfies the enforcement floor — so "counts
for the floor" and "is recorded here" can never disagree. A refused domain, a
missing persona file, a failed LLM call, or an empty reply writes nothing:
a row in this table always means the domain counts as consulted.

``verdict_signal``/``confidence`` are the parsed fields of
``src/agent/specialists.py::parse_opinion`` (``blocking``/``caution``/``clear``
and ``high``/``moderate``/``low``, already defaulted upstream on an unreadable
reply — no DB enum, same reasoning as ``OpportunityAssessment.band``).
``raw_opinion`` keeps the reply exactly as the specialist wrote it, so parsing
changes never lose the original.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class SpecialistConsult(Base):
    __tablename__ = "specialist_consults"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    simulation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("simulation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The consulting hub (blackbird) and the PI whose interview prompted the ask.
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_agent_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    channel_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    domain: Mapped[str] = mapped_column(String(20), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    verdict_signal: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    concerns: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    questions_to_ask: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    raw_opinion: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<SpecialistConsult domain={self.domain} signal={self.verdict_signal} "
            f"subject={self.subject_agent_id}>"
        )
