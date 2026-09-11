"""External-enrichment tables: NIH RePORTER grants, industry evidence, industry score.

The score tables are deliberately NOT imported by profile_export, thread_guidance,
tools.py or simulation.py — tests/unit/test_enrichment_isolation.py enforces it.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

GRANT_TENURE_MODES = ("org_and_year", "org_only")
EVIDENCE_KINDS = ("coauthor_company", "company_funder", "coi_relationship", "patent_filed",
                  "patent_assigned", "trial_industry_collab", "sbir_sttr")
COMPANY_CLASSES = ("pharma_biotech", "device_dx", "cro_vendor", "other", "unknown")


class PiGrant(Base):
    __tablename__ = "pi_grants"
    __table_args__ = (UniqueConstraint("user_id", "core_project_num", name="uq_pi_grants_user_core"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="nih_reporter")
    core_project_num: Mapped[str] = mapped_column(String(40), nullable=False)
    reporter_profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    phr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    agency_ic: Mapped[str | None] = mapped_column(String(20), nullable=True)
    funding_mechanism: Mapped[str | None] = mapped_column(String(40), nullable=True)
    org_name: Mapped[str] = mapped_column(String(200), nullable=False)
    first_fy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_fy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_award_in_tenure: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_contact_pi: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_subproject: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tenure_filter_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    identity_evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    vetoed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PiIndustryEvidence(Base):
    __tablename__ = "pi_industry_evidence"
    __table_args__ = (UniqueConstraint("user_id", "source", "kind", "external_id", name="uq_pi_industry_evidence_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)      # openalex|pubmed|uspto|ctgov|nih_reporter
    kind: Mapped[str] = mapped_column(String(30), nullable=False)        # EVIDENCE_KINDS
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)  # work id / pmid+company / app no / NCT
    company_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    company_external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)  # OpenAlex I… or ROR
    company_class: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pi_role: Mapped[str | None] = mapped_column(String(30), nullable=True)  # first|last|corresponding|middle|inventor|overall_official
    in_tenure: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    vetoed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vetoed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PiIndustryScore(Base):
    __tablename__ = "pi_industry_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)          # 0..100, NULL = unscored
    raw_sum: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)      # no_tenure_start | no_evidence | ok
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    field_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    primary_field: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tenure_start_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scorer_version: Mapped[str] = mapped_column(String(20), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
