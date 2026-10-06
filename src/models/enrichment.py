"""External-enrichment tables: NIH RePORTER grants and ORCID fundings, industry evidence,
industry score.

Nothing that builds profile text, a prompt, a tool result, the assessment chat or the
review bot may reach the two industry tables, directly or through a helper module:
tests/unit/test_enrichment_isolation.py walks the imports transitively.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

GRANT_TENURE_MODES = ("org_and_year", "org_only")
EVIDENCE_KINDS = ("coauthor_company", "company_funder", "coi_relationship", "patent_filed",
                  "patent_assigned", "trial_industry_collab", "sbir_sttr")
COMPANY_CLASSES = ("pharma_biotech", "device_dx", "cro_vendor", "other", "unknown")
GRANT_IDENTITY_STATUSES = (
    "resolved", "held", "unconfirmed", "no_match", "firehose", "pinned", "none_confirmed",
)
#: The identity statuses whose RePORTER rows reach the persona (spec 2026-10-05 §6.1, D9).
GRANT_RENDERING_STATUSES = ("resolved", "pinned")
_STATUS_SQL = "(" + ", ".join(f"'{s}'" for s in GRANT_IDENTITY_STATUSES) + ")"


class PiGrant(Base):
    __tablename__ = "pi_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "core_project_num", name="uq_pi_grants_user_core"),
        Index("ix_pi_grants_vetoed_by_user_id", "vetoed_by_user_id"),
    )

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
    #: Who vetoed the row (migration 0060). NULL on rows vetoed before 0060: never backfilled.
    vetoed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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
    #: Written by SCORER_VERSION 1.0.0 rows only. From 2.0.0 the percentile and the reason
    #: are computed when a page renders (src/services/directory.py `industry_views`; spec
    #: 2026-10-05 §6.2, D13). Kept, never dropped (D34).
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_sum: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Written by 1.0.0 rows only (see `score`).
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Never written (D34).
    field_percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Written by 1.0.0 rows only (D34, E-14).
    primary_field: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tenure_start_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scorer_version: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Set to clock_timestamp() by the writer (src/services/industry_evidence.py), so a long
    #: job's row never sorts before a quicker one's; the server default is now().
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    #: Per evidence source, what the run that wrote this row covered (migration 0061, spec
    #: 2026-10-05 §6.2): "ok", "truncated" (paging stopped at its cap) or
    #: "unavailable:<reason>". NULL on rows written before 0061. The manager pages show
    #: "partial" when any value is not "ok".
    coverage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


class PiOrcidFunding(Base):
    """One ORCID funding GROUP per row (spec 2026-10-05 §6.1): the summary with the lowest
    display-index supplies the fields. `group_key` is the group's grant identifier when it
    has one, else a hash of normalised title and funder (src/services/orcid_fundings.py).
    Refreshes upsert on (user_id, group_key) and never touch vetoed_*."""

    __tablename__ = "pi_orcid_fundings"
    __table_args__ = (
        UniqueConstraint("user_id", "group_key", name="uq_pi_orcid_fundings_user_group"),
        Index("ix_pi_orcid_fundings_vetoed_by_user_id", "vetoed_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    group_key: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    funder_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    funding_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: [{"type", "value", "relationship"}], as ORCID listed them.
    external_ids: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    vetoed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vetoed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PiGrantIdentity(Base):
    """The PI's NIH RePORTER identity (spec 2026-10-05 §6.1), one row per PI.

    `status` NULL means RePORTER has not been evaluated since the row was created or
    since staff removed a pin. The enrich_grants job writes status, accepted ids,
    candidates and evaluated_at; it never writes the staff columns (pinned_*,
    none_confirmed), from which it derives `pinned` / `none_confirmed`."""

    __tablename__ = "pi_grant_identity"
    __table_args__ = (
        CheckConstraint(f"status IS NULL OR status IN {_STATUS_SQL}", name="ck_pi_grant_identity_status"),
        Index("ix_pi_grant_identity_pinned_by_user_id", "pinned_by_user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    accepted_profile_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    #: [{"id", "name_on_award", "linked", "linking_pmids"}]
    candidates: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    pinned_profile_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    none_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    pinned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    orcid_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
