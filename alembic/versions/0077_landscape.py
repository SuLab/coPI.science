"""landscape_*: the landscaping pipeline's 42 tables

Creates every table of docs/specs/2026-10-30-blackbird-landscaping-design.md §11.1, with its
uniqueness rules: shared inputs (no epoch), epoch-scoped judgments (``epoch_id`` in every
uniqueness key, spec §7.1), stable opportunities and their review, and the audit, batch,
ledger and report tables. Status-like columns are strings with a CHECK (no new enum type), and
every foreign key column and every worker-claimed ``status`` column is indexed. No table has a
``simulation_run_id``, and no existing table is altered (K3).

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads or writes a ``landscape_`` table.

NEW CODE ON THE OLD SCHEMA: the landscaping worker and the landscape pages refuse
(``src.landscaping.schema_check.schema_ready`` is false while ``landscape_epochs`` is absent,
spec §12.5); nothing else reads these tables. The agent image needs no rebuild (D45): the
engine never touches them.

The foreign keys to ``users`` take a brief lock on ``users``; a 10 s lock_timeout rollback is
harmless, re-run.

Downgrade drops every table and every row, in reverse dependency order.

Revision ID: 0077
Revises: 0076
Create Date: 2026-10-30
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every stage row's status CHECK (spec §7.1); src/models/landscape.py's STAGE_STATUS_CHECK.
_STAGE_STATUS_CHECK = (
    "status IN ('pending', 'submitting', 'submitted', 'running', 'done', 'rescued', 'refused') "
    "OR status LIKE 'excluded:%' OR status LIKE 'failed:%'"
)


def upgrade() -> None:
    # Each helper runs after the tables its foreign keys reference.
    _create_shared_inputs()
    _create_batches_and_epochs()
    _create_screens_and_extractions()
    _create_ideas()
    _create_triage_cuts_and_deep()
    _create_consults_to_similarity()
    _create_opportunities()
    _create_decisions_and_reviews()
    _create_review_batches()
    _create_audits_ledger_reports()


def _create_shared_inputs() -> None:
    """Shared inputs: no epoch (spec §7.1)."""
    op.create_table(
        "landscape_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("watermark", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completeness_ratio", sa.Float(), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source", name="uq_landscape_sources_source"),
    )
    op.create_table(
        "landscape_fetches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("slice_key", sa.String(200), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_count", sa.Integer(), nullable=True),
        sa.Column("received_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "source", "slice_key", "started_at",
            name="uq_landscape_fetches_source_slice_started",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'complete', 'incomplete', 'failed')",
            name="ck_landscape_fetches_status",
        ),
    )
    op.create_index("ix_landscape_fetches_status", "landscape_fetches", ["status"])
    op.create_table(
        "landscape_labs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lab_key", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("orcid", sa.String(40), nullable=True),
        sa.Column("openalex_author_id", sa.String(60), nullable=True),
        sa.Column("reporter_profile_id", sa.String(60), nullable=True),
        sa.Column("aliases", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("alias_of", sa.String(200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("lab_key", name="uq_landscape_labs_lab_key"),
    )
    op.create_table(
        "landscape_evidence_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("release", sa.String(60), nullable=False),
        sa.Column("query_key", sa.String(500), nullable=False),
        sa.Column("payload", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "source", "release", "query_key",
            name="uq_landscape_evidence_cache_source_release_query",
        ),
    )
    op.create_table(
        "landscape_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("canonical_external_id", sa.String(300), nullable=False),
        sa.Column(
            "merged_into", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("pmid", sa.String(20), nullable=True),
        sa.Column("pmcid", sa.String(20), nullable=True),
        sa.Column("openalex_id", sa.String(60), nullable=True),
        sa.Column("external_ids", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("summary_source", sa.String(20), nullable=True),
        sa.Column("summary_hash", sa.String(64), nullable=True),
        sa.Column("published_on", sa.Date(), nullable=True),
        sa.Column("dates", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("document_type", sa.String(40), nullable=True),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("license", sa.String(60), nullable=True),
        sa.Column("is_oa", sa.Boolean(), nullable=True),
        sa.Column("oa_flags", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("is_retracted", sa.Boolean(), nullable=True),
        sa.Column("details", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("qualifying_authorships", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("qualifying_institution", sa.String(100), nullable=True),
        sa.Column("role_basis", sa.String(30), nullable=True),
        sa.Column("lab_key", sa.String(200), nullable=True),
        sa.Column("s1_status", sa.String(60), nullable=False),
        sa.Column("s1_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "kind", "canonical_external_id",
            name="uq_landscape_documents_kind_external_id",
        ),
        sa.CheckConstraint(
            "kind IN ('work', 'preprint', 'grant', 'patent', 'jhtv', 'dataset')",
            name="ck_landscape_documents_kind",
        ),
        sa.CheckConstraint(
            "s1_status = 'eligible' OR s1_status LIKE 'excluded:%'",
            name="ck_landscape_documents_s1_status",
        ),
    )
    op.create_index("ix_landscape_documents_doi", "landscape_documents", ["doi"])
    op.create_index("ix_landscape_documents_pmid", "landscape_documents", ["pmid"])
    op.create_index("ix_landscape_documents_openalex_id", "landscape_documents", ["openalex_id"])
    op.create_index("ix_landscape_documents_lab_key", "landscape_documents", ["lab_key"])
    op.create_index("ix_landscape_documents_s1_status", "landscape_documents", ["s1_status"])
    op.create_index("ix_landscape_documents_merged_into", "landscape_documents", ["merged_into"])
    op.create_table(
        "landscape_document_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_a", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "doc_b", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("link_kind", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "doc_a", "doc_b", "link_kind",
            name="uq_landscape_document_links_pair_kind",
        ),
        sa.CheckConstraint(
            "link_kind IN ('same_work', 'preprint_of', 'grant_cites', 'dataset_of')",
            name="ck_landscape_document_links_kind",
        ),
    )
    op.create_index("ix_landscape_document_links_doc_b", "landscape_document_links", ["doc_b"])
    op.create_table(
        "landscape_texts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("text_gz", sa.LargeBinary(), nullable=True),
        sa.Column("sections", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("text_basis", sa.String(20), nullable=False),
        sa.Column("fetch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("license", sa.String(60), nullable=True),
        sa.Column("route", sa.String(20), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("document_id", name="uq_landscape_texts_document_id"),
        sa.CheckConstraint(
            "text_basis IN ('full_text', 'summary', 'fetch_failed')",
            name="ck_landscape_texts_basis",
        ),
    )


def _create_batches_and_epochs() -> None:
    """Message Batches (stage rows reference their items), regression sets and epochs."""
    op.create_table(
        "landscape_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("api_batch_id", sa.String(100), nullable=True),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("row_table", sa.String(63), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="submitting"),
        sa.Column("create_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("seq", name="uq_landscape_batches_seq"),
        sa.UniqueConstraint("api_batch_id", name="uq_landscape_batches_api_batch_id"),
        sa.CheckConstraint(
            "status IN ('submitting', 'submitted', 'ended', 'ingested')",
            name="ck_landscape_batches_status",
        ),
    )
    op.create_index("ix_landscape_batches_status", "landscape_batches", ["status"])
    op.create_table(
        "landscape_batch_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "batch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batches.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("item_seq", sa.Integer(), nullable=False),
        sa.Column("row_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_kind", sa.String(20), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("batch_id", "item_seq", name="uq_landscape_batch_items_batch_seq"),
    )
    op.create_index("ix_landscape_batch_items_row_id", "landscape_batch_items", ["row_id"])
    op.create_table(
        "landscape_regression_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("version", name="uq_landscape_regression_sets_version"),
    )
    op.create_table(
        "landscape_epochs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="candidate"),
        sa.Column("components", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("prompt_set_version", sa.String(40), nullable=True),
        sa.Column("criteria_version", sa.String(40), nullable=True),
        sa.Column("changed_stages", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "built_from_epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("regression_set_version", sa.Integer(), nullable=True),
        sa.Column("regression_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hold_reason", sa.Text(), nullable=True),
        sa.Column(
            "override_actor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("override_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejudge_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'held', 'active', 'retired')",
            name="ck_landscape_epochs_status",
        ),
    )
    op.create_index(
        "uq_landscape_epochs_one_active", "landscape_epochs", ["status"],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_landscape_epochs_built_from_epoch_id", "landscape_epochs", ["built_from_epoch_id"],
    )
    op.create_index(
        "ix_landscape_epochs_override_actor_id", "landscape_epochs", ["override_actor_id"],
    )
    op.create_table(
        "landscape_regression_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "set_version", sa.Integer(),
            sa.ForeignKey("landscape_regression_sets.version", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("item_kind", sa.String(30), nullable=False),
        sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("item_ref", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("expected", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("set_version", "id", name="uq_landscape_regression_items_set_id"),
    )
    op.create_table(
        "landscape_regression_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "set_version", sa.Integer(),
            sa.ForeignKey("landscape_regression_sets.version", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("results", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.UniqueConstraint(
            "epoch_id", "started_at",
            name="uq_landscape_regression_runs_epoch_started",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'passed', 'failed')",
            name="ck_landscape_regression_runs_status",
        ),
    )
    op.create_index(
        "ix_landscape_regression_runs_set_version", "landscape_regression_runs", ["set_version"],
    )


def _create_screens_and_extractions() -> None:
    """S2 and S3 results, epoch-scoped."""
    op.create_table(
        "landscape_screens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("decision", sa.String(10), nullable=True),
        sa.Column("kinds", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("mentions", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("summary_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("document_id", "epoch_id", name="uq_landscape_screens_document_epoch"),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_screens_status",
        ),
    )
    op.create_index("ix_landscape_screens_status", "landscape_screens", ["status"])
    op.create_index("ix_landscape_screens_epoch_id", "landscape_screens", ["epoch_id"])
    op.create_index("ix_landscape_screens_batch_item_id", "landscape_screens", ["batch_item_id"])
    op.create_table(
        "landscape_extractions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("basis", sa.String(20), nullable=False),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("quotes_total", sa.Integer(), nullable=True),
        sa.Column("quotes_verified", sa.Integer(), nullable=True),
        sa.Column("summary_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "document_id", "epoch_id", "basis",
            name="uq_landscape_extractions_document_epoch_basis",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_extractions_status",
        ),
        sa.CheckConstraint(
            "basis IN ('summary', 'full_text')",
            name="ck_landscape_extractions_basis",
        ),
    )
    op.create_index("ix_landscape_extractions_status", "landscape_extractions", ["status"])
    op.create_index("ix_landscape_extractions_epoch_id", "landscape_extractions", ["epoch_id"])
    op.create_index(
        "ix_landscape_extractions_batch_item_id", "landscape_extractions", ["batch_item_id"],
    )


def _create_ideas() -> None:
    """Ideas with their links, evidence and aliases, epoch-scoped."""
    op.create_table(
        "landscape_ideas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("idea_key", sa.String(500), nullable=False),
        sa.Column("idea_type", sa.String(20), nullable=False),
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("targets", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("direction", sa.String(20), nullable=True),
        sa.Column("agent", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("disease", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("normalization", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("modality_variants", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("stage", sa.String(40), nullable=True),
        sa.Column("evidence_grade", sa.String(40), nullable=True),
        sa.Column("jhu_group_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outside_group_count", sa.Integer(), nullable=True),
        sa.Column("evidence_summary", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("data_flags", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("evidence_status", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("evidence_assembled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version_hash", sa.String(64), nullable=True),
        sa.Column("in_current_cut", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("epoch_id", "idea_key", name="uq_landscape_ideas_epoch_key"),
        sa.CheckConstraint(
            "idea_type IN ('biology', 'chemistry')",
            name="ck_landscape_ideas_type",
        ),
    )
    op.create_table(
        "landscape_idea_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "idea_a", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "idea_b", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "document_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "epoch_id", "idea_a", "idea_b",
            name="uq_landscape_idea_links_epoch_pair",
        ),
    )
    op.create_index("ix_landscape_idea_links_idea_a", "landscape_idea_links", ["idea_a"])
    op.create_index("ix_landscape_idea_links_idea_b", "landscape_idea_links", ["idea_b"])
    op.create_index("ix_landscape_idea_links_document_id", "landscape_idea_links", ["document_id"])
    op.create_table(
        "landscape_extraction_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "doc_a", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "doc_b", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_documents.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "extraction_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_extractions.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "epoch_id", "doc_a", "doc_b",
            name="uq_landscape_extraction_links_epoch_pair",
        ),
    )
    op.create_index("ix_landscape_extraction_links_doc_a", "landscape_extraction_links", ["doc_a"])
    op.create_index("ix_landscape_extraction_links_doc_b", "landscape_extraction_links", ["doc_b"])
    op.create_index(
        "ix_landscape_extraction_links_extraction_id",
        "landscape_extraction_links", ["extraction_id"],
    )
    op.create_table(
        "landscape_idea_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "idea_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "extraction_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_extractions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("merge_reason", sa.String(60), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idea_id", "extraction_id", "candidate_index",
            name="uq_landscape_idea_evidence_idea_extraction_index",
        ),
    )
    op.create_index(
        "ix_landscape_idea_evidence_extraction_id", "landscape_idea_evidence", ["extraction_id"],
    )
    op.create_table(
        "landscape_idea_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("old_key", sa.String(500), nullable=False),
        sa.Column(
            "idea_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("epoch_id", "old_key", name="uq_landscape_idea_aliases_epoch_old_key"),
    )
    op.create_index("ix_landscape_idea_aliases_idea_id", "landscape_idea_aliases", ["idea_id"])


def _create_triage_cuts_and_deep() -> None:
    """S4, S5, S8 candidate groupings and S6, epoch-scoped."""
    op.create_table(
        "landscape_triage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "idea_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("idea_version", sa.String(64), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("gating", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("gating_rationales", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("scores", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("dimension_rationales", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("target_rubric", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("evidence_grade", sa.String(40), nullable=True),
        sa.Column("stage", sa.String(40), nullable=True),
        sa.Column("llm_flags", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("confidence", sa.String(20), nullable=True),
        sa.Column("in_therapeutic_scope", sa.Boolean(), nullable=True),
        sa.Column("druggability_note", sa.Text(), nullable=True),
        sa.Column("triage_score", sa.Float(), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=True),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idea_id", "idea_version", "epoch_id",
            name="uq_landscape_triage_idea_version_epoch",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_triage_status",
        ),
    )
    op.create_index("ix_landscape_triage_status", "landscape_triage", ["status"])
    op.create_index("ix_landscape_triage_epoch_id", "landscape_triage", ["epoch_id"])
    op.create_index("ix_landscape_triage_batch_item_id", "landscape_triage", ["batch_item_id"])
    op.create_table(
        "landscape_cuts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="complete"),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("ordered_idea_ids", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("folds", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("tie_class_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "boundary_batch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batches.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('boundary_pending', 'complete')",
            name="ck_landscape_cuts_status",
        ),
    )
    op.create_index("ix_landscape_cuts_epoch_id", "landscape_cuts", ["epoch_id"])
    op.create_index("ix_landscape_cuts_boundary_batch_id", "landscape_cuts", ["boundary_batch_id"])
    op.create_table(
        "landscape_candidate_groupings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "cut_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_cuts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("grouping_key", sa.String(64), nullable=False),
        sa.Column("member_idea_keys", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("theme", sa.Text(), nullable=True),
        sa.Column("platform_signal", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("edge_kinds", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "epoch_id", "grouping_key",
            name="uq_landscape_candidate_groupings_epoch_key",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_candidate_groupings_status",
        ),
    )
    op.create_index(
        "ix_landscape_candidate_groupings_status", "landscape_candidate_groupings", ["status"],
    )
    op.create_index(
        "ix_landscape_candidate_groupings_cut_id", "landscape_candidate_groupings", ["cut_id"],
    )
    op.create_index(
        "ix_landscape_candidate_groupings_batch_item_id",
        "landscape_candidate_groupings", ["batch_item_id"],
    )
    op.create_table(
        "landscape_deep",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "idea_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("idea_version", sa.String(64), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "cut_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_cuts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("full_text_evidence", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("disease_profile", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("target_biology", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("gating", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("gating_rationales", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("flags_reviewed", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("provisional_scores", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("dimension_rationales", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("provisional_weighted_score", sa.Float(), nullable=True),
        sa.Column("outside_group_count", sa.Integer(), nullable=True),
        sa.Column("search_transcript", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idea_id", "idea_version", "epoch_id",
            name="uq_landscape_deep_idea_version_epoch",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_deep_status",
        ),
    )
    op.create_index("ix_landscape_deep_status", "landscape_deep", ["status"])
    op.create_index("ix_landscape_deep_epoch_id", "landscape_deep", ["epoch_id"])
    op.create_index("ix_landscape_deep_cut_id", "landscape_deep", ["cut_id"])


def _create_consults_to_similarity() -> None:
    """S6 consults, S5/S7 comparisons, S7 and similarity."""
    op.create_table(
        "landscape_consults",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "deep_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_deep.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("domain", sa.String(30), nullable=False),
        sa.Column("opinion", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("deep_id", "domain", name="uq_landscape_consults_deep_domain"),
    )
    op.create_table(
        "landscape_comparisons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "idea_a", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version_a", sa.String(64), nullable=False),
        sa.Column(
            "idea_b", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version_b", sa.String(64), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("basis", sa.String(20), nullable=False),
        sa.Column("ask_order", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "winner_idea_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idea_a", "version_a", "idea_b", "version_b", "epoch_id", "basis", "ask_order",
            name="uq_landscape_comparisons_pair",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_comparisons_status",
        ),
        sa.CheckConstraint(
            "basis IN ('triage', 'deep')",
            name="ck_landscape_comparisons_basis",
        ),
        sa.CheckConstraint(
            "ask_order IN (1,2)",
            name="ck_landscape_comparisons_ask_order",
        ),
    )
    op.create_index("ix_landscape_comparisons_status", "landscape_comparisons", ["status"])
    op.create_index("ix_landscape_comparisons_idea_b", "landscape_comparisons", ["idea_b"])
    op.create_index("ix_landscape_comparisons_epoch_id", "landscape_comparisons", ["epoch_id"])
    op.create_index(
        "ix_landscape_comparisons_batch_item_id", "landscape_comparisons", ["batch_item_id"],
    )
    op.create_index(
        "ix_landscape_comparisons_winner_idea_id", "landscape_comparisons", ["winner_idea_id"],
    )
    op.create_table(
        "landscape_rankings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "idea_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "inserted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("epoch_id", "idea_id", name="uq_landscape_rankings_epoch_idea"),
    )
    op.create_index("ix_landscape_rankings_idea_id", "landscape_rankings", ["idea_id"])
    op.create_table(
        "landscape_similarity",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "idea_a", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version_a", sa.String(64), nullable=False),
        sa.Column(
            "idea_b", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_ideas.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version_b", sa.String(64), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("verdict", sa.String(20), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "idea_a", "version_a", "idea_b", "version_b", "epoch_id",
            name="uq_landscape_similarity_pair",
        ),
    )
    op.create_index("ix_landscape_similarity_idea_b", "landscape_similarity", ["idea_b"])
    op.create_index("ix_landscape_similarity_epoch_id", "landscape_similarity", ["epoch_id"])


def _create_opportunities() -> None:
    """Stable opportunities, their versions and S9 assessments."""
    op.create_table(
        "landscape_opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "landscape_opportunity_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "cut_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_cuts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("member_idea_keys", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("theme", sa.Text(), nullable=True),
        sa.Column("platform_signal", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("edge_kinds", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("is_probe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "version",
            name="uq_landscape_opportunity_versions_opportunity_version",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_opportunity_versions_status",
        ),
    )
    op.create_index(
        "ix_landscape_opportunity_versions_status", "landscape_opportunity_versions", ["status"],
    )
    op.create_index(
        "ix_landscape_opportunity_versions_epoch_id",
        "landscape_opportunity_versions", ["epoch_id"],
    )
    op.create_index(
        "ix_landscape_opportunity_versions_cut_id", "landscape_opportunity_versions", ["cut_id"],
    )
    op.create_index(
        "ix_landscape_opportunity_versions_batch_item_id",
        "landscape_opportunity_versions", ["batch_item_id"],
    )
    op.create_table(
        "landscape_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("opportunity_version", sa.Integer(), nullable=True),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "grouping_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_candidate_groupings.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("company_or_project", sa.Text(), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("key_points", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("elevator_pitch", sa.Text(), nullable=True),
        sa.Column("score_rationale", sa.Text(), nullable=True),
        sa.Column("dimension_rationales", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("gating_rationales", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("strengths", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("risks", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("competitive_landscape", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("evidence_maturity", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("funnel_stage", sa.String(20), nullable=True),
        sa.Column("recommendation", sa.String(30), nullable=True),
        sa.Column("confidence", sa.String(20), nullable=True),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("band", sa.String(20), nullable=True),
        sa.Column("gating", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("scores", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("derisking_milestones", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("prose_format", sa.String(20), nullable=True),
        sa.Column("recommended_next_experiment", sa.Text(), nullable=True),
        sa.Column("raw_verdict", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("rubric_version", sa.String(20), nullable=True),
        sa.Column("rubric_content_hash", sa.String(20), nullable=True),
        sa.Column("claim_support", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("member_notes", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("structured_lines", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "opportunity_version", "epoch_id",
            name="uq_landscape_assessments_opportunity_version_epoch",
        ),
        sa.UniqueConstraint(
            "epoch_id", "grouping_id",
            name="uq_landscape_assessments_epoch_grouping",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_assessments_status",
        ),
        sa.CheckConstraint(
            "(opportunity_id IS NULL) = (opportunity_version IS NULL) AND (opportunity_id "
            "IS NOT NULL OR grouping_id IS NOT NULL)",
            name="ck_landscape_assessments_one_key",
        ),
    )
    op.create_index("ix_landscape_assessments_status", "landscape_assessments", ["status"])
    op.create_index(
        "ix_landscape_assessments_grouping_id", "landscape_assessments", ["grouping_id"],
    )
    op.create_index(
        "ix_landscape_assessments_batch_item_id", "landscape_assessments", ["batch_item_id"],
    )


def _create_decisions_and_reviews() -> None:
    """Decisions, reviews, events and assignments: no epoch."""
    op.create_table(
        "landscape_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "reviewer_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reviewer_name", sa.String(255), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("reason_tags", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "landscape_assessment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "recorded_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "reviewer_id",
            name="uq_landscape_decisions_opportunity_reviewer",
        ),
        sa.CheckConstraint(
            "decision IN ('pursue', 'pass')",
            name="ck_landscape_decisions_decision",
        ),
        sa.CheckConstraint(
            "char_length(reason) <= 1000",
            name="ck_landscape_decisions_reason_length",
        ),
    )
    op.create_index("ix_landscape_decisions_reviewer_id", "landscape_decisions", ["reviewer_id"])
    op.create_index(
        "ix_landscape_decisions_landscape_assessment_id",
        "landscape_decisions", ["landscape_assessment_id"],
    )
    op.create_index(
        "ix_landscape_decisions_recorded_by_user_id",
        "landscape_decisions", ["recorded_by_user_id"],
    )
    op.create_table(
        "landscape_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "reviewer_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reviewer_name", sa.String(255), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("dimension_scores", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("rubric_version", sa.String(20), nullable=True),
        sa.Column("rubric_content_hash", sa.String(20), nullable=True),
        sa.Column(
            "landscape_assessment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "recorded_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "reviewer_id",
            name="uq_landscape_reviews_opportunity_reviewer",
        ),
        sa.CheckConstraint(
            "score >= 1 AND score <= 5",
            name="ck_landscape_reviews_score",
        ),
        sa.CheckConstraint(
            "char_length(comment) <= 10000",
            name="ck_landscape_reviews_comment_length",
        ),
    )
    op.create_index("ix_landscape_reviews_reviewer_id", "landscape_reviews", ["reviewer_id"])
    op.create_index(
        "ix_landscape_reviews_landscape_assessment_id",
        "landscape_reviews", ["landscape_assessment_id"],
    )
    op.create_index(
        "ix_landscape_reviews_recorded_by_user_id", "landscape_reviews", ["recorded_by_user_id"],
    )
    op.create_table(
        "landscape_review_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "landscape_assessment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("actor_name", sa.String(255), nullable=False),
        sa.Column(
            "recorded_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('approved', 'disapproved', 'cleared')",
            name="ck_landscape_review_events_action",
        ),
    )
    op.create_index(
        "ix_landscape_review_events_opportunity_id", "landscape_review_events", ["opportunity_id"],
    )
    op.create_index(
        "ix_landscape_review_events_landscape_assessment_id",
        "landscape_review_events", ["landscape_assessment_id"],
    )
    op.create_index("ix_landscape_review_events_actor_id", "landscape_review_events", ["actor_id"])
    op.create_index(
        "ix_landscape_review_events_recorded_by_user_id",
        "landscape_review_events", ["recorded_by_user_id"],
    )
    op.create_table(
        "landscape_review_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "assignee_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("assignee_name", sa.String(255), nullable=False),
        sa.Column(
            "assigned_by_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("assigned_by_name", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "assignee_id",
            name="uq_landscape_review_assignments_opportunity_assignee",
        ),
    )
    op.create_index(
        "ix_landscape_review_assignments_assignee_id",
        "landscape_review_assignments", ["assignee_id"],
    )
    op.create_index(
        "ix_landscape_review_assignments_assigned_by_user_id",
        "landscape_review_assignments", ["assigned_by_user_id"],
    )


def _create_review_batches() -> None:
    """Weekly review batches and expert judgments: no epoch."""
    op.create_table(
        "landscape_review_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("blind", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("issued_on", name="uq_landscape_review_batches_issued_on"),
    )
    op.create_table(
        "landscape_review_batch_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "batch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_review_batches.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("is_probe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "batch_id", "opportunity_id",
            name="uq_landscape_review_batch_items_batch_opportunity",
        ),
    )
    op.create_index(
        "ix_landscape_review_batch_items_opportunity_id",
        "landscape_review_batch_items", ["opportunity_id"],
    )
    op.create_table(
        "landscape_expert_judgments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_opportunities.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "expert_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("expert_name", sa.String(255), nullable=False),
        sa.Column("quarter", sa.String(7), nullable=False),
        sa.Column(
            "landscape_assessment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_assessments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("issued_before_draw", sa.Boolean(), nullable=True),
        sa.Column("gates", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("scores", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("deal_breakers", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("recommendation", sa.String(30), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "drawn_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "expert_id", "quarter",
            name="uq_landscape_expert_judgments_opportunity_expert_quarter",
        ),
    )
    op.create_index(
        "ix_landscape_expert_judgments_expert_id", "landscape_expert_judgments", ["expert_id"],
    )
    op.create_index(
        "ix_landscape_expert_judgments_landscape_assessment_id",
        "landscape_expert_judgments", ["landscape_assessment_id"],
    )


def _create_audits_ledger_reports() -> None:
    """Reject audits, rescues, the LLM ledger and reports."""
    op.create_table(
        "landscape_reject_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("item_kind", sa.String(20), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", sa.String(60), nullable=False, server_default="pending"),
        sa.Column(
            "batch_item_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batch_items.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated_once", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_from", sa.String(100), nullable=True),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("original_decision", sa.String(60), nullable=True),
        sa.Column("audit_decision", sa.String(60), nullable=True),
        sa.Column("agree", sa.Boolean(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "item_kind", "item_id", "epoch_id",
            name="uq_landscape_reject_audits_item_epoch",
        ),
        sa.CheckConstraint(
            _STAGE_STATUS_CHECK,
            name="ck_landscape_reject_audits_status",
        ),
    )
    op.create_index("ix_landscape_reject_audits_status", "landscape_reject_audits", ["status"])
    op.create_index("ix_landscape_reject_audits_epoch_id", "landscape_reject_audits", ["epoch_id"])
    op.create_index(
        "ix_landscape_reject_audits_batch_item_id", "landscape_reject_audits", ["batch_item_id"],
    )
    op.create_table(
        "landscape_rescues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("item_kind", sa.String(20), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column(
            "epoch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_epochs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("actor_name", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("carried", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_landscape_rescues_item", "landscape_rescues", ["item_kind", "item_id"])
    op.create_index("ix_landscape_rescues_epoch_id", "landscape_rescues", ["epoch_id"])
    op.create_index("ix_landscape_rescues_actor_id", "landscape_rescues", ["actor_id"])
    op.create_table(
        "landscape_llm_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column(
            "batch_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("landscape_batches.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("item_seq", sa.Integer(), nullable=True),
        sa.Column("custom_id", sa.String(64), nullable=True),
        sa.Column("item_ref", sa.String(200), nullable=True),
        sa.Column("batch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("batch_id", "item_seq", name="uq_landscape_llm_calls_batch_seq"),
    )
    op.create_index("ix_landscape_llm_calls_created_at", "landscape_llm_calls", ["created_at"])
    op.create_table(
        "landscape_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("kind", "created_at", name="uq_landscape_reports_kind_created"),
        sa.CheckConstraint(
            "kind IN ('bakeoff', 'bakeoff_request', 'probes', 'pilot', 'completeness', "
            "'drift', 'positive_controls', 'metrics')",
            name="ck_landscape_reports_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("landscape_reports")
    op.drop_table("landscape_llm_calls")
    op.drop_table("landscape_rescues")
    op.drop_table("landscape_reject_audits")
    op.drop_table("landscape_expert_judgments")
    op.drop_table("landscape_review_batch_items")
    op.drop_table("landscape_review_batches")
    op.drop_table("landscape_review_assignments")
    op.drop_table("landscape_review_events")
    op.drop_table("landscape_reviews")
    op.drop_table("landscape_decisions")
    op.drop_table("landscape_assessments")
    op.drop_table("landscape_opportunity_versions")
    op.drop_table("landscape_opportunities")
    op.drop_table("landscape_similarity")
    op.drop_table("landscape_rankings")
    op.drop_table("landscape_comparisons")
    op.drop_table("landscape_consults")
    op.drop_table("landscape_deep")
    op.drop_table("landscape_candidate_groupings")
    op.drop_table("landscape_cuts")
    op.drop_table("landscape_triage")
    op.drop_table("landscape_idea_aliases")
    op.drop_table("landscape_idea_evidence")
    op.drop_table("landscape_extraction_links")
    op.drop_table("landscape_idea_links")
    op.drop_table("landscape_ideas")
    op.drop_table("landscape_extractions")
    op.drop_table("landscape_screens")
    op.drop_table("landscape_regression_runs")
    op.drop_table("landscape_regression_items")
    op.drop_table("landscape_epochs")
    op.drop_table("landscape_regression_sets")
    op.drop_table("landscape_batch_items")
    op.drop_table("landscape_batches")
    op.drop_table("landscape_texts")
    op.drop_table("landscape_document_links")
    op.drop_table("landscape_documents")
    op.drop_table("landscape_evidence_cache")
    op.drop_table("landscape_labs")
    op.drop_table("landscape_fetches")
    op.drop_table("landscape_sources")
