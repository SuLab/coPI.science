"""The landscaping pipeline's tables (migration 0077).

Spec: docs/specs/2026-10-30-blackbird-landscaping-design.md §11.1.

Every table is new, prefixed ``landscape_``, and owned by the landscaping worker and its pages.
None carries ``simulation_run_id``: the pipeline runs outside the simulation (K3). Three kinds:

- **Shared inputs** carry no epoch: sources, fetches, documents (with the ``merged_into``
  tombstone of spec §6.1), document links, full texts, labs and the evidence cache.
- **Epoch-scoped judgments** carry ``epoch_id`` and have it in their uniqueness key (spec §7.1),
  so a candidate epoch re-judges beside the active one without touching its rows (§7.11). An
  idea's identity across epochs is its ``idea_key`` string, not its row id.
- **Stable identities and review** carry no epoch: opportunities, decisions, reviews, events,
  assignments, weekly batches and expert judgments. Decisions and reviews record the
  ``landscape_assessment_id`` they were made on (spec §13.3).

**Stage columns** (spec §7.1). Every row a stage result fills carries:

- ``status``: one of ``STAGE_STATUSES``, or ``excluded:<reason>`` / ``failed:<reason>``, held by
  a CHECK (``STAGE_STATUS_CHECK``). S1's result is the exception: it lives on the shared
  document row as ``s1_status`` (``eligible`` or ``excluded:<reason>``).
- ``batch_item_id`` (batched stages only): the batch request the row was last submitted in.
  Ingestion is a compare-and-set on ``status = 'submitted'`` and this id, so a replayed chunk,
  or a late result for an item since resubmitted, changes nothing (spec §7.1 step 5).
- ``attempts`` (resubmissions after ``errored``/``expired``), ``truncated_once`` (the next
  request uses the stage's retry limit), ``fallback_from`` (the model that refused, while the
  row waits for the fallback chain, D42), ``served_by_model``, ``refusal_category`` and
  ``error`` (stored through ``job_queue.job_error_text``).

``landscape_deep`` (S6) is synchronous, so it has the stage columns without ``batch_item_id``.

Status-like columns are strings with a CHECK, never a Postgres enum type, and every JSONB
column uses ``none_as_null=True`` (tests/unit/test_json_none_as_null.py). No relationship is
declared; foreign keys are plain columns.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

#: A stage row's plain statuses; ``excluded:<reason>`` and ``failed:<reason>`` are also valid.
STAGE_STATUSES = ("pending", "submitting", "submitted", "running", "done", "rescued", "refused")
#: ``landscape_epochs.status``; a partial unique index allows one ``active`` row.
EPOCH_STATUSES = ("candidate", "held", "active", "retired")
#: ``landscape_decisions.decision`` (L39).
DECISIONS = ("pursue", "pass")
#: ``landscape_review_events.action``.
REVIEW_ACTIONS = ("approved", "disapproved", "cleared")
#: ``landscape_batches.status``, in order.
BATCH_STATUSES = ("submitting", "submitted", "ended", "ingested")
#: ``landscape_reports.kind``.
REPORT_KINDS = (
    "bakeoff", "bakeoff_request", "probes", "pilot", "completeness", "drift",
    "positive_controls", "metrics",
)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


#: The CHECK on every stage row's ``status`` (spec §7.1).
STAGE_STATUS_CHECK = (
    _one_of("status", STAGE_STATUSES)
    + " OR status LIKE 'excluded:%' OR status LIKE 'failed:%'"
)


class LandscapeSource(Base):
    """One discovery source: its schedule, watermark and last completeness (spec §6.1, §12.4)."""

    __tablename__ = "landscape_sources"
    __table_args__ = (
        UniqueConstraint("source", name="uq_landscape_sources_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: Where the next incremental fetch starts (source-specific).
    watermark: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Received ÷ source-reported total of the last fetch.
    last_completeness_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeFetch(Base):
    """One paged fetch slice, expected vs received: the completeness record (G1, spec §12.4)."""

    __tablename__ = "landscape_fetches"
    __table_args__ = (
        UniqueConstraint(
            "source", "slice_key", "started_at",
            name="uq_landscape_fetches_source_slice_started",
        ),
        CheckConstraint(
            "status IN ('running', 'complete', 'incomplete', 'failed')",
            name="ck_landscape_fetches_status",
        ),
        Index("ix_landscape_fetches_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    slice_key: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The total the source reported for the slice.
    expected_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LandscapeLab(Base):
    """One lab key (spec §5.2): behind the chemistry key, same-lab edges and group counts."""

    __tablename__ = "landscape_labs"
    __table_args__ = (
        UniqueConstraint("lab_key", name="uq_landscape_labs_lab_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``orcid:…``, ``oa:…``, ``nih:…`` or ``name:<surname>|<initial>``.
    lab_key: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    orcid: Mapped[str | None] = mapped_column(String(40), nullable=True)
    openalex_author_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reporter_profile_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: Other lab keys unified into this one (F5).
    aliases: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: The canonical lab key this key was unified into, or NULL while it is canonical (D64).
    alias_of: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeEvidenceCache(Base):
    """One external lookup per source release (spec §7.4 step 5); a newer release expires it."""

    __tablename__ = "landscape_evidence_cache"
    __table_args__ = (
        UniqueConstraint(
            "source", "release", "query_key",
            name="uq_landscape_evidence_cache_source_release_query",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    release: Mapped[str] = mapped_column(String(60), nullable=False)
    query_key: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeDocument(Base):
    """One linked source record (spec §6.1), shared by every epoch; S1's result lives here."""

    __tablename__ = "landscape_documents"
    __table_args__ = (
        UniqueConstraint(
            "kind", "canonical_external_id",
            name="uq_landscape_documents_kind_external_id",
        ),
        CheckConstraint(
            "kind IN ('work', 'preprint', 'grant', 'patent', 'jhtv', 'dataset')",
            name="ck_landscape_documents_kind",
        ),
        CheckConstraint(
            "s1_status = 'eligible' OR s1_status LIKE 'excluded:%'",
            name="ck_landscape_documents_s1_status",
        ),
        Index("ix_landscape_documents_doi", "doi"),
        Index("ix_landscape_documents_pmid", "pmid"),
        Index("ix_landscape_documents_openalex_id", "openalex_id"),
        Index("ix_landscape_documents_lab_key", "lab_key"),
        Index("ix_landscape_documents_s1_status", "s1_status"),
        Index("ix_landscape_documents_merged_into", "merged_into"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The highest-precedence id (DOI > PMID > OpenAlex id for works, D38).
    canonical_external_id: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Set on a tombstone: the kept row of the same work (spec §6.1, F10). Ideas count, hash and
    #: pool only rows where this is NULL.
    merged_into: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="SET NULL"), nullable=True
    )
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pmid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pmcid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    openalex_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: Every other source id (appl_id, caseId, patent number, …).
    external_ids: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What S2 and S3 read (spec §6.1 summary precedence).
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: sha256 of ``summary_text``; a change re-runs S2 and S3.
    summary_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Other dates: grant period, filing, priority, entry.
    dates: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    license: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_oa: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    oa_flags: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    is_retracted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Kind-specific normalized fields (amounts, first claim, inventors, …).
    details: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    qualifying_authorships: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    qualifying_institution: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: How the record qualified (spec §5.2), e.g. ``grant_pi``.
    role_basis: Mapped[str | None] = mapped_column(String(30), nullable=True)
    lab_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: ``eligible`` or ``excluded:<reason>``; recomputed when the row is re-keyed.
    s1_status: Mapped[str] = mapped_column(String(60), nullable=False)
    s1_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeDocumentLink(Base):
    """A deterministic document link (spec §6.1); LLM-named links are extraction links."""

    __tablename__ = "landscape_document_links"
    __table_args__ = (
        UniqueConstraint(
            "doc_a", "doc_b", "link_kind",
            name="uq_landscape_document_links_pair_kind",
        ),
        CheckConstraint(
            "link_kind IN ('same_work', 'preprint_of', 'grant_cites', 'dataset_of')",
            name="ck_landscape_document_links_kind",
        ),
        Index("ix_landscape_document_links_doc_b", "doc_b"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    doc_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    link_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeText(Base):
    """One fetched full text, for documents behind ideas that reached S6 (spec §6.2, §11.2)."""

    __tablename__ = "landscape_texts"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_landscape_texts_document_id"),
        CheckConstraint(
            "text_basis IN ('full_text', 'summary', 'fetch_failed')",
            name="ck_landscape_texts_basis",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    #: The sectioned text, gzip-compressed, at most 200,000 characters.
    text_gz: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: Section index into the text.
    sections: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    text_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    fetch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    license: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: pmc, europepmc or biorxiv.
    route: Mapped[str | None] = mapped_column(String(20), nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeBatch(Base):
    """One Message Batch, committed ``submitting`` before ``batches.create`` (spec §7.1, D31)."""

    __tablename__ = "landscape_batches"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_landscape_batches_seq"),
        UniqueConstraint("api_batch_id", name="uq_landscape_batches_api_batch_id"),
        CheckConstraint(_one_of("status", BATCH_STATUSES), name="ck_landscape_batches_status"),
        Index("ix_landscape_batches_status", "status"),
    )
    #: ``seq`` is generated by the database; RETURNING fetches it at flush.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Assigned by the database; the ``b{seq}-`` prefix of every ``custom_id``.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    #: The API's batch id, stored after ``create`` returns; NULL while unconfirmed.
    api_batch_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The stage rows' table name.
    row_table: Mapped[str] = mapped_column(String(63), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="submitting")
    create_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeBatchItem(Base):
    """One request in a Message Batch. Stage rows reference it as ``batch_item_id`` (spec §7.1)."""

    __tablename__ = "landscape_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "item_seq", name="uq_landscape_batch_items_batch_seq"),
        Index("ix_landscape_batch_items_row_id", "row_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_batches.id", ondelete="CASCADE"), nullable=False
    )
    item_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The stage row (in the batch's ``row_table``).
    row_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The ingested outcome kind; NULL until ingested.
    result_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeRegressionSet(Base):
    """One version of the regression set (spec §10.4); additions create a new version."""

    __tablename__ = "landscape_regression_sets"
    __table_args__ = (
        UniqueConstraint("version", name="uq_landscape_regression_sets_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeEpoch(Base):
    """One judgment epoch (spec §7.11): the judging inputs every epoch-scoped row is keyed on."""

    __tablename__ = "landscape_epochs"
    __table_args__ = (
        CheckConstraint(_one_of("status", EPOCH_STATUSES), name="ck_landscape_epochs_status"),
        # At most one active epoch.
        Index(
            "uq_landscape_epochs_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_landscape_epochs_built_from_epoch_id", "built_from_epoch_id"),
        Index("ix_landscape_epochs_override_actor_id", "override_actor_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="candidate")
    #: The identifying components (D61): judging hashes, criteria and rubric hashes, models.
    components: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: The judging inputs themselves, stored when the epoch is created (``epochs.build_snapshot``):
    #: prompt and criteria texts, BlackbirdBot extracts, rubric text and stage models. Its stage
    #: runners read these, never the files on disk or the current settings (§7.11).
    snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: Recorded for people to read; does not identify.
    prompt_set_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Recorded for people to read; does not identify.
    criteria_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The stages this candidate re-judges (§7.11 step 2).
    changed_stages: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    built_from_epoch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="SET NULL"), nullable=True
    )
    regression_set_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The regression run that passed or held it; no foreign key, since runs reference their epoch.
    regression_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Items created before this are the ones the drain waits for (§7.11 step 4).
    rejudge_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeRegressionItem(Base):
    """One regression item with its expected output (spec §10.4)."""

    __tablename__ = "landscape_regression_items"
    __table_args__ = (
        UniqueConstraint("set_version", "id", name="uq_landscape_regression_items_set_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    set_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("landscape_regression_sets.version", ondelete="CASCADE"), nullable=False
    )
    #: The stage it checks.
    item_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    #: bakeoff, frozen, positive_control, rescue or expert.
    origin: Mapped[str] = mapped_column(String(30), nullable=False)
    item_ref: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    expected: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeRegressionRun(Base):
    """One regression run against a candidate epoch (spec §10.4)."""

    __tablename__ = "landscape_regression_runs"
    __table_args__ = (
        UniqueConstraint(
            "epoch_id", "started_at",
            name="uq_landscape_regression_runs_epoch_started",
        ),
        CheckConstraint(
            "status IN ('running', 'passed', 'failed')",
            name="ck_landscape_regression_runs_status",
        ),
        Index("ix_landscape_regression_runs_set_version", "set_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    set_version: Mapped[int] = mapped_column(
        Integer, ForeignKey("landscape_regression_sets.version", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    #: Per-stage results, pass or fail.
    results: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)


class LandscapeScreen(Base):
    """One S2 result per document and epoch (spec §7.3)."""

    __tablename__ = "landscape_screens"
    __table_args__ = (
        UniqueConstraint("document_id", "epoch_id", name="uq_landscape_screens_document_epoch"),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_screens_status"),
        Index("ix_landscape_screens_status", "status"),
        Index("ix_landscape_screens_epoch_id", "epoch_id"),
        Index("ix_landscape_screens_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: yes, no or unsure.
    decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    kinds: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    mentions: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The summary judged; a different hash re-runs S2.
    summary_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeExtraction(Base):
    """One S3 (``summary``) or S6 (``full_text``) extraction per document and epoch (§7.4)."""

    __tablename__ = "landscape_extractions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "epoch_id", "basis",
            name="uq_landscape_extractions_document_epoch_basis",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_extractions_status"),
        CheckConstraint(
            "basis IN ('summary', 'full_text')",
            name="ck_landscape_extractions_basis",
        ),
        Index("ix_landscape_extractions_status", "status"),
        Index("ix_landscape_extractions_epoch_id", "epoch_id"),
        Index("ix_landscape_extractions_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    basis: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The parsed model output, quotes as checked.
    raw: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    quotes_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quotes_verified: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeIdea(Base):
    """One idea in an epoch (spec §7.4 step 7). ``idea_key`` links an idea across epochs."""

    __tablename__ = "landscape_ideas"
    __table_args__ = (
        UniqueConstraint("epoch_id", "idea_key", name="uq_landscape_ideas_epoch_key"),
        CheckConstraint(
            "idea_type IN ('biology', 'chemistry')",
            name="ck_landscape_ideas_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    idea_key: Mapped[str] = mapped_column(String(500), nullable=False)
    idea_type: Mapped[str] = mapped_column(String(20), nullable=False)
    #: True while the key holds an unmatched target, disease or agent (D36).
    provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: Ids and labels; unmatched names keep provisional keys.
    targets: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(20), nullable=True)
    agent: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    disease: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: Which facets matched HGNC, Mondo and ChEMBL.
    normalization: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    modality_variants: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    evidence_grade: Mapped[str | None] = mapped_column(String(40), nullable=True)
    jhu_group_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Filled by S6; NULL until then.
    outside_group_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The assembled database evidence (§7.4 step 5).
    evidence_summary: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: target_essential, safety_liabilities, same_target_in_clinic, each with evidence.
    data_flags: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: Per evidence source: ``{"status": "pending" | "done" | "failed", "attempts": int}``.
    evidence_status: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    evidence_assembled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Hash of the attached qualifying idea-source documents (D37); triage keys on it.
    version_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    in_current_cut: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeIdeaLink(Base):
    """A biology ↔ chemistry link between two ideas from one document (spec §7.4 step 7)."""

    __tablename__ = "landscape_idea_links"
    __table_args__ = (
        UniqueConstraint("epoch_id", "idea_a", "idea_b", name="uq_landscape_idea_links_epoch_pair"),
        Index("ix_landscape_idea_links_idea_a", "idea_a"),
        Index("ix_landscape_idea_links_idea_b", "idea_b"),
        Index("ix_landscape_idea_links_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    idea_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    idea_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeExtractionLink(Base):
    """A patent or JHTV listing ↔ paper link named by an S3 extraction (spec §6.1, F1, D67)."""

    __tablename__ = "landscape_extraction_links"
    __table_args__ = (
        UniqueConstraint(
            "epoch_id", "doc_a", "doc_b",
            name="uq_landscape_extraction_links_epoch_pair",
        ),
        Index("ix_landscape_extraction_links_doc_a", "doc_a"),
        Index("ix_landscape_extraction_links_doc_b", "doc_b"),
        Index("ix_landscape_extraction_links_extraction_id", "extraction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    doc_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    doc_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False
    )
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_extractions.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeIdeaEvidence(Base):
    """Which extraction candidate fed an idea, and why it merged there."""

    __tablename__ = "landscape_idea_evidence"
    __table_args__ = (
        UniqueConstraint(
            "idea_id", "extraction_id", "candidate_index",
            name="uq_landscape_idea_evidence_idea_extraction_index",
        ),
        Index("ix_landscape_idea_evidence_extraction_id", "extraction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idea_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_extractions.id", ondelete="CASCADE"), nullable=False
    )
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False)
    merge_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeIdeaAlias(Base):
    """A provisional key that resolved onto an existing idea (D36, D64)."""

    __tablename__ = "landscape_idea_aliases"
    __table_args__ = (
        UniqueConstraint("epoch_id", "old_key", name="uq_landscape_idea_aliases_epoch_old_key"),
        Index("ix_landscape_idea_aliases_idea_id", "idea_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    old_key: Mapped[str] = mapped_column(String(500), nullable=False)
    #: The idea it merged into.
    idea_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeTriage(Base):
    """One S4 result per idea version and epoch (spec §7.5)."""

    __tablename__ = "landscape_triage"
    __table_args__ = (
        UniqueConstraint(
            "idea_id", "idea_version", "epoch_id",
            name="uq_landscape_triage_idea_version_epoch",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_triage_status"),
        Index("ix_landscape_triage_status", "status"),
        Index("ix_landscape_triage_epoch_id", "epoch_id"),
        Index("ix_landscape_triage_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idea_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    #: The idea's ``version_hash`` judged.
    idea_version: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    gating: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    gating_rationales: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    dimension_rationales: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    target_rubric: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    evidence_grade: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    llm_flags: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    red_flags: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    in_therapeutic_scope: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    druggability_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Computed in code (spec §7.5), never taken from the model.
    triage_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeCut(Base):
    """One S5 run in an epoch (spec §7.6)."""

    __tablename__ = "landscape_cuts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('boundary_pending', 'complete')",
            name="ck_landscape_cuts_status",
        ),
        Index("ix_landscape_cuts_epoch_id", "epoch_id"),
        Index("ix_landscape_cuts_boundary_batch_id", "boundary_batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    #: ``boundary_pending`` while its tie-break batch runs; the previous cut stays current.
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="complete")
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    ordered_idea_ids: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    folds: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    tie_class_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    boundary_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_batches.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeCandidateGrouping(Base):
    """A candidate epoch's provisional S8 grouping (spec §7.9, D59); matched at activation."""

    __tablename__ = "landscape_candidate_groupings"
    __table_args__ = (
        UniqueConstraint(
            "epoch_id", "grouping_key",
            name="uq_landscape_candidate_groupings_epoch_key",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_candidate_groupings_status"),
        Index("ix_landscape_candidate_groupings_status", "status"),
        Index("ix_landscape_candidate_groupings_cut_id", "cut_id"),
        Index("ix_landscape_candidate_groupings_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    #: The cut the grouping was formed from.
    cut_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_cuts.id", ondelete="CASCADE"), nullable=False
    )
    #: sha256 of its sorted (``idea_key``, idea version) pairs.
    grouping_key: Mapped[str] = mapped_column(String(64), nullable=False)
    member_idea_keys: Mapped[list] = mapped_column(JSONB(none_as_null=True), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    theme: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_signal: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    edge_kinds: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: Cleared when a later S8 run of the epoch no longer produces it.
    current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeDeep(Base):
    """One S6 result per idea version and epoch (spec §7.7); synchronous, so no batch item."""

    __tablename__ = "landscape_deep"
    __table_args__ = (
        UniqueConstraint(
            "idea_id", "idea_version", "epoch_id",
            name="uq_landscape_deep_idea_version_epoch",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_deep_status"),
        Index("ix_landscape_deep_status", "status"),
        Index("ix_landscape_deep_epoch_id", "epoch_id"),
        Index("ix_landscape_deep_cut_id", "cut_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idea_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    idea_version: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    #: The cut that queued it; NULL for a blind-batch probe idea.
    cut_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_cuts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: References to the full-text extractions.
    full_text_evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: Every field carries ``support`` (L30, L31).
    disease_profile: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    target_biology: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    gating: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    gating_rationales: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    flags_reviewed: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    provisional_scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    dimension_rationales: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    provisional_weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    outside_group_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: S6's free-text summary of the record; what the S7 comparator reads when present.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The retrieved source ids of the search loop.
    search_transcript: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeConsult(Base):
    """One S6 specialist consult (spec §7.7 step 8)."""

    __tablename__ = "landscape_consults"
    __table_args__ = (
        UniqueConstraint("deep_id", "domain", name="uq_landscape_consults_deep_domain"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deep_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_deep.id", ondelete="CASCADE"), nullable=False
    )
    domain: Mapped[str] = mapped_column(String(30), nullable=False)
    #: ``specialists.parse_opinion`` output.
    opinion: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeComparison(Base):
    """One pairwise call (spec §7.6 tie-break, basis ``triage``; §7.8, basis ``deep``)."""

    __tablename__ = "landscape_comparisons"
    __table_args__ = (
        UniqueConstraint(
            "idea_a", "version_a", "idea_b", "version_b", "epoch_id", "basis", "ask_order",
            name="uq_landscape_comparisons_pair",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_comparisons_status"),
        CheckConstraint(
            "basis IN ('triage', 'deep')",
            name="ck_landscape_comparisons_basis",
        ),
        CheckConstraint(
            "ask_order IN (1,2)",
            name="ck_landscape_comparisons_ask_order",
        ),
        Index("ix_landscape_comparisons_status", "status"),
        Index("ix_landscape_comparisons_idea_b", "idea_b"),
        Index("ix_landscape_comparisons_epoch_id", "epoch_id"),
        Index("ix_landscape_comparisons_batch_item_id", "batch_item_id"),
        Index("ix_landscape_comparisons_winner_idea_id", "winner_idea_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idea_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    version_a: Mapped[str] = mapped_column(String(64), nullable=False)
    idea_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    version_b: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    basis: Mapped[str] = mapped_column(String(20), nullable=False)
    #: 1 when ``idea_a`` was shown first, 2 when ``idea_b`` was (each pair is asked both ways).
    ask_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    winner_idea_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeRanking(Base):
    """One S7 position in an epoch's ranked list (spec §7.8)."""

    __tablename__ = "landscape_rankings"
    __table_args__ = (
        UniqueConstraint("epoch_id", "idea_id", name="uq_landscape_rankings_epoch_idea"),
        Index("ix_landscape_rankings_idea_id", "idea_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    idea_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeSimilarity(Base):
    """One similarity judgment per idea-version pair and epoch (spec §7.9, D14)."""

    __tablename__ = "landscape_similarity"
    __table_args__ = (
        UniqueConstraint(
            "idea_a", "version_a", "idea_b", "version_b", "epoch_id",
            name="uq_landscape_similarity_pair",
        ),
        Index("ix_landscape_similarity_idea_b", "idea_b"),
        Index("ix_landscape_similarity_epoch_id", "epoch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idea_a: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    version_a: Mapped[str] = mapped_column(String(64), nullable=False)
    idea_b: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False
    )
    version_b: Mapped[str] = mapped_column(String(64), nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeOpportunity(Base):
    """A stable opportunity identity (spec §7.9); carries no epoch."""

    __tablename__ = "landscape_opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The latest version number; only ever moves forward (D37, D50).
    version_counter: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeOpportunityVersion(Base):
    """One opportunity version (spec §7.9), minted by the active epoch's S8 or at activation."""

    __tablename__ = "landscape_opportunity_versions"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "version",
            name="uq_landscape_opportunity_versions_opportunity_version",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_opportunity_versions_status"),
        Index("ix_landscape_opportunity_versions_status", "status"),
        Index("ix_landscape_opportunity_versions_epoch_id", "epoch_id"),
        Index("ix_landscape_opportunity_versions_cut_id", "cut_id"),
        Index("ix_landscape_opportunity_versions_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    #: The cut it was grouped from; NULL for a probe.
    cut_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_cuts.id", ondelete="SET NULL"), nullable=True
    )
    member_idea_keys: Mapped[list] = mapped_column(JSONB(none_as_null=True), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    theme: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_signal: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    edge_kinds: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: A blind-batch probe (L52); never shown until its batch is superseded.
    is_probe: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeAssessment(Base):
    """One S9 verdict (spec §7.10), keyed by opportunity version and epoch, or in a candidate
    epoch by grouping (D59).
    """

    __tablename__ = "landscape_assessments"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "opportunity_version", "epoch_id",
            name="uq_landscape_assessments_opportunity_version_epoch",
        ),
        UniqueConstraint("epoch_id", "grouping_id", name="uq_landscape_assessments_epoch_grouping"),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_assessments_status"),
        # One complete key at least: (opportunity_id, opportunity_version), or grouping_id in a
        # candidate epoch until activation re-points it (D59); never half a key.
        CheckConstraint(
            "(opportunity_id IS NULL) = (opportunity_version IS NULL) AND (opportunity_id "
            "IS NOT NULL OR grouping_id IS NOT NULL)",
            name="ck_landscape_assessments_one_key",
        ),
        Index("ix_landscape_assessments_status", "status"),
        Index("ix_landscape_assessments_grouping_id", "grouping_id"),
        Index("ix_landscape_assessments_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=True
    )
    opportunity_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    grouping_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_candidate_groupings.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The contract's columns, named and typed as OpportunityAssessment's, so
    # ``verdict_fields.sidecar_column_kwargs`` output maps onto them unchanged (spec §7.10).
    company_or_project: Mapped[str | None] = mapped_column(Text, nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_points: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    elevator_pitch: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimension_rationales: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    gating_rationales: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    strengths: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    risks: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    competitive_landscape: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    evidence_maturity: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    funnel_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Computed by ``blackbird_rubric.weighted_score``.
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str | None] = mapped_column(String(20), nullable=True)
    gating: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    red_flags: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    derisking_milestones: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    prose_format: Mapped[str | None] = mapped_column(String(20), nullable=True)
    recommended_next_experiment: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_verdict: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    rubric_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rubric_content_hash: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Per key-point bullet, by group and index: references or ``"estimate"`` (L30).
    claim_support: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    member_notes: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: One per member idea, composed in code (D29).
    structured_lines: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeDecision(Base):
    """One quick decision per opportunity and reviewer (L39, spec §13.3)."""

    __tablename__ = "landscape_decisions"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "reviewer_id",
            name="uq_landscape_decisions_opportunity_reviewer",
        ),
        CheckConstraint(_one_of("decision", DECISIONS), name="ck_landscape_decisions_decision"),
        CheckConstraint(
            "char_length(reason) <= 1000",
            name="ck_landscape_decisions_reason_length",
        ),
        Index("ix_landscape_decisions_reviewer_id", "reviewer_id"),
        Index("ix_landscape_decisions_landscape_assessment_id", "landscape_assessment_id"),
        Index("ix_landscape_decisions_recorded_by_user_id", "recorded_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)
    reason_tags: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: The assessment shown when the decision was made (D50).
    landscape_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True
    )
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeReview(Base):
    """One optional full review per opportunity and reviewer (spec §13.3)."""

    __tablename__ = "landscape_reviews"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "reviewer_id",
            name="uq_landscape_reviews_opportunity_reviewer",
        ),
        CheckConstraint(
            "score >= 1 AND score <= 5",
            name="ck_landscape_reviews_score",
        ),
        CheckConstraint(
            "char_length(comment) <= 10000",
            name="ck_landscape_reviews_comment_length",
        ),
        Index("ix_landscape_reviews_reviewer_id", "reviewer_id"),
        Index("ix_landscape_reviews_landscape_assessment_id", "landscape_assessment_id"),
        Index("ix_landscape_reviews_recorded_by_user_id", "recorded_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    dimension_scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    rubric_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rubric_content_hash: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: The assessment shown when the review was written.
    landscape_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True
    )
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeReviewEvent(Base):
    """An approve, disapprove or clear on an opportunity; append-only (spec §13.3)."""

    __tablename__ = "landscape_review_events"
    __table_args__ = (
        CheckConstraint(
            _one_of("action", REVIEW_ACTIONS), name="ck_landscape_review_events_action",
        ),
        Index("ix_landscape_review_events_opportunity_id", "opportunity_id"),
        Index("ix_landscape_review_events_landscape_assessment_id", "landscape_assessment_id"),
        Index("ix_landscape_review_events_actor_id", "actor_id"),
        Index("ix_landscape_review_events_recorded_by_user_id", "recorded_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    landscape_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeReviewAssignment(Base):
    """A reviewer assigned to an opportunity (spec §13.3)."""

    __tablename__ = "landscape_review_assignments"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "assignee_id",
            name="uq_landscape_review_assignments_opportunity_assignee",
        ),
        Index("ix_landscape_review_assignments_assignee_id", "assignee_id"),
        Index("ix_landscape_review_assignments_assigned_by_user_id", "assigned_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assignee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeReviewBatch(Base):
    """One weekly review batch, the same for everyone (spec §13.1)."""

    __tablename__ = "landscape_review_batches"
    __table_args__ = (
        UniqueConstraint("issued_on", name="uq_landscape_review_batches_issued_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    blind: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeReviewBatchItem(Base):
    """One card in a weekly batch (spec §13.1, §10.6)."""

    __tablename__ = "landscape_review_batch_items"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "opportunity_id",
            name="uq_landscape_review_batch_items_batch_opportunity",
        ),
        Index("ix_landscape_review_batch_items_opportunity_id", "opportunity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_review_batches.id", ondelete="CASCADE"), nullable=False
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    is_probe: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    #: A blind batch's shuffled order; NULL in a normal batch, whose cards follow live card order
    #: (D30).
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeExpertJudgment(Base):
    """One expert sample item and, once submitted, the expert's own calls (L35, D63)."""

    __tablename__ = "landscape_expert_judgments"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "expert_id", "quarter",
            name="uq_landscape_expert_judgments_opportunity_expert_quarter",
        ),
        Index("ix_landscape_expert_judgments_expert_id", "expert_id"),
        Index("ix_landscape_expert_judgments_landscape_assessment_id", "landscape_assessment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    expert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expert_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: ``YYYY-Qn``.
    quarter: Mapped[str] = mapped_column(String(7), nullable=False)
    #: The S9 assessment the judgment is compared with.
    landscape_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True
    )
    #: The item was in a batch issued before the draw, so S9's calls may have been seen.
    issued_before_draw: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gates: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    scores: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    deal_breakers: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    recommendation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    drawn_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: NULL while the item is open: S9's and S6's calls stay hidden.
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeRejectAudit(Base):
    """One audited reject (spec §10.5, D66)."""

    __tablename__ = "landscape_reject_audits"
    __table_args__ = (
        UniqueConstraint(
            "item_kind", "item_id", "epoch_id",
            name="uq_landscape_reject_audits_item_epoch",
        ),
        CheckConstraint(STAGE_STATUS_CHECK, name="ck_landscape_reject_audits_status"),
        Index("ix_landscape_reject_audits_status", "status"),
        Index("ix_landscape_reject_audits_epoch_id", "epoch_id"),
        Index("ix_landscape_reject_audits_batch_item_id", "batch_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The audited stage row's kind.
    item_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The audited stage row; no foreign key.
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(60), nullable=False, server_default="pending")
    batch_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncated_once: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    fallback_from: Mapped[str | None] = mapped_column(String(100), nullable=True)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_decision: Mapped[str | None] = mapped_column(String(60), nullable=True)
    audit_decision: Mapped[str | None] = mapped_column(String(60), nullable=True)
    agree: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeRescue(Base):
    """One staff rescue of a filtered item, or one carried into a later epoch (spec §10.5)."""

    __tablename__ = "landscape_rescues"
    __table_args__ = (
        Index("ix_landscape_rescues_item", "item_kind", "item_id"),
        Index("ix_landscape_rescues_epoch_id", "epoch_id"),
        Index("ix_landscape_rescues_actor_id", "actor_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The rescued stage row; no foreign key.
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    epoch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    carried: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeLlmCall(Base):
    """The landscaping LLM ledger: one row per call (spec §9.5). Synchronous rows leave
    ``batch_id`` and ``item_seq`` NULL, so the unique pair never blocks them.
    """

    __tablename__ = "landscape_llm_calls"
    __table_args__ = (
        UniqueConstraint("batch_id", "item_seq", name="uq_landscape_llm_calls_batch_seq"),
        Index("ix_landscape_llm_calls_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("landscape_batches.id", ondelete="RESTRICT"), nullable=True
    )
    item_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Priced at the batch rate.
    batch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: ``llm_pricing.cost_for_request``; NULL for an unpriced model.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LandscapeReport(Base):
    """One generated report (spec §10, §12.4); ``bakeoff_request`` asks the worker for a run."""

    __tablename__ = "landscape_reports"
    __table_args__ = (
        UniqueConstraint("kind", "created_at", name="uq_landscape_reports_kind_created"),
        CheckConstraint(_one_of("kind", REPORT_KINDS), name="ck_landscape_reports_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: Set when the worker has acted on a request row.
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
