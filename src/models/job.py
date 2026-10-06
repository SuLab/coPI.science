"""Job model for PostgreSQL-backed job queue."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

#: The job types that may have at most one pending-or-processing row per user
#: (0056 `uq_jobs_one_active_per_user_type`, rebuilt by 0058 to add
#: `company_discovery`). `review_feedback_analysis` is excluded on purpose: one
#: reviewer press enqueues up to 25 rows (SA3-04).
PER_USER_JOB_TYPES = (
    "generate_profile", "enrich_grants", "industry_evidence", "company_discovery",
)

#: The partial-index predicate, shared by the model, the migration that last built the
#: index (0058) and every `ON CONFLICT (user_id, type) WHERE ...` enqueue (Postgres
#: infers the index only when the conflict predicate matches this text). `job_type_text`
#: is the IMMUTABLE `enum::text` wrapper 0056 creates: a fresh alembic chain cannot use
#: the enum values 0039, 0047 and 0058 add in the same transaction, and a plain cast is
#: not immutable.
ONE_ACTIVE_PER_USER_TYPE_WHERE = (
    "status IN ('pending','processing') AND user_id IS NOT NULL "
    "AND job_type_text(type) IN "
    "('generate_profile','enrich_grants','industry_evidence','company_discovery')"
)

#: `jobs.priority` (0056): higher is claimed first, NULL reads as 0. A person
#: waiting on a page outranks bulk enrichment and regeneration.
INTERACTIVE_PRIORITY = 10
BULK_PRIORITY = -10


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    type: Mapped[str] = mapped_column(
        Enum(
            "generate_profile", "monthly_refresh", "review_feedback_analysis",
            "enrich_grants", "industry_evidence", "company_discovery",
            name="job_type_enum",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "processing", "completed", "failed", "dead", name="job_status_enum"),
        default="pending",
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Earliest time `claim_job` may take this row again (migration 0053). Set
    #: only when a failed job is re-queued for retry: 4 min after the first
    #: failure, 16 min after the second (src/worker/main.py `retry_delay`). NULL
    #: on every row that never failed: claimable at once, as before.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Claim order: `claim_job` sorts by COALESCE(priority, 0) DESC, enqueued_at.
    #: 10 = interactive (a person is waiting), -10 = bulk; NULL = 0 (every row
    #: written before 0056, and every enqueue that states no priority).
    priority: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    #: A change requested while this job was processing (migration 0060, spec §4.2):
    #: `job_queue.request_job` sets it; `claim_job` clears it; when the job ends
    #: (`completed` or `dead`) the worker inserts a fresh pending job carrying
    #: `rerun_not_before`; a retry back to `pending` clears both.
    rerun_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rerun_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_jobs_one_active_per_user_type", "user_id", "type",
            unique=True, postgresql_where=text(ONE_ACTIVE_PER_USER_TYPE_WHERE),
        ),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_user_id", "user_id"),
    )

    # Relationships
    user: Mapped["User | None"] = relationship("User", back_populates="jobs")

    def __repr__(self) -> str:
        return f"<Job id={self.id} type={self.type} status={self.status}>"
