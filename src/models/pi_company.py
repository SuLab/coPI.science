"""The staff Companies list: one row per company a PI founded, co-founded, advises or
sits on the board of (migration 0058; spec 2026-10-02 §7.1).

Deliberately NOT in src/models/enrichment.py. That module's industry tables must never
reach profile text or prompts (tests/unit/test_enrichment_isolation.py), while the
``confirmed`` rows of this table are written to profiles/private/companies/<agent_id>.md
for the hub's ``retrieve_profile`` (src/services/pi_companies.py). Only ``confirmed`` rows
leave the table: ``suggested`` rows wait for a manager, and ``rejected`` rows exist only
so company discovery never suggests the same normalized name again.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

PI_COMPANY_ROLES = ("founder", "co_founder", "board", "advisor")
PI_COMPANY_STATUSES = ("suggested", "confirmed", "rejected")
PI_COMPANY_ORIGINS = ("manual", "discovered")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class PiCompany(Base):
    """One company tie of one PI. Discovery writes ``suggested`` rows; a manager moves
    each to ``confirmed`` or ``rejected``. A manual entry is ``confirmed`` from the start
    (O9)."""

    __tablename__ = "pi_companies"
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_name", name="uq_pi_companies_user_normalized_name"),
        CheckConstraint(f"pi_role IN ({_sql_in(PI_COMPANY_ROLES)})", name="ck_pi_companies_pi_role"),
        CheckConstraint("funding_usd >= 0", name="ck_pi_companies_funding_usd"),
        CheckConstraint(f"status IN ({_sql_in(PI_COMPANY_STATUSES)})", name="ck_pi_companies_status"),
        CheckConstraint(f"origin IN ({_sql_in(PI_COMPANY_ORIGINS)})", name="ck_pi_companies_origin"),
    )
    #: The INSERT returns `created_at` itself, so a flushed row is complete: an expired
    #: server default would lazy-load on first read, which an async session cannot do.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The PI. The rows go with the account.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: ``normalize_company_name(company_name)``: the merge and dedupe key (§7.5), unique per PI.
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pi_role: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Total raised in whole US dollars, as recorded. For a discovered row, the SEC Form D
    #: floor total (O13).
    funding_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: The date the figure is as of (for a Form D floor, the newest filing date).
    funding_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: http(s) only, validated in the service: the link a manager gave, or for a discovered
    #: row the PubMed record of the newest founder statement (else the Wikidata item).
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: The company's EDGAR filings page, while ``funding_usd`` is the Form D floor total.
    funding_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Discovered rows only: the attributed sentences, the Wikidata item and the Form D
    #: filings (§7.1). NULL for a manual entry.
    evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: The effective user, so an admin impersonating a manager records the manager. NULL
    #: for a discovered row.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Who confirmed or rejected it (for a manual entry, its creator).
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<PiCompany {self.company_name!r} user={self.user_id} status={self.status}>"
