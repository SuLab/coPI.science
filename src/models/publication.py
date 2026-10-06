"""Publication model and the staff review queue of unanchored finds (migration 0062)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base

#: publications.provenance (migration 0062, spec 2026-10-05 §6.3). Staff accepted the paper
#: or kept an unanchored row: no run overwrites the row's provenance or metadata.
PROVENANCE_MANUAL = "manual"
#: The resolver does not return the row with an ORCID anchor (stage s1 or s3); listed on the
#: manager's Unanchored papers card until staff keep or exclude it.
PROVENANCE_UNANCHORED = "unanchored"
#: Any other value is the stage list of an anchored record, sorted and comma-joined ("s1,s4");
#: the CHECK requires s1 or s3 in it, so a stage list can never mean "unanchored".
PROVENANCE_CHECK_SQL = (
    "provenance IS NULL OR provenance IN ('manual', 'unanchored') "
    "OR provenance ~ '^(s[1-4],)*s[13](,s[1-4])*$'"
)
#: publication_candidates.status (migration 0062).
CANDIDATE_STATUSES = ("pending", "accepted", "rejected")
#: publication_candidates.reason for a resolved record with no ORCID anchor.
CANDIDATE_REASON_NO_ANCHOR = "no_orcid_anchor"


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    pmid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pmcid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    journal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_position: Mapped[str | None] = mapped_column(
        Enum("first", "last", "middle", name="author_position_enum"), nullable=True
    )
    methods_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: How the row came to be stored (migration 0062): PROVENANCE_MANUAL, PROVENANCE_UNANCHORED
    #: or an anchored record's stage list. NULL: stored before 0062 and not yet seen by a profile run
    #: or scripts/unanchored_publications_report.py.
    provenance: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Staff excluded the row (migration 0062, D45). Excluded rows are kept (D15) and ignored
    #: by synthesis, export, RePORTER linking, discovery and industry scans
    #: (src/services/tenure_scope.py `publication_in_use`).
    excluded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    excluded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: True when `doi` is the DOI PubMed records for `pmid` (reconcile_pub_doi "ok", "filled"
    #: or "corrected"); False when PubMed has none to check it against ("unverified"); NULL
    #: when never checked or there is no DOI. The export links the DOI only when True and
    #: otherwise prefers the PubMed link (spec 2026-10-05 §6.3).
    doi_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="publications", foreign_keys=[user_id])

    # NULL pmids do not conflict (C24).
    __table_args__ = (
        UniqueConstraint("user_id", "pmid", name="uq_publications_user_pmid"),
        CheckConstraint(PROVENANCE_CHECK_SQL, name="ck_publications_provenance"),
        Index("ix_publications_pmid", "pmid"),
        Index("ix_publications_user_id", "user_id"),
        Index("ix_publications_excluded_by_user_id", "excluded_by_user_id"),
    )

    def __repr__(self) -> str:
        return f"<Publication id={self.id} pmid={self.pmid} title={self.title[:40]!r}>"


class PublicationCandidate(Base):
    """A resolved record with no ORCID anchor, held for staff review instead of being stored
    (migration 0062, spec 2026-10-05 §6.3, D17, D21). One row per (PI, PMID). Accept stores
    the paper with provenance "manual"; Reject keeps the row so the paper is never offered
    again. A pending candidate that a later run returns anchored is stored and marked
    accepted with no deciding user."""

    __tablename__ = "publication_candidates"
    __table_args__ = (
        UniqueConstraint("user_id", "pmid", name="uq_publication_candidates_user_pmid"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_publication_candidates_status",
        ),
        Index("ix_publication_candidates_decided_by_user_id", "decided_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    pmid: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The resolver stages that found it, sorted and comma-joined ("s2,s4").
    stages: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")
    #: Who accepted or rejected it; NULL for the system (a pending candidate stored because
    #: a later run found it anchored) and after that user is deleted.
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
