#!/usr/bin/env python3
"""Post-migration verification for an upgrade to --target (default DEFAULT_TARGET).

Run this AFTER `alembic upgrade`, against the database you just migrated.

    docker compose -f docker-compose.prod.yml run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app python scripts/migrate/postflight.py --snapshot /app/backups/preflight_snapshot.json

What follows --target and what does not:

  * The revision stamp, the enum check and the row-count comparison follow it. The
    enum check expects only the types created at or before --target, and the row-count
    comparison derives the tables legitimately created or dropped from the snapshot's
    own ``current_revision`` to --target (preflight's ``tables_created_between`` /
    ``tables_dropped_between``), so a table any revision in that span creates must exist
    afterwards. The snapshot must name the same database (host, port, name) as this run
    or the comparison FAILs; a snapshot ``target`` other than --target only WARNs.
  * The pinned table/column/index/constraint expectations (EXPECTED_TABLES,
    EXPECTED_COLUMNS, EXPECTED_INDEXES, EXPECTED_CONSTRAINTS) describe the 0019-0023
    chain and the 0056, 0062, 0065, 0068, 0069, 0070, 0071, 0072, 0073, 0075, 0076 and 0077 objects only (VERIFIED_REVISIONS), whatever the target.

Exit codes (contract):

    0  verified
    1  verification FAILED

Why this script exists at all, given that `alembic upgrade` exits 0 on success:
because `alembic upgrade` exiting 0 does not mean the schema changed.

  * A duplicate revision id makes a targeted ``upgrade <rev>`` apply whichever file
    sorts last while stamping the database as fully migrated. This repo has had two
    files claiming ``revision = "0019"`` (``0019_agent_message_content.py`` and, on
    branch cohort-agent-isolation, ``0019_add_cohorts.py``).
  * If ``alembic/env.py`` emits SQL on the connection before
    ``context.begin_transaction()``, alembic hands the commit back to the caller,
    which never commits: the log shows the whole chain "Running upgrade ..." and the
    database is left untouched. Reproduced on this tree.

So the version stamp is evidence of nothing on its own. Every check below looks at
the actual catalog, the actual data, or the actual ORM.

Warnings are reported but do not fail the run (exit stays 0); only a FAIL exits 1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # See the identical note in preflight.py: without this, `import src...` resolves to
    # the stale copy baked into site-packages by the Dockerfile's `pip install .`.
    sys.path.insert(0, str(REPO_ROOT))


def _load_preflight():
    """Import preflight.py by path.

    scripts/ is not a package (no __init__.py anywhere in it), so a plain
    `from preflight import ...` only works when CWD happens to be scripts/migrate.
    """
    name = "copi_migrate_preflight"
    already = sys.modules.get(name)
    if already is not None:
        # Idempotent: return the module that is already registered rather than exec'ing a
        # second copy. Two live copies would leave preflight's @dataclass types resolving
        # their annotations against the OTHER copy's globals.
        return already
    path = Path(__file__).resolve().parent / "preflight.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec_module, not after: @dataclass resolves annotations through
    # sys.modules[cls.__module__], and preflight.py defines three dataclasses.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pf = _load_preflight()

PASS = _pf.PASS
WARN = _pf.WARN
FAIL = _pf.BLOCK  # same token, read as "verification failed" here

DEFAULT_TARGET = _pf.DEFAULT_TARGET
Report = _pf.Report
compare_row_counts = _pf.compare_row_counts
fetch_all = _pf.fetch_all
fetch_one_value = _pf.fetch_one_value
current_revision = _pf.current_revision
open_connection = _pf.open_connection
redact_url = _pf.redact_url
resolve_database_url = _pf.resolve_database_url
table_exists = _pf.table_exists
add_common_arguments = _pf.add_common_arguments
check_alembic_scripts = _pf.check_alembic_scripts

# ---------------------------------------------------------------------------
# What "migrated to 0023" actually means, object by object.
# Captured from a database that reached 0023 through the real chain, then pinned here.
# ---------------------------------------------------------------------------

#: (table, column, data_type, is_nullable, column_default)
#: column_default None means "any default is acceptable"; '' means "must have none".
EXPECTED_COLUMNS: tuple[tuple[str, str, str, bool, str | None], ...] = (
    # 0019 content columns. NOT NULL with a server_default is what makes 0019 fast on a
    # big table (Postgres 11+ fills a non-volatile default without a rewrite) AND what
    # makes every legacy row read as an empty message.
    ("agent_messages", "content", "text", False, "''::text"),
    ("agent_messages", "sender_name", "character varying", False, "''::character varying"),
    ("agent_messages", "is_bot", "boolean", False, "true"),
    ("agent_messages", "posted_at", "double precision", False, "'0'::double precision"),
    ("agent_messages", "slack_ts", "character varying", True, ""),
    ("agent_messages", "slack_channel_id", "character varying", True, ""),
    ("agent_messages", "slack_thread_ts", "character varying", True, ""),
    # 0019 RELAXES this one. If it is still NOT NULL, 0019 did not really run.
    ("agent_messages", "agent_id", "character varying", True, ""),
    # 0020
    ("pi_dm_messages", "id", "uuid", False, None),
    ("pi_dm_messages", "simulation_run_id", "uuid", False, None),
    ("pi_dm_messages", "agent_id", "character varying", False, None),
    ("pi_dm_messages", "pi_user_id", "character varying", False, None),
    ("pi_dm_messages", "direction", "USER-DEFINED", False, None),
    ("pi_dm_messages", "content", "text", False, None),
    ("pi_dm_messages", "sender_name", "character varying", False, "''::character varying"),
    ("pi_dm_messages", "ts", "character varying", False, None),
    ("pi_dm_messages", "slack_ts", "character varying", True, ""),
    ("pi_dm_messages", "posted_at", "double precision", False, "'0'::double precision"),
    ("pi_dm_messages", "created_at", "timestamp with time zone", False, "now()"),
    # 0023 — deliberately nullable and deliberately NOT backfilled. NULL means
    # "this row predates the columns"; a non-null value here would be invented provenance.
    ("researcher_profiles", "synthesis_validated", "boolean", True, ""),
    ("researcher_profiles", "evidence_pmid_count", "integer", True, ""),
    ("researcher_profiles", "evidence_pub_count", "integer", True, ""),
    # 0056 — NULL means "enqueued before 0056 or without a stated priority"; never backfilled.
    ("jobs", "priority", "smallint", True, ""),
    # 0062 (publications provenance and exclusion; profile drafts).
    ("publications", "provenance", "character varying", True, ""),
    ("publications", "excluded_at", "timestamp with time zone", True, ""),
    ("publications", "excluded_by_user_id", "uuid", True, ""),
    ("publications", "doi_verified", "boolean", True, ""),
    # 0068 — NULL on in-use rows and on rows excluded before 0068; never backfilled.
    ("publications", "excluded_reason", "character varying", True, ""),
    ("researcher_profiles", "human_edited_at", "timestamp with time zone", True, ""),
    ("researcher_profiles", "evidence_flagged_count", "integer", True, ""),
    ("agents", "persona_export_failed_at", "timestamp with time zone", True, ""),
    # 0069: NULL means "written before 0069" (or an agent with no user); never backfilled.
    ("profile_revisions", "owner_user_id", "uuid", True, ""),
    # 0070 — authorship_claim_checks (C7). No backfill: no row means "never checked".
    ("authorship_claim_checks", "id", "uuid", False, None),
    ("authorship_claim_checks", "simulation_run_id", "uuid", True, ""),
    ("authorship_claim_checks", "agent_id", "character varying", False, ""),
    ("authorship_claim_checks", "thread_key", "character varying", False, ""),
    ("authorship_claim_checks", "phase", "character varying", False, ""),
    ("authorship_claim_checks", "mode", "character varying", False, ""),
    ("authorship_claim_checks", "outcome", "character varying", False, ""),
    ("authorship_claim_checks", "reasons", "jsonb", False, "'[]'::jsonb"),
    ("authorship_claim_checks", "excerpt", "text", False, ""),
    ("authorship_claim_checks", "created_at", "timestamp with time zone", False, "now()"),
    # 0071 — hub_declines (owner decision 2026-10-16). The migration writes no row; past
    # declines are recorded by scripts/backfill_hub_declines.py, run by an operator.
    ("hub_declines", "id", "uuid", False, None),
    ("hub_declines", "simulation_run_id", "uuid", False, ""),
    ("hub_declines", "agent_id", "character varying", False, ""),
    ("hub_declines", "subject_agent_id", "character varying", True, ""),
    ("hub_declines", "channel_name", "character varying", False, ""),
    ("hub_declines", "thread_id", "character varying", False, ""),
    ("hub_declines", "slack_ts", "character varying", True, ""),
    ("hub_declines", "reason_text", "text", False, ""),
    ("hub_declines", "created_at", "timestamp with time zone", False, "now()"),
    ("hub_declines", "summary_claimed_at", "timestamp with time zone", True, ""),
    ("hub_declines", "summary_posted_at", "timestamp with time zone", True, ""),
    # 0072 — interview_trajectories. The migration writes no row; the worker back-fills past
    # runs under INTERVIEW_TRAJECTORIES_DAILY_USD_LIMIT.
    ("interview_trajectories", "id", "uuid", False, None),
    ("interview_trajectories", "simulation_run_id", "uuid", False, ""),
    ("interview_trajectories", "thread_id", "character varying", False, ""),
    ("interview_trajectories", "status", "character varying", False, ""),
    ("interview_trajectories", "lines", "jsonb", True, ""),
    ("interview_trajectories", "error_code", "character varying", True, ""),
    ("interview_trajectories", "attempts", "integer", False, "1"),
    ("interview_trajectories", "model", "character varying", False, ""),
    ("interview_trajectories", "served_by_model", "character varying", True, ""),
    ("interview_trajectories", "prompt_sha256_12", "character varying", False, ""),
    ("interview_trajectories", "posts_sha256_12", "character varying", False, ""),
    ("interview_trajectories", "usage_by_model", "jsonb", True, ""),
    ("interview_trajectories", "latency_ms", "integer", True, ""),
    ("interview_trajectories", "created_at", "timestamp with time zone", False, "now()"),
    ("interview_trajectories", "updated_at", "timestamp with time zone", False, "now()"),
    # 0073 — the projects prototype's four tables. The migration writes no row; NULL means
    # "never asked" (not yet researched, posted or finished).
    ("hub_projects", "id", "uuid", False, None),
    ("hub_projects", "simulation_run_id", "uuid", False, ""),
    ("hub_projects", "agent_id", "character varying", False, ""),
    ("hub_projects", "slot", "integer", False, ""),
    ("hub_projects", "status", "character varying", False, ""),
    ("hub_projects", "reason_code", "character varying", True, ""),
    ("hub_projects", "requested_run_seconds", "double precision", False, ""),
    ("hub_projects", "requested_at", "timestamp with time zone", False, "now()"),
    ("hub_projects", "research", "jsonb", True, ""),
    ("hub_projects", "choice", "jsonb", True, ""),
    ("hub_projects", "title", "character varying", True, ""),
    ("hub_projects", "posting_text", "text", True, ""),
    ("hub_projects", "posting_claimed_at", "timestamp with time zone", True, ""),
    ("hub_projects", "posting_ts", "character varying", True, ""),
    ("hub_projects", "posted_at", "timestamp with time zone", True, ""),
    ("hub_projects", "reply_claimed_at", "timestamp with time zone", True, ""),
    ("hub_projects", "reply_ts", "character varying", True, ""),
    ("hub_projects", "reply_failures", "integer", False, "0"),
    ("hub_projects", "final", "jsonb", True, ""),
    ("hub_projects", "finished_at", "timestamp with time zone", True, ""),
    ("hub_projects", "finished_run_seconds", "double precision", True, ""),
    ("hub_projects", "next_step_after", "timestamp with time zone", True, ""),
    ("hub_projects", "created_at", "timestamp with time zone", False, "now()"),
    ("hub_projects", "updated_at", "timestamp with time zone", False, "now()"),
    ("hub_project_calls", "id", "uuid", False, None),
    ("hub_project_calls", "project_id", "uuid", False, ""),
    ("hub_project_calls", "step", "character varying", False, ""),
    ("hub_project_calls", "model", "character varying", False, ""),
    ("hub_project_calls", "served_by_model", "character varying", True, ""),
    ("hub_project_calls", "status", "character varying", False, ""),
    ("hub_project_calls", "error_code", "character varying", True, ""),
    ("hub_project_calls", "usage_by_model", "jsonb", True, ""),
    ("hub_project_calls", "latency_ms", "integer", True, ""),
    ("hub_project_calls", "created_at", "timestamp with time zone", False, "now()"),
    ("hub_project_calls", "updated_at", "timestamp with time zone", False, "now()"),
    ("hub_project_clocks", "id", "uuid", False, None),
    ("hub_project_clocks", "simulation_run_id", "uuid", False, ""),
    ("hub_project_clocks", "run_seconds", "double precision", False, "'0'::double precision"),
    ("hub_project_clocks", "updated_at", "timestamp with time zone", False, "now()"),
    ("hub_project_reviews", "id", "uuid", False, None),
    ("hub_project_reviews", "project_id", "uuid", False, ""),
    ("hub_project_reviews", "reviewer_user_id", "uuid", True, ""),
    ("hub_project_reviews", "reviewer_name", "character varying", False, ""),
    ("hub_project_reviews", "rating", "smallint", False, ""),
    ("hub_project_reviews", "comment", "text", False, "''::text"),
    ("hub_project_reviews", "fund", "boolean", False, ""),
    ("hub_project_reviews", "created_at", "timestamp with time zone", False, "now()"),
    ("hub_project_reviews", "updated_at", "timestamp with time zone", False, "now()"),
    # 0075 — hub_project_review_assignments. The migration writes no row.
    ("hub_project_review_assignments", "id", "uuid", False, None),
    ("hub_project_review_assignments", "project_id", "uuid", False, ""),
    ("hub_project_review_assignments", "assignee_user_id", "uuid", False, ""),
    ("hub_project_review_assignments", "assignee_name", "character varying", False, ""),
    ("hub_project_review_assignments", "assigned_by_user_id", "uuid", True, ""),
    ("hub_project_review_assignments", "assigned_by_name", "character varying", False, ""),
    ("hub_project_review_assignments", "created_at", "timestamp with time zone", False, "now()"),
    # 0076 — the chat tables' project subject. NULL subject means "written before 0076"
    # (every such row an assessment); never back-filled.
    ("assessment_chat_turns", "project_id", "uuid", True, ""),
    ("assessment_chat_usage", "project_id", "uuid", True, ""),
    ("assessment_chat_usage", "subject", "character varying", True, ""),
    ("assessment_chat_opens", "project_id", "uuid", True, ""),
    ("assessment_chat_opens", "subject", "character varying", True, ""),
    # 0076 RELAXES this one (a project turn has none). If it is still NOT NULL, 0076 did not
    # really run.
    ("assessment_chat_turns", "assessment_id", "uuid", True, ""),
    # 0077 — the 42 landscape_* tables, every column. The migration writes no row.
    ("landscape_sources", "id", "uuid", False, None),
    ("landscape_sources", "source", "character varying", False, ""),
    ("landscape_sources", "enabled", "boolean", False, "true"),
    ("landscape_sources", "watermark", "jsonb", True, ""),
    ("landscape_sources", "last_success_at", "timestamp with time zone", True, ""),
    ("landscape_sources", "last_completeness_ratio", "double precision", True, ""),
    ("landscape_sources", "next_due_at", "timestamp with time zone", True, ""),
    ("landscape_sources", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_sources", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_fetches", "id", "uuid", False, None),
    ("landscape_fetches", "source", "character varying", False, ""),
    ("landscape_fetches", "slice_key", "character varying", False, ""),
    ("landscape_fetches", "started_at", "timestamp with time zone", False, "now()"),
    ("landscape_fetches", "finished_at", "timestamp with time zone", True, ""),
    ("landscape_fetches", "expected_count", "integer", True, ""),
    ("landscape_fetches", "received_count", "integer", True, ""),
    ("landscape_fetches", "status", "character varying", False, "'running'::character varying"),
    ("landscape_fetches", "attempts", "integer", False, "0"),
    ("landscape_fetches", "error", "text", True, ""),
    ("landscape_labs", "id", "uuid", False, None),
    ("landscape_labs", "lab_key", "character varying", False, ""),
    ("landscape_labs", "display_name", "character varying", True, ""),
    ("landscape_labs", "orcid", "character varying", True, ""),
    ("landscape_labs", "openalex_author_id", "character varying", True, ""),
    ("landscape_labs", "reporter_profile_id", "character varying", True, ""),
    ("landscape_labs", "aliases", "jsonb", True, ""),
    ("landscape_labs", "alias_of", "character varying", True, ""),
    ("landscape_labs", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_labs", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_evidence_cache", "id", "uuid", False, None),
    ("landscape_evidence_cache", "source", "character varying", False, ""),
    ("landscape_evidence_cache", "release", "character varying", False, ""),
    ("landscape_evidence_cache", "query_key", "character varying", False, ""),
    ("landscape_evidence_cache", "payload", "jsonb", True, ""),
    ("landscape_evidence_cache", "fetched_at", "timestamp with time zone", False, "now()"),
    ("landscape_documents", "id", "uuid", False, None),
    ("landscape_documents", "kind", "character varying", False, ""),
    ("landscape_documents", "canonical_external_id", "character varying", False, ""),
    ("landscape_documents", "merged_into", "uuid", True, ""),
    ("landscape_documents", "doi", "character varying", True, ""),
    ("landscape_documents", "pmid", "character varying", True, ""),
    ("landscape_documents", "pmcid", "character varying", True, ""),
    ("landscape_documents", "openalex_id", "character varying", True, ""),
    ("landscape_documents", "external_ids", "jsonb", True, ""),
    ("landscape_documents", "title", "text", True, ""),
    ("landscape_documents", "summary_text", "text", True, ""),
    ("landscape_documents", "summary_source", "character varying", True, ""),
    ("landscape_documents", "summary_hash", "character varying", True, ""),
    ("landscape_documents", "published_on", "date", True, ""),
    ("landscape_documents", "dates", "jsonb", True, ""),
    ("landscape_documents", "document_type", "character varying", True, ""),
    ("landscape_documents", "language", "character varying", True, ""),
    ("landscape_documents", "license", "character varying", True, ""),
    ("landscape_documents", "is_oa", "boolean", True, ""),
    ("landscape_documents", "oa_flags", "jsonb", True, ""),
    ("landscape_documents", "is_retracted", "boolean", True, ""),
    ("landscape_documents", "details", "jsonb", True, ""),
    ("landscape_documents", "qualifying_authorships", "jsonb", True, ""),
    ("landscape_documents", "qualifying_institution", "character varying", True, ""),
    ("landscape_documents", "role_basis", "character varying", True, ""),
    ("landscape_documents", "lab_key", "character varying", True, ""),
    ("landscape_documents", "s1_status", "character varying", False, ""),
    ("landscape_documents", "s1_reason", "text", True, ""),
    ("landscape_documents", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_documents", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_document_links", "id", "uuid", False, None),
    ("landscape_document_links", "doc_a", "uuid", False, ""),
    ("landscape_document_links", "doc_b", "uuid", False, ""),
    ("landscape_document_links", "link_kind", "character varying", False, ""),
    ("landscape_document_links", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_texts", "id", "uuid", False, None),
    ("landscape_texts", "document_id", "uuid", False, ""),
    ("landscape_texts", "text_gz", "bytea", True, ""),
    ("landscape_texts", "sections", "jsonb", True, ""),
    ("landscape_texts", "text_basis", "character varying", False, ""),
    ("landscape_texts", "fetch_attempts", "integer", False, "0"),
    ("landscape_texts", "license", "character varying", True, ""),
    ("landscape_texts", "route", "character varying", True, ""),
    ("landscape_texts", "truncated", "boolean", False, "false"),
    ("landscape_texts", "fetched_at", "timestamp with time zone", True, ""),
    ("landscape_texts", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_texts", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_batches", "id", "uuid", False, None),
    ("landscape_batches", "seq", "bigint", False, ""),
    ("landscape_batches", "api_batch_id", "character varying", True, ""),
    ("landscape_batches", "stage", "character varying", False, ""),
    ("landscape_batches", "model", "character varying", False, ""),
    ("landscape_batches", "row_table", "character varying", False, ""),
    ("landscape_batches", "status", "character varying", False, "'submitting'::character varying"),
    ("landscape_batches", "create_failures", "integer", False, "0"),
    ("landscape_batches", "request_count", "integer", False, "0"),
    ("landscape_batches", "submitted_at", "timestamp with time zone", True, ""),
    ("landscape_batches", "ended_at", "timestamp with time zone", True, ""),
    ("landscape_batches", "ingested_at", "timestamp with time zone", True, ""),
    ("landscape_batches", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_batches", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_batch_items", "id", "uuid", False, None),
    ("landscape_batch_items", "batch_id", "uuid", False, ""),
    ("landscape_batch_items", "item_seq", "integer", False, ""),
    ("landscape_batch_items", "row_id", "uuid", False, ""),
    ("landscape_batch_items", "result_kind", "character varying", True, ""),
    ("landscape_batch_items", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_regression_sets", "id", "uuid", False, None),
    ("landscape_regression_sets", "version", "integer", False, ""),
    ("landscape_regression_sets", "notes", "text", True, ""),
    ("landscape_regression_sets", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_epochs", "id", "uuid", False, None),
    ("landscape_epochs", "status", "character varying", False, "'candidate'::character varying"),
    ("landscape_epochs", "components", "jsonb", True, ""),
    ("landscape_epochs", "snapshot", "jsonb", True, ""),
    ("landscape_epochs", "prompt_set_version", "character varying", True, ""),
    ("landscape_epochs", "criteria_version", "character varying", True, ""),
    ("landscape_epochs", "changed_stages", "jsonb", True, ""),
    ("landscape_epochs", "built_from_epoch_id", "uuid", True, ""),
    ("landscape_epochs", "regression_set_version", "integer", True, ""),
    ("landscape_epochs", "regression_run_id", "uuid", True, ""),
    ("landscape_epochs", "hold_reason", "text", True, ""),
    ("landscape_epochs", "override_actor_id", "uuid", True, ""),
    ("landscape_epochs", "override_reason", "text", True, ""),
    ("landscape_epochs", "override_at", "timestamp with time zone", True, ""),
    ("landscape_epochs", "rejudge_started_at", "timestamp with time zone", True, ""),
    ("landscape_epochs", "drained_at", "timestamp with time zone", True, ""),
    ("landscape_epochs", "activated_at", "timestamp with time zone", True, ""),
    ("landscape_epochs", "retired_at", "timestamp with time zone", True, ""),
    ("landscape_epochs", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_epochs", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_regression_items", "id", "uuid", False, None),
    ("landscape_regression_items", "set_version", "integer", False, ""),
    ("landscape_regression_items", "item_kind", "character varying", False, ""),
    ("landscape_regression_items", "origin", "character varying", False, ""),
    ("landscape_regression_items", "item_ref", "jsonb", True, ""),
    ("landscape_regression_items", "expected", "jsonb", True, ""),
    ("landscape_regression_items", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_regression_runs", "id", "uuid", False, None),
    ("landscape_regression_runs", "epoch_id", "uuid", False, ""),
    ("landscape_regression_runs", "set_version", "integer", False, ""),
    ("landscape_regression_runs", "started_at", "timestamp with time zone", False, "now()"),
    ("landscape_regression_runs", "finished_at", "timestamp with time zone", True, ""),
    (
        "landscape_regression_runs", "status", "character varying", False, "'running'::character varying",
    ),
    ("landscape_regression_runs", "results", "jsonb", True, ""),
    ("landscape_screens", "id", "uuid", False, None),
    ("landscape_screens", "document_id", "uuid", False, ""),
    ("landscape_screens", "epoch_id", "uuid", False, ""),
    ("landscape_screens", "status", "character varying", False, "'pending'::character varying"),
    ("landscape_screens", "batch_item_id", "uuid", True, ""),
    ("landscape_screens", "attempts", "integer", False, "0"),
    ("landscape_screens", "truncated_once", "boolean", False, "false"),
    ("landscape_screens", "fallback_from", "character varying", True, ""),
    ("landscape_screens", "served_by_model", "character varying", True, ""),
    ("landscape_screens", "refusal_category", "character varying", True, ""),
    ("landscape_screens", "error", "text", True, ""),
    ("landscape_screens", "decision", "character varying", True, ""),
    ("landscape_screens", "kinds", "jsonb", True, ""),
    ("landscape_screens", "mentions", "jsonb", True, ""),
    ("landscape_screens", "reason", "text", True, ""),
    ("landscape_screens", "summary_hash", "character varying", True, ""),
    ("landscape_screens", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_screens", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_extractions", "id", "uuid", False, None),
    ("landscape_extractions", "document_id", "uuid", False, ""),
    ("landscape_extractions", "epoch_id", "uuid", False, ""),
    ("landscape_extractions", "basis", "character varying", False, ""),
    ("landscape_extractions", "status", "character varying", False, "'pending'::character varying"),
    ("landscape_extractions", "batch_item_id", "uuid", True, ""),
    ("landscape_extractions", "attempts", "integer", False, "0"),
    ("landscape_extractions", "truncated_once", "boolean", False, "false"),
    ("landscape_extractions", "fallback_from", "character varying", True, ""),
    ("landscape_extractions", "served_by_model", "character varying", True, ""),
    ("landscape_extractions", "refusal_category", "character varying", True, ""),
    ("landscape_extractions", "error", "text", True, ""),
    ("landscape_extractions", "raw", "jsonb", True, ""),
    ("landscape_extractions", "quotes_total", "integer", True, ""),
    ("landscape_extractions", "quotes_verified", "integer", True, ""),
    ("landscape_extractions", "summary_hash", "character varying", True, ""),
    ("landscape_extractions", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_extractions", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_ideas", "id", "uuid", False, None),
    ("landscape_ideas", "epoch_id", "uuid", False, ""),
    ("landscape_ideas", "idea_key", "character varying", False, ""),
    ("landscape_ideas", "idea_type", "character varying", False, ""),
    ("landscape_ideas", "provisional", "boolean", False, "false"),
    ("landscape_ideas", "targets", "jsonb", True, ""),
    ("landscape_ideas", "direction", "character varying", True, ""),
    ("landscape_ideas", "agent", "jsonb", True, ""),
    ("landscape_ideas", "disease", "jsonb", True, ""),
    ("landscape_ideas", "normalization", "jsonb", True, ""),
    ("landscape_ideas", "modality_variants", "jsonb", True, ""),
    ("landscape_ideas", "stage", "character varying", True, ""),
    ("landscape_ideas", "evidence_grade", "character varying", True, ""),
    ("landscape_ideas", "jhu_group_count", "integer", False, "0"),
    ("landscape_ideas", "outside_group_count", "integer", True, ""),
    ("landscape_ideas", "evidence_summary", "jsonb", True, ""),
    ("landscape_ideas", "data_flags", "jsonb", True, ""),
    ("landscape_ideas", "evidence_status", "jsonb", True, ""),
    ("landscape_ideas", "evidence_assembled_at", "timestamp with time zone", True, ""),
    ("landscape_ideas", "version_hash", "character varying", True, ""),
    ("landscape_ideas", "in_current_cut", "boolean", False, "false"),
    ("landscape_ideas", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_ideas", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_idea_links", "id", "uuid", False, None),
    ("landscape_idea_links", "epoch_id", "uuid", False, ""),
    ("landscape_idea_links", "idea_a", "uuid", False, ""),
    ("landscape_idea_links", "idea_b", "uuid", False, ""),
    ("landscape_idea_links", "document_id", "uuid", True, ""),
    ("landscape_idea_links", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_extraction_links", "id", "uuid", False, None),
    ("landscape_extraction_links", "epoch_id", "uuid", False, ""),
    ("landscape_extraction_links", "doc_a", "uuid", False, ""),
    ("landscape_extraction_links", "doc_b", "uuid", False, ""),
    ("landscape_extraction_links", "extraction_id", "uuid", True, ""),
    ("landscape_extraction_links", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_idea_evidence", "id", "uuid", False, None),
    ("landscape_idea_evidence", "idea_id", "uuid", False, ""),
    ("landscape_idea_evidence", "extraction_id", "uuid", False, ""),
    ("landscape_idea_evidence", "candidate_index", "integer", False, ""),
    ("landscape_idea_evidence", "merge_reason", "character varying", True, ""),
    ("landscape_idea_evidence", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_idea_aliases", "id", "uuid", False, None),
    ("landscape_idea_aliases", "epoch_id", "uuid", False, ""),
    ("landscape_idea_aliases", "old_key", "character varying", False, ""),
    ("landscape_idea_aliases", "idea_id", "uuid", False, ""),
    ("landscape_idea_aliases", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_triage", "id", "uuid", False, None),
    ("landscape_triage", "idea_id", "uuid", False, ""),
    ("landscape_triage", "idea_version", "character varying", False, ""),
    ("landscape_triage", "epoch_id", "uuid", False, ""),
    ("landscape_triage", "status", "character varying", False, "'pending'::character varying"),
    ("landscape_triage", "batch_item_id", "uuid", True, ""),
    ("landscape_triage", "attempts", "integer", False, "0"),
    ("landscape_triage", "truncated_once", "boolean", False, "false"),
    ("landscape_triage", "fallback_from", "character varying", True, ""),
    ("landscape_triage", "served_by_model", "character varying", True, ""),
    ("landscape_triage", "refusal_category", "character varying", True, ""),
    ("landscape_triage", "error", "text", True, ""),
    ("landscape_triage", "gating", "jsonb", True, ""),
    ("landscape_triage", "gating_rationales", "jsonb", True, ""),
    ("landscape_triage", "scores", "jsonb", True, ""),
    ("landscape_triage", "dimension_rationales", "jsonb", True, ""),
    ("landscape_triage", "target_rubric", "jsonb", True, ""),
    ("landscape_triage", "evidence_grade", "character varying", True, ""),
    ("landscape_triage", "stage", "character varying", True, ""),
    ("landscape_triage", "llm_flags", "jsonb", True, ""),
    ("landscape_triage", "red_flags", "jsonb", True, ""),
    ("landscape_triage", "confidence", "character varying", True, ""),
    ("landscape_triage", "in_therapeutic_scope", "boolean", True, ""),
    ("landscape_triage", "druggability_note", "text", True, ""),
    ("landscape_triage", "triage_score", "double precision", True, ""),
    ("landscape_triage", "evidence_count", "integer", True, ""),
    ("landscape_triage", "exclusion_reason", "text", True, ""),
    ("landscape_triage", "raw", "jsonb", True, ""),
    ("landscape_triage", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_triage", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_cuts", "id", "uuid", False, None),
    ("landscape_cuts", "epoch_id", "uuid", False, ""),
    ("landscape_cuts", "status", "character varying", False, "'complete'::character varying"),
    ("landscape_cuts", "n", "integer", False, ""),
    ("landscape_cuts", "k", "integer", False, ""),
    ("landscape_cuts", "ordered_idea_ids", "jsonb", True, ""),
    ("landscape_cuts", "folds", "jsonb", True, ""),
    ("landscape_cuts", "tie_class_size", "integer", False, "0"),
    ("landscape_cuts", "boundary_batch_id", "uuid", True, ""),
    ("landscape_cuts", "completed_at", "timestamp with time zone", True, ""),
    ("landscape_cuts", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_candidate_groupings", "id", "uuid", False, None),
    ("landscape_candidate_groupings", "epoch_id", "uuid", False, ""),
    ("landscape_candidate_groupings", "cut_id", "uuid", False, ""),
    ("landscape_candidate_groupings", "grouping_key", "character varying", False, ""),
    ("landscape_candidate_groupings", "member_idea_keys", "jsonb", False, ""),
    ("landscape_candidate_groupings", "label", "character varying", True, ""),
    ("landscape_candidate_groupings", "theme", "text", True, ""),
    ("landscape_candidate_groupings", "platform_signal", "jsonb", True, ""),
    ("landscape_candidate_groupings", "edge_kinds", "jsonb", True, ""),
    ("landscape_candidate_groupings", "current", "boolean", False, "true"),
    (
        "landscape_candidate_groupings", "status", "character varying", False, "'pending'::character varying",
    ),
    ("landscape_candidate_groupings", "batch_item_id", "uuid", True, ""),
    ("landscape_candidate_groupings", "attempts", "integer", False, "0"),
    ("landscape_candidate_groupings", "truncated_once", "boolean", False, "false"),
    ("landscape_candidate_groupings", "fallback_from", "character varying", True, ""),
    ("landscape_candidate_groupings", "served_by_model", "character varying", True, ""),
    ("landscape_candidate_groupings", "refusal_category", "character varying", True, ""),
    ("landscape_candidate_groupings", "error", "text", True, ""),
    ("landscape_candidate_groupings", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_candidate_groupings", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_deep", "id", "uuid", False, None),
    ("landscape_deep", "idea_id", "uuid", False, ""),
    ("landscape_deep", "idea_version", "character varying", False, ""),
    ("landscape_deep", "epoch_id", "uuid", False, ""),
    ("landscape_deep", "cut_id", "uuid", True, ""),
    ("landscape_deep", "status", "character varying", False, "'pending'::character varying"),
    ("landscape_deep", "attempts", "integer", False, "0"),
    ("landscape_deep", "truncated_once", "boolean", False, "false"),
    ("landscape_deep", "fallback_from", "character varying", True, ""),
    ("landscape_deep", "served_by_model", "character varying", True, ""),
    ("landscape_deep", "refusal_category", "character varying", True, ""),
    ("landscape_deep", "error", "text", True, ""),
    ("landscape_deep", "full_text_evidence", "jsonb", True, ""),
    ("landscape_deep", "disease_profile", "jsonb", True, ""),
    ("landscape_deep", "target_biology", "jsonb", True, ""),
    ("landscape_deep", "gating", "jsonb", True, ""),
    ("landscape_deep", "gating_rationales", "jsonb", True, ""),
    ("landscape_deep", "flags_reviewed", "jsonb", True, ""),
    ("landscape_deep", "provisional_scores", "jsonb", True, ""),
    ("landscape_deep", "dimension_rationales", "jsonb", True, ""),
    ("landscape_deep", "provisional_weighted_score", "double precision", True, ""),
    ("landscape_deep", "outside_group_count", "integer", True, ""),
    ("landscape_deep", "summary", "text", True, ""),
    ("landscape_deep", "search_transcript", "jsonb", True, ""),
    ("landscape_deep", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_deep", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_consults", "id", "uuid", False, None),
    ("landscape_consults", "deep_id", "uuid", False, ""),
    ("landscape_consults", "domain", "character varying", False, ""),
    ("landscape_consults", "opinion", "jsonb", True, ""),
    ("landscape_consults", "raw_text", "text", True, ""),
    ("landscape_consults", "served_by_model", "character varying", True, ""),
    ("landscape_consults", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_comparisons", "id", "uuid", False, None),
    ("landscape_comparisons", "idea_a", "uuid", False, ""),
    ("landscape_comparisons", "version_a", "character varying", False, ""),
    ("landscape_comparisons", "idea_b", "uuid", False, ""),
    ("landscape_comparisons", "version_b", "character varying", False, ""),
    ("landscape_comparisons", "epoch_id", "uuid", False, ""),
    ("landscape_comparisons", "basis", "character varying", False, ""),
    ("landscape_comparisons", "ask_order", "smallint", False, ""),
    ("landscape_comparisons", "status", "character varying", False, "'pending'::character varying"),
    ("landscape_comparisons", "batch_item_id", "uuid", True, ""),
    ("landscape_comparisons", "attempts", "integer", False, "0"),
    ("landscape_comparisons", "truncated_once", "boolean", False, "false"),
    ("landscape_comparisons", "fallback_from", "character varying", True, ""),
    ("landscape_comparisons", "served_by_model", "character varying", True, ""),
    ("landscape_comparisons", "refusal_category", "character varying", True, ""),
    ("landscape_comparisons", "error", "text", True, ""),
    ("landscape_comparisons", "winner_idea_id", "uuid", True, ""),
    ("landscape_comparisons", "reason", "text", True, ""),
    ("landscape_comparisons", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_comparisons", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_rankings", "id", "uuid", False, None),
    ("landscape_rankings", "epoch_id", "uuid", False, ""),
    ("landscape_rankings", "idea_id", "uuid", False, ""),
    ("landscape_rankings", "position", "integer", False, ""),
    ("landscape_rankings", "inserted_at", "timestamp with time zone", False, "now()"),
    ("landscape_similarity", "id", "uuid", False, None),
    ("landscape_similarity", "idea_a", "uuid", False, ""),
    ("landscape_similarity", "version_a", "character varying", False, ""),
    ("landscape_similarity", "idea_b", "uuid", False, ""),
    ("landscape_similarity", "version_b", "character varying", False, ""),
    ("landscape_similarity", "epoch_id", "uuid", False, ""),
    ("landscape_similarity", "verdict", "character varying", True, ""),
    ("landscape_similarity", "reason", "text", True, ""),
    ("landscape_similarity", "served_by_model", "character varying", True, ""),
    ("landscape_similarity", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_opportunities", "id", "uuid", False, None),
    ("landscape_opportunities", "version_counter", "integer", False, "0"),
    ("landscape_opportunities", "retired_at", "timestamp with time zone", True, ""),
    ("landscape_opportunities", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_opportunities", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_opportunity_versions", "id", "uuid", False, None),
    ("landscape_opportunity_versions", "opportunity_id", "uuid", False, ""),
    ("landscape_opportunity_versions", "version", "integer", False, ""),
    ("landscape_opportunity_versions", "epoch_id", "uuid", False, ""),
    ("landscape_opportunity_versions", "cut_id", "uuid", True, ""),
    ("landscape_opportunity_versions", "member_idea_keys", "jsonb", False, ""),
    ("landscape_opportunity_versions", "label", "character varying", True, ""),
    ("landscape_opportunity_versions", "theme", "text", True, ""),
    ("landscape_opportunity_versions", "platform_signal", "jsonb", True, ""),
    ("landscape_opportunity_versions", "edge_kinds", "jsonb", True, ""),
    ("landscape_opportunity_versions", "is_probe", "boolean", False, "false"),
    (
        "landscape_opportunity_versions", "status", "character varying", False, "'pending'::character varying",
    ),
    ("landscape_opportunity_versions", "batch_item_id", "uuid", True, ""),
    ("landscape_opportunity_versions", "attempts", "integer", False, "0"),
    ("landscape_opportunity_versions", "truncated_once", "boolean", False, "false"),
    ("landscape_opportunity_versions", "fallback_from", "character varying", True, ""),
    ("landscape_opportunity_versions", "served_by_model", "character varying", True, ""),
    ("landscape_opportunity_versions", "refusal_category", "character varying", True, ""),
    ("landscape_opportunity_versions", "error", "text", True, ""),
    ("landscape_opportunity_versions", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_opportunity_versions", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_assessments", "id", "uuid", False, None),
    ("landscape_assessments", "opportunity_id", "uuid", True, ""),
    ("landscape_assessments", "opportunity_version", "integer", True, ""),
    ("landscape_assessments", "epoch_id", "uuid", False, ""),
    ("landscape_assessments", "grouping_id", "uuid", True, ""),
    ("landscape_assessments", "status", "character varying", False, "'pending'::character varying"),
    ("landscape_assessments", "batch_item_id", "uuid", True, ""),
    ("landscape_assessments", "attempts", "integer", False, "0"),
    ("landscape_assessments", "truncated_once", "boolean", False, "false"),
    ("landscape_assessments", "fallback_from", "character varying", True, ""),
    ("landscape_assessments", "served_by_model", "character varying", True, ""),
    ("landscape_assessments", "refusal_category", "character varying", True, ""),
    ("landscape_assessments", "error", "text", True, ""),
    ("landscape_assessments", "company_or_project", "text", True, ""),
    ("landscape_assessments", "headline", "text", True, ""),
    ("landscape_assessments", "key_points", "jsonb", True, ""),
    ("landscape_assessments", "elevator_pitch", "text", True, ""),
    ("landscape_assessments", "score_rationale", "text", True, ""),
    ("landscape_assessments", "dimension_rationales", "jsonb", True, ""),
    ("landscape_assessments", "gating_rationales", "jsonb", True, ""),
    ("landscape_assessments", "strengths", "jsonb", True, ""),
    ("landscape_assessments", "risks", "jsonb", True, ""),
    ("landscape_assessments", "competitive_landscape", "jsonb", True, ""),
    ("landscape_assessments", "evidence_maturity", "jsonb", True, ""),
    ("landscape_assessments", "funnel_stage", "character varying", True, ""),
    ("landscape_assessments", "recommendation", "character varying", True, ""),
    ("landscape_assessments", "confidence", "character varying", True, ""),
    ("landscape_assessments", "weighted_score", "double precision", True, ""),
    ("landscape_assessments", "band", "character varying", True, ""),
    ("landscape_assessments", "gating", "jsonb", True, ""),
    ("landscape_assessments", "scores", "jsonb", True, ""),
    ("landscape_assessments", "red_flags", "jsonb", True, ""),
    ("landscape_assessments", "derisking_milestones", "jsonb", True, ""),
    ("landscape_assessments", "rationale", "text", True, ""),
    ("landscape_assessments", "prose_format", "character varying", True, ""),
    ("landscape_assessments", "recommended_next_experiment", "text", True, ""),
    ("landscape_assessments", "raw_verdict", "jsonb", True, ""),
    ("landscape_assessments", "rubric_version", "character varying", True, ""),
    ("landscape_assessments", "rubric_content_hash", "character varying", True, ""),
    ("landscape_assessments", "claim_support", "jsonb", True, ""),
    ("landscape_assessments", "member_notes", "jsonb", True, ""),
    ("landscape_assessments", "structured_lines", "jsonb", True, ""),
    ("landscape_assessments", "rank", "integer", True, ""),
    ("landscape_assessments", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_assessments", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_decisions", "id", "uuid", False, None),
    ("landscape_decisions", "opportunity_id", "uuid", False, ""),
    ("landscape_decisions", "reviewer_id", "uuid", True, ""),
    ("landscape_decisions", "reviewer_name", "character varying", False, ""),
    ("landscape_decisions", "decision", "character varying", False, ""),
    ("landscape_decisions", "reason_tags", "jsonb", True, ""),
    ("landscape_decisions", "reason", "text", False, "''::text"),
    ("landscape_decisions", "landscape_assessment_id", "uuid", True, ""),
    ("landscape_decisions", "edited", "boolean", False, "false"),
    ("landscape_decisions", "recorded_by_user_id", "uuid", True, ""),
    ("landscape_decisions", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_decisions", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_reviews", "id", "uuid", False, None),
    ("landscape_reviews", "opportunity_id", "uuid", False, ""),
    ("landscape_reviews", "reviewer_id", "uuid", True, ""),
    ("landscape_reviews", "reviewer_name", "character varying", False, ""),
    ("landscape_reviews", "score", "smallint", False, ""),
    ("landscape_reviews", "comment", "text", False, "''::text"),
    ("landscape_reviews", "dimension_scores", "jsonb", True, ""),
    ("landscape_reviews", "rubric_version", "character varying", True, ""),
    ("landscape_reviews", "rubric_content_hash", "character varying", True, ""),
    ("landscape_reviews", "landscape_assessment_id", "uuid", True, ""),
    ("landscape_reviews", "edited", "boolean", False, "false"),
    ("landscape_reviews", "recorded_by_user_id", "uuid", True, ""),
    ("landscape_reviews", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_reviews", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_review_events", "id", "uuid", False, None),
    ("landscape_review_events", "opportunity_id", "uuid", False, ""),
    ("landscape_review_events", "landscape_assessment_id", "uuid", True, ""),
    ("landscape_review_events", "action", "character varying", False, ""),
    ("landscape_review_events", "actor_id", "uuid", True, ""),
    ("landscape_review_events", "actor_name", "character varying", False, ""),
    ("landscape_review_events", "recorded_by_user_id", "uuid", True, ""),
    ("landscape_review_events", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_review_assignments", "id", "uuid", False, None),
    ("landscape_review_assignments", "opportunity_id", "uuid", False, ""),
    ("landscape_review_assignments", "assignee_id", "uuid", False, ""),
    ("landscape_review_assignments", "assignee_name", "character varying", False, ""),
    ("landscape_review_assignments", "assigned_by_user_id", "uuid", True, ""),
    ("landscape_review_assignments", "assigned_by_name", "character varying", False, ""),
    ("landscape_review_assignments", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_review_batches", "id", "uuid", False, None),
    ("landscape_review_batches", "issued_on", "date", False, ""),
    ("landscape_review_batches", "blind", "boolean", False, "false"),
    ("landscape_review_batches", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_review_batch_items", "id", "uuid", False, None),
    ("landscape_review_batch_items", "batch_id", "uuid", False, ""),
    ("landscape_review_batch_items", "opportunity_id", "uuid", False, ""),
    ("landscape_review_batch_items", "is_probe", "boolean", False, "false"),
    ("landscape_review_batch_items", "position", "integer", True, ""),
    ("landscape_review_batch_items", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_expert_judgments", "id", "uuid", False, None),
    ("landscape_expert_judgments", "opportunity_id", "uuid", False, ""),
    ("landscape_expert_judgments", "expert_id", "uuid", True, ""),
    ("landscape_expert_judgments", "expert_name", "character varying", False, ""),
    ("landscape_expert_judgments", "quarter", "character varying", False, ""),
    ("landscape_expert_judgments", "landscape_assessment_id", "uuid", True, ""),
    ("landscape_expert_judgments", "issued_before_draw", "boolean", True, ""),
    ("landscape_expert_judgments", "gates", "jsonb", True, ""),
    ("landscape_expert_judgments", "scores", "jsonb", True, ""),
    ("landscape_expert_judgments", "deal_breakers", "jsonb", True, ""),
    ("landscape_expert_judgments", "recommendation", "character varying", True, ""),
    ("landscape_expert_judgments", "comment", "text", True, ""),
    ("landscape_expert_judgments", "drawn_at", "timestamp with time zone", False, "now()"),
    ("landscape_expert_judgments", "submitted_at", "timestamp with time zone", True, ""),
    ("landscape_expert_judgments", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_expert_judgments", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_reject_audits", "id", "uuid", False, None),
    ("landscape_reject_audits", "item_kind", "character varying", False, ""),
    ("landscape_reject_audits", "item_id", "uuid", False, ""),
    ("landscape_reject_audits", "epoch_id", "uuid", False, ""),
    (
        "landscape_reject_audits", "status", "character varying", False, "'pending'::character varying",
    ),
    ("landscape_reject_audits", "batch_item_id", "uuid", True, ""),
    ("landscape_reject_audits", "attempts", "integer", False, "0"),
    ("landscape_reject_audits", "truncated_once", "boolean", False, "false"),
    ("landscape_reject_audits", "fallback_from", "character varying", True, ""),
    ("landscape_reject_audits", "served_by_model", "character varying", True, ""),
    ("landscape_reject_audits", "refusal_category", "character varying", True, ""),
    ("landscape_reject_audits", "error", "text", True, ""),
    ("landscape_reject_audits", "original_decision", "character varying", True, ""),
    ("landscape_reject_audits", "audit_decision", "character varying", True, ""),
    ("landscape_reject_audits", "agree", "boolean", True, ""),
    ("landscape_reject_audits", "reason", "text", True, ""),
    ("landscape_reject_audits", "raw", "jsonb", True, ""),
    ("landscape_reject_audits", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_reject_audits", "updated_at", "timestamp with time zone", False, "now()"),
    ("landscape_rescues", "id", "uuid", False, None),
    ("landscape_rescues", "item_kind", "character varying", False, ""),
    ("landscape_rescues", "item_id", "uuid", False, ""),
    ("landscape_rescues", "stage", "character varying", False, ""),
    ("landscape_rescues", "epoch_id", "uuid", False, ""),
    ("landscape_rescues", "actor_id", "uuid", True, ""),
    ("landscape_rescues", "actor_name", "character varying", False, ""),
    ("landscape_rescues", "reason", "text", False, ""),
    ("landscape_rescues", "carried", "boolean", False, "false"),
    ("landscape_rescues", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_llm_calls", "id", "uuid", False, None),
    ("landscape_llm_calls", "stage", "character varying", False, ""),
    ("landscape_llm_calls", "model", "character varying", False, ""),
    ("landscape_llm_calls", "served_by_model", "character varying", True, ""),
    ("landscape_llm_calls", "batch_id", "uuid", True, ""),
    ("landscape_llm_calls", "item_seq", "integer", True, ""),
    ("landscape_llm_calls", "custom_id", "character varying", True, ""),
    ("landscape_llm_calls", "item_ref", "character varying", True, ""),
    ("landscape_llm_calls", "batch", "boolean", False, "false"),
    ("landscape_llm_calls", "input_tokens", "integer", False, "0"),
    ("landscape_llm_calls", "output_tokens", "integer", False, "0"),
    ("landscape_llm_calls", "cache_read_tokens", "integer", False, "0"),
    ("landscape_llm_calls", "cache_creation_tokens", "integer", False, "0"),
    ("landscape_llm_calls", "cost_usd", "numeric", True, ""),
    ("landscape_llm_calls", "refusal_category", "character varying", True, ""),
    ("landscape_llm_calls", "latency_ms", "integer", True, ""),
    ("landscape_llm_calls", "created_at", "timestamp with time zone", False, "now()"),
    ("landscape_reports", "id", "uuid", False, None),
    ("landscape_reports", "kind", "character varying", False, ""),
    ("landscape_reports", "payload", "jsonb", True, ""),
    ("landscape_reports", "handled_at", "timestamp with time zone", True, ""),
    ("landscape_reports", "created_at", "timestamp with time zone", False, "now()"),
)

EXPECTED_TABLES = (
    "pi_dm_messages", "cohorts", "cohort_memberships", "cohort_audit_events", "rubric_documents",
    "publication_candidates", "authorship_claim_checks", "hub_declines",
    "interview_trajectories", "hub_projects", "hub_project_calls", "hub_project_clocks",
    "hub_project_reviews", "hub_project_review_assignments",
    # 0077.
    "landscape_sources",
    "landscape_fetches",
    "landscape_labs",
    "landscape_evidence_cache",
    "landscape_documents",
    "landscape_document_links",
    "landscape_texts",
    "landscape_batches",
    "landscape_batch_items",
    "landscape_regression_sets",
    "landscape_epochs",
    "landscape_regression_items",
    "landscape_regression_runs",
    "landscape_screens",
    "landscape_extractions",
    "landscape_ideas",
    "landscape_idea_links",
    "landscape_extraction_links",
    "landscape_idea_evidence",
    "landscape_idea_aliases",
    "landscape_triage",
    "landscape_cuts",
    "landscape_candidate_groupings",
    "landscape_deep",
    "landscape_consults",
    "landscape_comparisons",
    "landscape_rankings",
    "landscape_similarity",
    "landscape_opportunities",
    "landscape_opportunity_versions",
    "landscape_assessments",
    "landscape_decisions",
    "landscape_reviews",
    "landscape_review_events",
    "landscape_review_assignments",
    "landscape_review_batches",
    "landscape_review_batch_items",
    "landscape_expert_judgments",
    "landscape_reject_audits",
    "landscape_rescues",
    "landscape_llm_calls",
    "landscape_reports",
)

#: Revisions this script's EXPECTED_TABLES/EXPECTED_COLUMNS/EXPECTED_INDEXES below
#: actually verify. Pinned: 0019-0023, 0056, 0062, 0065, 0068, 0069, 0070, 0071, 0072, 0073,
#: 0075, 0076 and 0077. Not pinned (documented omission): the 0024-0055, 0057-0061, 0063-0064
#: and 0066-0067 objects (0074 creates none), which preflight's PLANNED_OBJECTS collision check and
#: tests/integration/test_model_migration_parity.py cover. Bump this tuple (and add the
#: corresponding EXPECTED_* entries) to extend the pins. The row-count and enum checks
#: do not read it: they are scoped by revision span instead.
VERIFIED_REVISIONS: tuple[str, ...] = ("0019", "0020", "0021", "0022", "0023", "0056", "0062", "0065", "0068", "0069", "0070", "0071", "0072", "0073", "0075", "0076", "0077")

#: index name -> the exact pg_indexes.indexdef tail, so a same-named index on the WRONG
#: columns (or a partial index that lost its predicate) fails too.
EXPECTED_INDEXES: dict[str, str] = {
    "uq_agent_messages_run_ts": "USING btree (simulation_run_id, message_ts)",
    "ix_agent_messages_run_posted": "USING btree (simulation_run_id, posted_at)",
    "ix_agent_messages_run_channel_posted": (
        "USING btree (simulation_run_id, channel_name, posted_at)"
    ),
    "ix_agent_messages_run_slack_ts": (
        "USING btree (simulation_run_id, slack_ts) WHERE (slack_ts IS NOT NULL)"
    ),
    "ix_agent_messages_run_created": "USING btree (simulation_run_id, created_at)",
    "ix_pi_dm_run_agent_posted": "USING btree (simulation_run_id, agent_id, posted_at)",
    "ix_pi_dm_run_direction_posted": "USING btree (simulation_run_id, direction, posted_at)",
    "ix_pi_dm_run_direction_created": "USING btree (simulation_run_id, direction, created_at)",
    "ix_cohort_memberships_cohort_id": "USING btree (cohort_id)",
    "ix_cohort_memberships_agent_id": "USING btree (agent_id)",
    "ix_cohort_audit_events_cohort_id": "USING btree (cohort_id)",
    "ix_cohort_audit_events_created_at": "USING btree (created_at)",
    "uq_cohort_membership_cohort_agent": "USING btree (cohort_id, agent_id)",
    # 0056. Partial indexes pin the prefix up to the predicate: the predicate's
    # enum-cast rendering is Postgres's, not ours.
    "uq_jobs_one_active_per_user_type": "USING btree (user_id, type) WHERE (",
    "uq_users_email_lower": "USING btree (lower((email)::text))",
    "ix_agent_messages_agent_phase": "USING btree (agent_id, phase)",
    "ix_chat_usage_streaming": "USING btree (created_at) WHERE (",
    # 0056 unique constraints: their backing indexes, as for the 0019/0022 ones.
    "uq_slack_app_provisions_agent": "USING btree (agent_registry_id)",
    "uq_publications_user_pmid": "USING btree (user_id, pmid)",
    # 0062 and 0065.
    "ix_publications_excluded_by_user_id": "USING btree (excluded_by_user_id)",
    "uq_publication_candidates_user_pmid": "USING btree (user_id, pmid)",
    "ix_publication_candidates_decided_by_user_id": "USING btree (decided_by_user_id)",
    "uq_publications_user_doi_nopmid": (
        "USING btree (user_id, lower((doi)::text)) WHERE (pmid IS NULL)"
    ),
    # 0069.
    "ix_profile_revisions_owner_user_id": "USING btree (owner_user_id)",
    # 0070.
    "ix_authorship_claim_checks_run_agent": "USING btree (simulation_run_id, agent_id)",
    "ix_authorship_claim_checks_created_at": "USING btree (created_at)",
    # 0071: the unique constraint's backing index.
    "uq_hub_declines_run_thread": "USING btree (simulation_run_id, thread_id)",
    # 0072: the unique constraint's backing index and the spend ceiling's index.
    "uq_interview_trajectories_run_thread": "USING btree (simulation_run_id, thread_id)",
    "ix_interview_trajectories_updated": "USING btree (updated_at)",
    # 0073: the three unique constraints' backing indexes and the three plain indexes.
    "uq_hub_projects_run_agent_slot": "USING btree (simulation_run_id, agent_id, slot)",
    "uq_hub_project_clocks_run": "USING btree (simulation_run_id)",
    "uq_hub_project_reviews_project_reviewer": "USING btree (project_id, reviewer_user_id)",
    "ix_hub_projects_run_reply": "USING btree (simulation_run_id, reply_ts)",
    "ix_hub_project_calls_updated": "USING btree (updated_at)",
    "ix_hub_project_calls_project": "USING btree (project_id)",
    # 0075: the unique constraint's backing index and the two plain indexes.
    "uq_hub_project_review_assignment_once": "USING btree (project_id, assignee_user_id)",
    "ix_hub_project_review_assignments_project_id": "USING btree (project_id)",
    "ix_hub_project_review_assignments_assignee": "USING btree (assignee_user_id)",
    # 0076.
    "ix_assessment_chat_turns_project_conversation": (
        "USING btree (project_id, user_id, context_tier, created_at)"
    ),
    "ix_assessment_chat_usage_project_id": "USING btree (project_id)",
    "ix_assessment_chat_opens_project_id": "USING btree (project_id)",
    # 0077: every unique constraint's backing index and every plain or partial index.
    "uq_landscape_sources_source": "USING btree (source)",
    "uq_landscape_fetches_source_slice_started": "USING btree (source, slice_key, started_at)",
    "ix_landscape_fetches_status": "USING btree (status)",
    "uq_landscape_labs_lab_key": "USING btree (lab_key)",
    "uq_landscape_evidence_cache_source_release_query": "USING btree (source, release, query_key)",
    "uq_landscape_documents_kind_external_id": "USING btree (kind, canonical_external_id)",
    "ix_landscape_documents_doi": "USING btree (doi)",
    "ix_landscape_documents_pmid": "USING btree (pmid)",
    "ix_landscape_documents_openalex_id": "USING btree (openalex_id)",
    "ix_landscape_documents_lab_key": "USING btree (lab_key)",
    "ix_landscape_documents_s1_status": "USING btree (s1_status)",
    "ix_landscape_documents_merged_into": "USING btree (merged_into)",
    "uq_landscape_document_links_pair_kind": "USING btree (doc_a, doc_b, link_kind)",
    "ix_landscape_document_links_doc_b": "USING btree (doc_b)",
    "uq_landscape_texts_document_id": "USING btree (document_id)",
    "uq_landscape_batches_seq": "USING btree (seq)",
    "uq_landscape_batches_api_batch_id": "USING btree (api_batch_id)",
    "ix_landscape_batches_status": "USING btree (status)",
    "uq_landscape_batch_items_batch_seq": "USING btree (batch_id, item_seq)",
    "ix_landscape_batch_items_row_id": "USING btree (row_id)",
    "uq_landscape_regression_sets_version": "USING btree (version)",
    "uq_landscape_epochs_one_active": "USING btree (status) WHERE (",
    "ix_landscape_epochs_built_from_epoch_id": "USING btree (built_from_epoch_id)",
    "ix_landscape_epochs_override_actor_id": "USING btree (override_actor_id)",
    "uq_landscape_regression_items_set_id": "USING btree (set_version, id)",
    "uq_landscape_regression_runs_epoch_started": "USING btree (epoch_id, started_at)",
    "ix_landscape_regression_runs_set_version": "USING btree (set_version)",
    "uq_landscape_screens_document_epoch": "USING btree (document_id, epoch_id)",
    "ix_landscape_screens_status": "USING btree (status)",
    "ix_landscape_screens_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_screens_batch_item_id": "USING btree (batch_item_id)",
    "uq_landscape_extractions_document_epoch_basis": "USING btree (document_id, epoch_id, basis)",
    "ix_landscape_extractions_status": "USING btree (status)",
    "ix_landscape_extractions_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_extractions_batch_item_id": "USING btree (batch_item_id)",
    "uq_landscape_ideas_epoch_key": "USING btree (epoch_id, idea_key)",
    "uq_landscape_idea_links_epoch_pair": "USING btree (epoch_id, idea_a, idea_b)",
    "ix_landscape_idea_links_idea_a": "USING btree (idea_a)",
    "ix_landscape_idea_links_idea_b": "USING btree (idea_b)",
    "ix_landscape_idea_links_document_id": "USING btree (document_id)",
    "uq_landscape_extraction_links_epoch_pair": "USING btree (epoch_id, doc_a, doc_b)",
    "ix_landscape_extraction_links_doc_a": "USING btree (doc_a)",
    "ix_landscape_extraction_links_doc_b": "USING btree (doc_b)",
    "ix_landscape_extraction_links_extraction_id": "USING btree (extraction_id)",
    "uq_landscape_idea_evidence_idea_extraction_index": (
        "USING btree (idea_id, extraction_id, candidate_index)"
    ),
    "ix_landscape_idea_evidence_extraction_id": "USING btree (extraction_id)",
    "uq_landscape_idea_aliases_epoch_old_key": "USING btree (epoch_id, old_key)",
    "ix_landscape_idea_aliases_idea_id": "USING btree (idea_id)",
    "uq_landscape_triage_idea_version_epoch": "USING btree (idea_id, idea_version, epoch_id)",
    "ix_landscape_triage_status": "USING btree (status)",
    "ix_landscape_triage_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_triage_batch_item_id": "USING btree (batch_item_id)",
    "ix_landscape_cuts_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_cuts_boundary_batch_id": "USING btree (boundary_batch_id)",
    "uq_landscape_candidate_groupings_epoch_key": "USING btree (epoch_id, grouping_key)",
    "ix_landscape_candidate_groupings_status": "USING btree (status)",
    "ix_landscape_candidate_groupings_cut_id": "USING btree (cut_id)",
    "ix_landscape_candidate_groupings_batch_item_id": "USING btree (batch_item_id)",
    "uq_landscape_deep_idea_version_epoch": "USING btree (idea_id, idea_version, epoch_id)",
    "ix_landscape_deep_status": "USING btree (status)",
    "ix_landscape_deep_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_deep_cut_id": "USING btree (cut_id)",
    "uq_landscape_consults_deep_domain": "USING btree (deep_id, domain)",
    "uq_landscape_comparisons_pair": (
        "USING btree (idea_a, version_a, idea_b, version_b, epoch_id, basis, ask_order)"
    ),
    "ix_landscape_comparisons_status": "USING btree (status)",
    "ix_landscape_comparisons_idea_b": "USING btree (idea_b)",
    "ix_landscape_comparisons_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_comparisons_batch_item_id": "USING btree (batch_item_id)",
    "ix_landscape_comparisons_winner_idea_id": "USING btree (winner_idea_id)",
    "uq_landscape_rankings_epoch_idea": "USING btree (epoch_id, idea_id)",
    "ix_landscape_rankings_idea_id": "USING btree (idea_id)",
    "uq_landscape_similarity_pair": "USING btree (idea_a, version_a, idea_b, version_b, epoch_id)",
    "ix_landscape_similarity_idea_b": "USING btree (idea_b)",
    "ix_landscape_similarity_epoch_id": "USING btree (epoch_id)",
    "uq_landscape_opportunity_versions_opportunity_version": (
        "USING btree (opportunity_id, version)"
    ),
    "ix_landscape_opportunity_versions_status": "USING btree (status)",
    "ix_landscape_opportunity_versions_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_opportunity_versions_cut_id": "USING btree (cut_id)",
    "ix_landscape_opportunity_versions_batch_item_id": "USING btree (batch_item_id)",
    "uq_landscape_assessments_opportunity_version_epoch": (
        "USING btree (opportunity_id, opportunity_version, epoch_id)"
    ),
    "uq_landscape_assessments_epoch_grouping": "USING btree (epoch_id, grouping_id)",
    "ix_landscape_assessments_status": "USING btree (status)",
    "ix_landscape_assessments_grouping_id": "USING btree (grouping_id)",
    "ix_landscape_assessments_batch_item_id": "USING btree (batch_item_id)",
    "uq_landscape_decisions_opportunity_reviewer": "USING btree (opportunity_id, reviewer_id)",
    "ix_landscape_decisions_reviewer_id": "USING btree (reviewer_id)",
    "ix_landscape_decisions_landscape_assessment_id": "USING btree (landscape_assessment_id)",
    "ix_landscape_decisions_recorded_by_user_id": "USING btree (recorded_by_user_id)",
    "uq_landscape_reviews_opportunity_reviewer": "USING btree (opportunity_id, reviewer_id)",
    "ix_landscape_reviews_reviewer_id": "USING btree (reviewer_id)",
    "ix_landscape_reviews_landscape_assessment_id": "USING btree (landscape_assessment_id)",
    "ix_landscape_reviews_recorded_by_user_id": "USING btree (recorded_by_user_id)",
    "ix_landscape_review_events_opportunity_id": "USING btree (opportunity_id)",
    "ix_landscape_review_events_landscape_assessment_id": "USING btree (landscape_assessment_id)",
    "ix_landscape_review_events_actor_id": "USING btree (actor_id)",
    "ix_landscape_review_events_recorded_by_user_id": "USING btree (recorded_by_user_id)",
    "uq_landscape_review_assignments_opportunity_assignee": (
        "USING btree (opportunity_id, assignee_id)"
    ),
    "ix_landscape_review_assignments_assignee_id": "USING btree (assignee_id)",
    "ix_landscape_review_assignments_assigned_by_user_id": "USING btree (assigned_by_user_id)",
    "uq_landscape_review_batches_issued_on": "USING btree (issued_on)",
    "uq_landscape_review_batch_items_batch_opportunity": "USING btree (batch_id, opportunity_id)",
    "ix_landscape_review_batch_items_opportunity_id": "USING btree (opportunity_id)",
    "uq_landscape_expert_judgments_opportunity_expert_quarter": (
        "USING btree (opportunity_id, expert_id, quarter)"
    ),
    "ix_landscape_expert_judgments_expert_id": "USING btree (expert_id)",
    "ix_landscape_expert_judgments_landscape_assessment_id": (
        "USING btree (landscape_assessment_id)"
    ),
    "uq_landscape_reject_audits_item_epoch": "USING btree (item_kind, item_id, epoch_id)",
    "ix_landscape_reject_audits_status": "USING btree (status)",
    "ix_landscape_reject_audits_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_reject_audits_batch_item_id": "USING btree (batch_item_id)",
    "ix_landscape_rescues_item": "USING btree (item_kind, item_id)",
    "ix_landscape_rescues_epoch_id": "USING btree (epoch_id)",
    "ix_landscape_rescues_actor_id": "USING btree (actor_id)",
    "uq_landscape_llm_calls_batch_seq": "USING btree (batch_id, item_seq)",
    "ix_landscape_llm_calls_created_at": "USING btree (created_at)",
    "uq_landscape_reports_kind_created": "USING btree (kind, created_at)",
}

#: constraint name -> (table, pg_get_constraintdef)
EXPECTED_CONSTRAINTS: dict[str, tuple[str, str]] = {
    "uq_agent_messages_run_ts": (
        "agent_messages",
        "UNIQUE (simulation_run_id, message_ts)",
    ),
    "uq_cohort_membership_cohort_agent": (
        "cohort_memberships",
        "UNIQUE (cohort_id, agent_id)",
    ),
    "uq_slack_app_provisions_agent": ("slack_app_provisions", "UNIQUE (agent_registry_id)"),
    "uq_publications_user_pmid": ("publications", "UNIQUE (user_id, pmid)"),
    "fk_publications_excluded_by_user_id_users": (
        "publications",
        "FOREIGN KEY (excluded_by_user_id) REFERENCES users(id) ON DELETE SET NULL",
    ),
    # 0065 recreates this CHECK under the same name; 'openalex' proves the 0065 version.
    "ck_publications_provenance": ("publications", "'openalex'"),
    # 0068: the last allowed value proves this revision's CHECK (substring compare).
    "ck_publications_excluded_reason": ("publications", "'repair_script'"),
    # 0069.
    "fk_profile_revisions_owner_user_id_users": (
        "profile_revisions",
        "FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE",
    ),
    "uq_publication_candidates_user_pmid": ("publication_candidates", "UNIQUE (user_id, pmid)"),
    "ck_publication_candidates_status": ("publication_candidates", "'rejected'"),
    # 0070.
    "ck_authorship_claim_checks_mode": ("authorship_claim_checks", "'enforce'::character varying"),
    "ck_authorship_claim_checks_outcome": (
        "authorship_claim_checks", "'rejected'::character varying",
    ),
    "ck_authorship_claim_checks_excerpt_length": (
        "authorship_claim_checks", "char_length(excerpt) <= 2000",
    ),
    "authorship_claim_checks_simulation_run_id_fkey": (
        "authorship_claim_checks", "REFERENCES simulation_runs(id) ON DELETE CASCADE",
    ),
    # 0071.
    "uq_hub_declines_run_thread": ("hub_declines", "UNIQUE (simulation_run_id, thread_id)"),
    "ck_hub_declines_reason_length": ("hub_declines", "char_length(reason_text) <= 4000"),
    "hub_declines_simulation_run_id_fkey": (
        "hub_declines", "REFERENCES simulation_runs(id) ON DELETE CASCADE",
    ),
    # 0072.
    "uq_interview_trajectories_run_thread": (
        "interview_trajectories", "UNIQUE (simulation_run_id, thread_id)",
    ),
    "ck_interview_trajectories_status": ("interview_trajectories", "'refused'"),
    "interview_trajectories_simulation_run_id_fkey": (
        "interview_trajectories", "REFERENCES simulation_runs(id) ON DELETE CASCADE",
    ),
    # 0073. Each CHECK is pinned by its last allowed value (or its bound).
    "uq_hub_projects_run_agent_slot": (
        "hub_projects", "UNIQUE (simulation_run_id, agent_id, slot)",
    ),
    "ck_hub_projects_status": ("hub_projects", "'not_finished'"),
    "ck_hub_project_calls_status": ("hub_project_calls", "'refused'"),
    "uq_hub_project_clocks_run": ("hub_project_clocks", "UNIQUE (simulation_run_id)"),
    "uq_hub_project_reviews_project_reviewer": (
        "hub_project_reviews", "UNIQUE (project_id, reviewer_user_id)",
    ),
    "ck_hub_project_reviews_rating": ("hub_project_reviews", "<= 5"),
    "hub_projects_simulation_run_id_fkey": (
        "hub_projects", "REFERENCES simulation_runs(id) ON DELETE CASCADE",
    ),
    "hub_project_calls_project_id_fkey": (
        "hub_project_calls", "REFERENCES hub_projects(id) ON DELETE CASCADE",
    ),
    "hub_project_clocks_simulation_run_id_fkey": (
        "hub_project_clocks", "REFERENCES simulation_runs(id) ON DELETE CASCADE",
    ),
    "hub_project_reviews_project_id_fkey": (
        "hub_project_reviews", "REFERENCES hub_projects(id) ON DELETE CASCADE",
    ),
    "hub_project_reviews_reviewer_user_id_fkey": (
        "hub_project_reviews", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    # 0075. Its foreign keys are inline, so they carry Postgres's default names.
    "uq_hub_project_review_assignment_once": (
        "hub_project_review_assignments", "UNIQUE (project_id, assignee_user_id)",
    ),
    "hub_project_review_assignments_project_id_fkey": (
        "hub_project_review_assignments", "REFERENCES hub_projects(id) ON DELETE CASCADE",
    ),
    "hub_project_review_assignments_assignee_user_id_fkey": (
        "hub_project_review_assignments", "REFERENCES users(id) ON DELETE CASCADE",
    ),
    "hub_project_review_assignments_assigned_by_user_id_fkey": (
        "hub_project_review_assignments", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    # 0076. Each subject CHECK is pinned by its last allowed value.
    "fk_assessment_chat_turns_project_id_hub_projects": (
        "assessment_chat_turns",
        "FOREIGN KEY (project_id) REFERENCES hub_projects(id) ON DELETE CASCADE",
    ),
    "fk_assessment_chat_usage_project_id_hub_projects": (
        "assessment_chat_usage",
        "FOREIGN KEY (project_id) REFERENCES hub_projects(id) ON DELETE SET NULL",
    ),
    "fk_assessment_chat_opens_project_id_hub_projects": (
        "assessment_chat_opens",
        "FOREIGN KEY (project_id) REFERENCES hub_projects(id) ON DELETE SET NULL",
    ),
    "ck_assessment_chat_turns_one_subject": (
        "assessment_chat_turns", "num_nonnulls(assessment_id, project_id) = 1",
    ),
    "ck_assessment_chat_usage_subject": ("assessment_chat_usage", "'project'"),
    "ck_assessment_chat_opens_subject": ("assessment_chat_opens", "'project'"),
    # 0077. Uniques by definition, each CHECK by a value or bound it must keep, and the inline
    # foreign keys by Postgres's default names.
    "uq_landscape_sources_source": ("landscape_sources", "UNIQUE (source)"),
    "uq_landscape_fetches_source_slice_started": (
        "landscape_fetches", "UNIQUE (source, slice_key, started_at)",
    ),
    "ck_landscape_fetches_status": ("landscape_fetches", "'failed'"),
    "uq_landscape_labs_lab_key": ("landscape_labs", "UNIQUE (lab_key)"),
    "uq_landscape_evidence_cache_source_release_query": (
        "landscape_evidence_cache", "UNIQUE (source, release, query_key)",
    ),
    "uq_landscape_documents_kind_external_id": (
        "landscape_documents", "UNIQUE (kind, canonical_external_id)",
    ),
    "ck_landscape_documents_kind": ("landscape_documents", "'dataset'"),
    "ck_landscape_documents_s1_status": ("landscape_documents", "'excluded:%'"),
    "landscape_documents_merged_into_fkey": (
        "landscape_documents", "REFERENCES landscape_documents(id) ON DELETE SET NULL",
    ),
    "uq_landscape_document_links_pair_kind": (
        "landscape_document_links", "UNIQUE (doc_a, doc_b, link_kind)",
    ),
    "ck_landscape_document_links_kind": ("landscape_document_links", "'dataset_of'"),
    "landscape_document_links_doc_a_fkey": (
        "landscape_document_links", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "landscape_document_links_doc_b_fkey": (
        "landscape_document_links", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "uq_landscape_texts_document_id": ("landscape_texts", "UNIQUE (document_id)"),
    "ck_landscape_texts_basis": ("landscape_texts", "'fetch_failed'"),
    "landscape_texts_document_id_fkey": (
        "landscape_texts", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "uq_landscape_batches_seq": ("landscape_batches", "UNIQUE (seq)"),
    "uq_landscape_batches_api_batch_id": ("landscape_batches", "UNIQUE (api_batch_id)"),
    "ck_landscape_batches_status": ("landscape_batches", "'ingested'"),
    "uq_landscape_batch_items_batch_seq": ("landscape_batch_items", "UNIQUE (batch_id, item_seq)"),
    "landscape_batch_items_batch_id_fkey": (
        "landscape_batch_items", "REFERENCES landscape_batches(id) ON DELETE CASCADE",
    ),
    "uq_landscape_regression_sets_version": ("landscape_regression_sets", "UNIQUE (version)"),
    "ck_landscape_epochs_status": ("landscape_epochs", "'retired'"),
    "landscape_epochs_built_from_epoch_id_fkey": (
        "landscape_epochs", "REFERENCES landscape_epochs(id) ON DELETE SET NULL",
    ),
    "landscape_epochs_override_actor_id_fkey": (
        "landscape_epochs", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "uq_landscape_regression_items_set_id": (
        "landscape_regression_items", "UNIQUE (set_version, id)",
    ),
    "landscape_regression_items_set_version_fkey": (
        "landscape_regression_items", "REFERENCES landscape_regression_sets(version) ON DELETE CASCADE",
    ),
    "uq_landscape_regression_runs_epoch_started": (
        "landscape_regression_runs", "UNIQUE (epoch_id, started_at)",
    ),
    "ck_landscape_regression_runs_status": ("landscape_regression_runs", "'failed'"),
    "landscape_regression_runs_epoch_id_fkey": (
        "landscape_regression_runs", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_regression_runs_set_version_fkey": (
        "landscape_regression_runs", "REFERENCES landscape_regression_sets(version) ON DELETE CASCADE",
    ),
    "uq_landscape_screens_document_epoch": ("landscape_screens", "UNIQUE (document_id, epoch_id)"),
    "ck_landscape_screens_status": ("landscape_screens", "'failed:%'"),
    "landscape_screens_document_id_fkey": (
        "landscape_screens", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "landscape_screens_epoch_id_fkey": (
        "landscape_screens", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_screens_batch_item_id_fkey": (
        "landscape_screens", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "uq_landscape_extractions_document_epoch_basis": (
        "landscape_extractions", "UNIQUE (document_id, epoch_id, basis)",
    ),
    "ck_landscape_extractions_status": ("landscape_extractions", "'failed:%'"),
    "ck_landscape_extractions_basis": ("landscape_extractions", "'full_text'"),
    "landscape_extractions_document_id_fkey": (
        "landscape_extractions", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "landscape_extractions_epoch_id_fkey": (
        "landscape_extractions", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_extractions_batch_item_id_fkey": (
        "landscape_extractions", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "uq_landscape_ideas_epoch_key": ("landscape_ideas", "UNIQUE (epoch_id, idea_key)"),
    "ck_landscape_ideas_type": ("landscape_ideas", "'chemistry'"),
    "landscape_ideas_epoch_id_fkey": (
        "landscape_ideas", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "uq_landscape_idea_links_epoch_pair": (
        "landscape_idea_links", "UNIQUE (epoch_id, idea_a, idea_b)",
    ),
    "landscape_idea_links_epoch_id_fkey": (
        "landscape_idea_links", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_idea_links_idea_a_fkey": (
        "landscape_idea_links", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_idea_links_idea_b_fkey": (
        "landscape_idea_links", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_idea_links_document_id_fkey": (
        "landscape_idea_links", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "uq_landscape_extraction_links_epoch_pair": (
        "landscape_extraction_links", "UNIQUE (epoch_id, doc_a, doc_b)",
    ),
    "landscape_extraction_links_epoch_id_fkey": (
        "landscape_extraction_links", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_extraction_links_doc_a_fkey": (
        "landscape_extraction_links", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "landscape_extraction_links_doc_b_fkey": (
        "landscape_extraction_links", "REFERENCES landscape_documents(id) ON DELETE CASCADE",
    ),
    "landscape_extraction_links_extraction_id_fkey": (
        "landscape_extraction_links", "REFERENCES landscape_extractions(id) ON DELETE CASCADE",
    ),
    "uq_landscape_idea_evidence_idea_extraction_index": (
        "landscape_idea_evidence", "UNIQUE (idea_id, extraction_id, candidate_index)",
    ),
    "landscape_idea_evidence_idea_id_fkey": (
        "landscape_idea_evidence", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_idea_evidence_extraction_id_fkey": (
        "landscape_idea_evidence", "REFERENCES landscape_extractions(id) ON DELETE CASCADE",
    ),
    "uq_landscape_idea_aliases_epoch_old_key": (
        "landscape_idea_aliases", "UNIQUE (epoch_id, old_key)",
    ),
    "landscape_idea_aliases_epoch_id_fkey": (
        "landscape_idea_aliases", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_idea_aliases_idea_id_fkey": (
        "landscape_idea_aliases", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "uq_landscape_triage_idea_version_epoch": (
        "landscape_triage", "UNIQUE (idea_id, idea_version, epoch_id)",
    ),
    "ck_landscape_triage_status": ("landscape_triage", "'failed:%'"),
    "landscape_triage_idea_id_fkey": (
        "landscape_triage", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_triage_epoch_id_fkey": (
        "landscape_triage", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_triage_batch_item_id_fkey": (
        "landscape_triage", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "ck_landscape_cuts_status": ("landscape_cuts", "'complete'"),
    "landscape_cuts_epoch_id_fkey": (
        "landscape_cuts", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_cuts_boundary_batch_id_fkey": (
        "landscape_cuts", "REFERENCES landscape_batches(id) ON DELETE SET NULL",
    ),
    "uq_landscape_candidate_groupings_epoch_key": (
        "landscape_candidate_groupings", "UNIQUE (epoch_id, grouping_key)",
    ),
    "ck_landscape_candidate_groupings_status": ("landscape_candidate_groupings", "'failed:%'"),
    "landscape_candidate_groupings_epoch_id_fkey": (
        "landscape_candidate_groupings", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_candidate_groupings_cut_id_fkey": (
        "landscape_candidate_groupings", "REFERENCES landscape_cuts(id) ON DELETE CASCADE",
    ),
    "landscape_candidate_groupings_batch_item_id_fkey": (
        "landscape_candidate_groupings", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "uq_landscape_deep_idea_version_epoch": (
        "landscape_deep", "UNIQUE (idea_id, idea_version, epoch_id)",
    ),
    "ck_landscape_deep_status": ("landscape_deep", "'failed:%'"),
    "landscape_deep_idea_id_fkey": (
        "landscape_deep", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_deep_epoch_id_fkey": (
        "landscape_deep", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_deep_cut_id_fkey": (
        "landscape_deep", "REFERENCES landscape_cuts(id) ON DELETE SET NULL",
    ),
    "uq_landscape_consults_deep_domain": ("landscape_consults", "UNIQUE (deep_id, domain)"),
    "landscape_consults_deep_id_fkey": (
        "landscape_consults", "REFERENCES landscape_deep(id) ON DELETE CASCADE",
    ),
    "uq_landscape_comparisons_pair": (
        "landscape_comparisons", "UNIQUE (idea_a, version_a, idea_b, version_b, epoch_id, basis, ask_order)",
    ),
    "ck_landscape_comparisons_status": ("landscape_comparisons", "'failed:%'"),
    "ck_landscape_comparisons_basis": ("landscape_comparisons", "'deep'"),
    "ck_landscape_comparisons_ask_order": ("landscape_comparisons", "ask_order"),
    "landscape_comparisons_idea_a_fkey": (
        "landscape_comparisons", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_comparisons_idea_b_fkey": (
        "landscape_comparisons", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_comparisons_epoch_id_fkey": (
        "landscape_comparisons", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_comparisons_batch_item_id_fkey": (
        "landscape_comparisons", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "landscape_comparisons_winner_idea_id_fkey": (
        "landscape_comparisons", "REFERENCES landscape_ideas(id) ON DELETE SET NULL",
    ),
    "uq_landscape_rankings_epoch_idea": ("landscape_rankings", "UNIQUE (epoch_id, idea_id)"),
    "landscape_rankings_epoch_id_fkey": (
        "landscape_rankings", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_rankings_idea_id_fkey": (
        "landscape_rankings", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "uq_landscape_similarity_pair": (
        "landscape_similarity", "UNIQUE (idea_a, version_a, idea_b, version_b, epoch_id)",
    ),
    "landscape_similarity_idea_a_fkey": (
        "landscape_similarity", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_similarity_idea_b_fkey": (
        "landscape_similarity", "REFERENCES landscape_ideas(id) ON DELETE CASCADE",
    ),
    "landscape_similarity_epoch_id_fkey": (
        "landscape_similarity", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "uq_landscape_opportunity_versions_opportunity_version": (
        "landscape_opportunity_versions", "UNIQUE (opportunity_id, version)",
    ),
    "ck_landscape_opportunity_versions_status": ("landscape_opportunity_versions", "'failed:%'"),
    "landscape_opportunity_versions_opportunity_id_fkey": (
        "landscape_opportunity_versions", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_opportunity_versions_epoch_id_fkey": (
        "landscape_opportunity_versions", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_opportunity_versions_cut_id_fkey": (
        "landscape_opportunity_versions", "REFERENCES landscape_cuts(id) ON DELETE SET NULL",
    ),
    "landscape_opportunity_versions_batch_item_id_fkey": (
        "landscape_opportunity_versions", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "uq_landscape_assessments_opportunity_version_epoch": (
        "landscape_assessments", "UNIQUE (opportunity_id, opportunity_version, epoch_id)",
    ),
    "uq_landscape_assessments_epoch_grouping": (
        "landscape_assessments", "UNIQUE (epoch_id, grouping_id)",
    ),
    "ck_landscape_assessments_status": ("landscape_assessments", "'failed:%'"),
    "ck_landscape_assessments_one_key": ("landscape_assessments", "grouping_id IS NOT NULL"),
    "landscape_assessments_opportunity_id_fkey": (
        "landscape_assessments", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_assessments_epoch_id_fkey": (
        "landscape_assessments", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_assessments_grouping_id_fkey": (
        "landscape_assessments", "REFERENCES landscape_candidate_groupings(id) ON DELETE CASCADE",
    ),
    "landscape_assessments_batch_item_id_fkey": (
        "landscape_assessments", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "uq_landscape_decisions_opportunity_reviewer": (
        "landscape_decisions", "UNIQUE (opportunity_id, reviewer_id)",
    ),
    "ck_landscape_decisions_decision": ("landscape_decisions", "'pass'"),
    "ck_landscape_decisions_reason_length": ("landscape_decisions", "<= 1000"),
    "landscape_decisions_opportunity_id_fkey": (
        "landscape_decisions", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_decisions_reviewer_id_fkey": (
        "landscape_decisions", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "landscape_decisions_landscape_assessment_id_fkey": (
        "landscape_decisions", "REFERENCES landscape_assessments(id) ON DELETE SET NULL",
    ),
    "landscape_decisions_recorded_by_user_id_fkey": (
        "landscape_decisions", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "uq_landscape_reviews_opportunity_reviewer": (
        "landscape_reviews", "UNIQUE (opportunity_id, reviewer_id)",
    ),
    "ck_landscape_reviews_score": ("landscape_reviews", "<= 5"),
    "ck_landscape_reviews_comment_length": ("landscape_reviews", "<= 10000"),
    "landscape_reviews_opportunity_id_fkey": (
        "landscape_reviews", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_reviews_reviewer_id_fkey": (
        "landscape_reviews", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "landscape_reviews_landscape_assessment_id_fkey": (
        "landscape_reviews", "REFERENCES landscape_assessments(id) ON DELETE SET NULL",
    ),
    "landscape_reviews_recorded_by_user_id_fkey": (
        "landscape_reviews", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "ck_landscape_review_events_action": ("landscape_review_events", "'cleared'"),
    "landscape_review_events_opportunity_id_fkey": (
        "landscape_review_events", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_review_events_landscape_assessment_id_fkey": (
        "landscape_review_events", "REFERENCES landscape_assessments(id) ON DELETE SET NULL",
    ),
    "landscape_review_events_actor_id_fkey": (
        "landscape_review_events", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "landscape_review_events_recorded_by_user_id_fkey": (
        "landscape_review_events", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "uq_landscape_review_assignments_opportunity_assignee": (
        "landscape_review_assignments", "UNIQUE (opportunity_id, assignee_id)",
    ),
    "landscape_review_assignments_opportunity_id_fkey": (
        "landscape_review_assignments", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_review_assignments_assignee_id_fkey": (
        "landscape_review_assignments", "REFERENCES users(id) ON DELETE CASCADE",
    ),
    "landscape_review_assignments_assigned_by_user_id_fkey": (
        "landscape_review_assignments", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "uq_landscape_review_batches_issued_on": ("landscape_review_batches", "UNIQUE (issued_on)"),
    "uq_landscape_review_batch_items_batch_opportunity": (
        "landscape_review_batch_items", "UNIQUE (batch_id, opportunity_id)",
    ),
    "landscape_review_batch_items_batch_id_fkey": (
        "landscape_review_batch_items", "REFERENCES landscape_review_batches(id) ON DELETE CASCADE",
    ),
    "landscape_review_batch_items_opportunity_id_fkey": (
        "landscape_review_batch_items", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "uq_landscape_expert_judgments_opportunity_expert_quarter": (
        "landscape_expert_judgments", "UNIQUE (opportunity_id, expert_id, quarter)",
    ),
    "landscape_expert_judgments_opportunity_id_fkey": (
        "landscape_expert_judgments", "REFERENCES landscape_opportunities(id) ON DELETE CASCADE",
    ),
    "landscape_expert_judgments_expert_id_fkey": (
        "landscape_expert_judgments", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "landscape_expert_judgments_landscape_assessment_id_fkey": (
        "landscape_expert_judgments", "REFERENCES landscape_assessments(id) ON DELETE SET NULL",
    ),
    "uq_landscape_reject_audits_item_epoch": (
        "landscape_reject_audits", "UNIQUE (item_kind, item_id, epoch_id)",
    ),
    "ck_landscape_reject_audits_status": ("landscape_reject_audits", "'failed:%'"),
    "landscape_reject_audits_epoch_id_fkey": (
        "landscape_reject_audits", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_reject_audits_batch_item_id_fkey": (
        "landscape_reject_audits", "REFERENCES landscape_batch_items(id) ON DELETE SET NULL",
    ),
    "landscape_rescues_epoch_id_fkey": (
        "landscape_rescues", "REFERENCES landscape_epochs(id) ON DELETE CASCADE",
    ),
    "landscape_rescues_actor_id_fkey": (
        "landscape_rescues", "REFERENCES users(id) ON DELETE SET NULL",
    ),
    "uq_landscape_llm_calls_batch_seq": ("landscape_llm_calls", "UNIQUE (batch_id, item_seq)"),
    "landscape_llm_calls_batch_id_fkey": (
        "landscape_llm_calls", "REFERENCES landscape_batches(id) ON DELETE RESTRICT",
    ),
    "uq_landscape_reports_kind_created": ("landscape_reports", "UNIQUE (kind, created_at)"),
    "ck_landscape_reports_kind": ("landscape_reports", "'metrics'"),
}

EXPECTED_ENUMS: dict[str, tuple[str, ...]] = {
    "pi_dm_direction_enum": ("inbound", "outbound"),
    "sim_command_enum": ("start", "stop", "system"),
    "sim_command_status_enum": ("pending", "done", "failed", "stale"),
}

#: (enum type, label) -> the revision that added the label to a type an earlier revision
#: created (``ALTER TYPE … ADD VALUE``): below that revision it is not expected yet.
_LABEL_REVISION: dict[tuple[str, str], str] = {
    ("sim_command_enum", "system"): "0074",
}

#: enum type name -> the revision that creates it, from preflight.PLANNED_OBJECTS.
_TYPE_REVISION: dict[str, str] = {
    o.name: o.revision for o in _pf.PLANNED_OBJECTS if o.kind == "type"
}


def expected_enums_for(target: str) -> dict[str, tuple[str, ...]]:
    """The EXPECTED_ENUMS entries a database at ``target`` must already have.

    A type whose creating revision is after ``target`` is not expected yet: 0042's two
    types must not fail a ``--target 0041`` run. Nor is a label a later revision added
    (``_LABEL_REVISION``): 0074's ``system`` must not fail a ``--target 0073`` run. An
    unknown ``target``, or a type with no PLANNED_OBJECTS entry, keeps the entry, so the
    check errs towards expecting more.
    """
    order = _pf.REVISION_ORDER
    if target not in order:
        return dict(EXPECTED_ENUMS)
    hi = order.index(target)

    def reached(revision: str | None) -> bool:
        return revision not in order or order.index(revision) <= hi

    return {
        name: tuple(label for label in labels if reached(_LABEL_REVISION.get((name, label))))
        for name, labels in EXPECTED_ENUMS.items()
        if reached(_TYPE_REVISION.get(name))
    }

#: Columns whose NULLs would be a data defect even though the catalog forbids them.
#: Checked in the data as well as in the catalog: a hand-relaxed column is exactly the
#: kind of drift this script is for.
MUST_BE_NON_NULL = tuple(
    (t, c) for (t, c, _dt, nullable, _d) in EXPECTED_COLUMNS if not nullable
)

# ---------------------------------------------------------------------------
# ORM-drift classification.
# ---------------------------------------------------------------------------
# alembic's compare_metadata() reports differences in both directions. Since Phase 3
# the models declare every index and constraint the migrations create, so on a correctly
# migrated database it reports only the legacy ``users.is_admin`` column (remove_column).
# The ops that mean "the DB is MISSING something the ORM requires" are the ones that
# matter, and they are the ones a dropped column/index/table produces: verified by
# sabotage — dropping agent_messages.content yields add_column, dropping
# ix_agent_messages_run_created yields add_index, and relaxing sender_name's NOT NULL
# yields modify_nullable. remove_index/remove_constraint stay ignorable in general, except
# for the names in DRIFT_ENFORCED_NAMES.
DRIFT_FAIL_OPS = frozenset(
    {
        "add_table",
        "add_column",
        "add_index",
        "add_constraint",
        "modify_nullable",
        "modify_type",
        "remove_table",
    }
)
DRIFT_IGNORED_OPS = frozenset(
    {"remove_index", "remove_constraint", "add_table_comment", "remove_column"}
)
#: 0056's objects: a remove_index/remove_constraint op naming one of these means
#: the MODEL stopped declaring it (DB-08), which must fail rather than be ignored.
DRIFT_ENFORCED_NAMES = frozenset({
    "uq_jobs_one_active_per_user_type", "uq_slack_app_provisions_agent", "uq_publications_user_pmid",
    "uq_users_email_lower", "ix_agent_messages_agent_phase", "ix_chat_usage_streaming",
})

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

FOREIGN_KEYS_SQL = """
SELECT con.conname AS name,
       src.relname AS child_table,
       (SELECT array_agg(a.attname ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = u.attnum
       ) AS child_cols,
       tgt.relname AS parent_table,
       (SELECT array_agg(a.attname ORDER BY u.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = u.attnum
       ) AS parent_cols,
       con.convalidated AS validated
  FROM pg_constraint con
  JOIN pg_class src ON src.oid = con.conrelid
  JOIN pg_class tgt ON tgt.oid = con.confrelid
  JOIN pg_namespace ns ON ns.oid = con.connamespace
 WHERE con.contype = 'f' AND ns.nspname = 'public'
 ORDER BY src.relname, con.conname
"""

INVALID_INDEXES_SQL = """
SELECT c.relname AS index_name,
       t.relname AS table_name,
       i.indisvalid AS is_valid,
       i.indisready AS is_ready,
       i.indislive AS is_live
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  JOIN pg_class t ON t.oid = i.indrelid
  JOIN pg_namespace ns ON ns.oid = c.relnamespace
 WHERE ns.nspname = 'public'
   AND NOT (i.indisvalid AND i.indisready AND i.indislive)
 ORDER BY c.relname
"""

COLUMNS_SQL = """
SELECT table_name, column_name, data_type, is_nullable, coalesce(column_default, '') AS def
  FROM information_schema.columns
 WHERE table_schema = 'public'
"""

INDEXES_SQL = "SELECT indexname AS name, indexdef AS def FROM pg_indexes WHERE schemaname='public'"

CONSTRAINTS_SQL = """
SELECT con.conname AS name, rel.relname AS table_name,
       pg_get_constraintdef(con.oid) AS def
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_namespace ns ON ns.oid = con.connamespace
 WHERE ns.nspname = 'public'
"""

ENUMS_SQL = """
SELECT t.typname AS name,
       array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
  FROM pg_type t
  JOIN pg_enum e ON e.enumtypid = t.oid
  JOIN pg_namespace ns ON ns.oid = t.typnamespace
 WHERE ns.nspname = 'public'
 GROUP BY t.typname
"""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def check_revision(conn, target: str):
    rev = await current_revision(conn)
    title = "alembic_version is exactly the target revision"
    if rev == target:
        return (
            title,
            PASS,
            f"alembic_version = {rev!r}. NOTE: this proves nothing on its own — see the "
            "schema checks below.",
            [],
            {"current_revision": rev, "target": target},
        )
    if rev is None:
        return (
            title,
            FAIL,
            "no alembic_version row (or no alembic_version table). If `alembic upgrade` "
            "just printed a successful chain, this is the silent-rollback signature: the "
            "harness never committed.",
            [
                "Check the harness, not the data:",
                "  python scripts/migrate/preflight.py   # check 8",
                "Then re-run the upgrade and re-run this script.",
            ],
            {"current_revision": None, "target": target},
        )
    return (
        title,
        FAIL,
        f"alembic_version = {rev!r}, expected {target!r}.",
        [f"  python -m alembic upgrade {target}", "then re-run this script."],
        {"current_revision": rev, "target": target},
    )


async def check_expected_columns(conn):
    title = "Every column 0019/0020/0023 adds exists, with the right type and nullability"
    live = {
        (r["table_name"], r["column_name"]): r
        for r in await fetch_all(conn, COLUMNS_SQL)
    }
    problems: list[str] = []
    for table, column, dtype, nullable, default in EXPECTED_COLUMNS:
        row = live.get((table, column))
        if row is None:
            problems.append(f"{table}.{column} is MISSING")
            continue
        if row["data_type"] != dtype:
            problems.append(
                f"{table}.{column} type is {row['data_type']!r}, expected {dtype!r}"
            )
        live_nullable = row["is_nullable"] == "YES"
        if live_nullable != nullable:
            problems.append(
                f"{table}.{column} is {'NULL' if live_nullable else 'NOT NULL'}able, "
                f"expected {'nullable' if nullable else 'NOT NULL'}"
            )
        if default is not None and row["def"] != default:
            problems.append(
                f"{table}.{column} default is {row['def']!r}, expected {default!r}"
            )
    if problems:
        return (
            title,
            FAIL,
            "\n".join([f"{len(problems)} column problem(s):"] + [f"  {p}" for p in problems]),
            ["The schema does not match the migrations. Do NOT let the app start against "
             "it. Restore from the backup, or work out which revision really ran:",
             "  python scripts/migrate/preflight.py --json"],
            {"problems": problems},
        )
    return (
        title,
        PASS,
        f"{len(EXPECTED_COLUMNS)} columns checked (type, nullability, server default).",
        [],
        {"checked": len(EXPECTED_COLUMNS)},
    )


async def check_expected_tables(conn):
    title = "Every table 0020/0022 creates exists"
    missing = [t for t in EXPECTED_TABLES if not await table_exists(conn, t)]
    if missing:
        return (
            title,
            FAIL,
            f"missing table(s): {', '.join(missing)}",
            ["  python -m alembic upgrade 0023", "then re-run this script."],
            {"missing": missing},
        )
    return (title, PASS, f"{len(EXPECTED_TABLES)} tables present: {', '.join(EXPECTED_TABLES)}.",
            [], {"checked": list(EXPECTED_TABLES)})


async def check_expected_indexes(conn):
    title = "Every index 0019/0020/0021/0022 creates exists, on the right columns"
    live = {r["name"]: r["def"] for r in await fetch_all(conn, INDEXES_SQL)}
    problems: list[str] = []
    for name, tail in EXPECTED_INDEXES.items():
        if name not in live:
            problems.append(f"{name} is MISSING")
        elif tail not in live[name]:
            problems.append(f"{name} definition is {live[name]!r}, expected to contain {tail!r}")
    if problems:
        return (
            title,
            FAIL,
            "\n".join([f"{len(problems)} index problem(s):"] + [f"  {p}" for p in problems]),
            ["Recreate the missing index(es) by hand, or restore and re-migrate. The "
             "partial index ix_agent_messages_run_slack_ts must keep its "
             "WHERE (slack_ts IS NOT NULL) predicate — without it the index is a "
             "different, much larger object that no query planner will use the same way."],
            {"problems": problems},
        )
    return (title, PASS, f"{len(EXPECTED_INDEXES)} indexes checked, definitions match.", [],
            {"checked": len(EXPECTED_INDEXES)})


async def check_expected_constraints(conn):
    title = "Constraints 0019/0022 add exist with the right definition"
    live = {r["name"]: (r["table_name"], r["def"]) for r in await fetch_all(conn, CONSTRAINTS_SQL)}
    problems: list[str] = []
    for name, (table, expect) in EXPECTED_CONSTRAINTS.items():
        got = live.get(name)
        if got is None:
            problems.append(f"{name} on {table} is MISSING")
        elif got[0] != table:
            problems.append(f"{name} is on {got[0]!r}, expected {table!r}")
        elif expect not in got[1]:
            problems.append(f"{name} def is {got[1]!r}, expected to contain {expect!r}")
    if problems:
        return (title, FAIL,
                "\n".join([f"{len(problems)} constraint problem(s):"] + [f"  {p}" for p in problems]),
                ["Without uq_agent_messages_run_ts the DB-primary write path loses its "
                 "idempotency key: the flush upserts on (simulation_run_id, message_ts) "
                 "and will start inserting duplicates instead."],
                {"problems": problems})
    return (title, PASS, f"{len(EXPECTED_CONSTRAINTS)} constraints checked.", [],
            {"checked": len(EXPECTED_CONSTRAINTS)})


async def check_enums(conn, target: str):
    """Every enum type created at or before ``target`` has exactly its expected values.

    0020 creates pi_dm_direction_enum inline in create_table, with no checkfirst; 0042
    creates the two simulation-command types.
    """
    title = "Enum types created up to the target have exactly the expected values"
    expected = expected_enums_for(target)
    live = {r["name"]: tuple(r["labels"]) for r in await fetch_all(conn, ENUMS_SQL)}
    problems: list[str] = []
    for name, labels in expected.items():
        got = live.get(name)
        if got is None:
            problems.append(f"type {name} is MISSING")
        elif got != labels:
            problems.append(f"type {name} has {list(got)}, expected {list(labels)}")
    if problems:
        return (title, FAIL,
                "\n".join(problems),
                ["An enum with extra values means someone ran ALTER TYPE ... ADD VALUE by "
                 "hand; note that added values cannot be removed, so the type must be "
                 "recreated:",
                 "  -- inspect first: SELECT DISTINCT direction FROM pi_dm_messages;"],
                {"problems": problems, "live": {k: list(v) for k, v in live.items()}})
    if not expected:
        return (title, PASS, f"no enum type is created at or before {target}.", [],
                {"expected": []})
    return (title, PASS,
            ", ".join(f"{k} = {list(v)}" for k, v in expected.items()) + ".",
            [], {"live": {k: list(v) for k, v in live.items() if k in expected}})


async def check_no_unintended_nulls(conn):
    """Catalog says NOT NULL, and the data agrees.

    The catalog check is the real one; the data check exists because it is the only
    thing that survives someone doing ``ALTER COLUMN ... DROP NOT NULL`` to make an
    insert work.
    """
    title = "No unintended NULLs in the columns the migrations declare NOT NULL"
    problems: list[str] = []
    counts: dict[str, int] = {}
    for table, column in MUST_BE_NON_NULL:
        if not await table_exists(conn, table):
            problems.append(f"{table} does not exist")
            continue
        enforced = await fetch_one_value(
            conn,
            "SELECT attnotnull FROM pg_attribute "
            f"WHERE attrelid='{table}'::regclass AND attname=:c",
            c=column,
        )
        n = int(await fetch_one_value(conn, f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL'))
        counts[f"{table}.{column}"] = n
        if not enforced:
            problems.append(f"{table}.{column} is not NOT NULL in the catalog")
        if n:
            problems.append(f"{table}.{column} has {n:,} NULL row(s)")
    if problems:
        return (title, FAIL, "\n".join(problems),
                ["Find and fix the rows, then re-apply the constraint:",
                 "  UPDATE <table> SET <col> = <default> WHERE <col> IS NULL;",
                 "  ALTER TABLE <table> ALTER COLUMN <col> SET NOT NULL;"],
                {"problems": problems, "null_counts": counts})
    return (title, PASS, f"{len(MUST_BE_NON_NULL)} NOT NULL columns verified in catalog and data.",
            [], {"null_counts": counts})


async def check_fk_integrity(conn):
    title = "No foreign-key orphans, and every FK is convalidated"
    fks = await fetch_all(conn, FOREIGN_KEYS_SQL)
    problems: list[str] = []
    orphan_counts: dict[str, int] = {}
    for fk in fks:
        if not fk["validated"]:
            problems.append(f"{fk['name']} on {fk['child_table']} is NOT VALIDATED")
        child_cols = list(fk["child_cols"] or [])
        parent_cols = list(fk["parent_cols"] or [])
        if not child_cols or len(child_cols) != len(parent_cols):
            continue
        not_null = " AND ".join(f'c."{c}" IS NOT NULL' for c in child_cols)
        join = " AND ".join(
            f'p."{p}" = c."{c}"' for c, p in zip(child_cols, parent_cols, strict=True)
        )
        n = int(
            await fetch_one_value(
                conn,
                f'SELECT count(*) FROM "{fk["child_table"]}" c WHERE {not_null} '
                f'AND NOT EXISTS (SELECT 1 FROM "{fk["parent_table"]}" p WHERE {join})',
            )
        )
        if n:
            orphan_counts[fk["name"]] = n
            problems.append(
                f"{fk['name']}: {n:,} row(s) in {fk['child_table']}"
                f"({', '.join(child_cols)}) with no {fk['parent_table']} parent"
            )
    if problems:
        return (title, FAIL, "\n".join(problems),
                ["Orphans mean a FK was created NOT VALID, or rows were inserted with "
                 "triggers disabled. Validate explicitly to see them all:",
                 "  ALTER TABLE <child> VALIDATE CONSTRAINT <name>;"],
                {"foreign_keys": len(fks), "problems": problems, "orphans": orphan_counts})
    return (title, PASS, f"{len(fks)} foreign keys: all convalidated, 0 orphans.", [],
            {"foreign_keys": len(fks)})


async def check_index_validity(conn):
    title = "No invalid indexes (pg_index.indisvalid / indisready / indislive)"
    bad = await fetch_all(conn, INVALID_INDEXES_SQL)
    if bad:
        return (title, FAIL,
                "\n".join(
                    f"  {r['index_name']} on {r['table_name']} "
                    f"valid={r['is_valid']} ready={r['is_ready']} live={r['is_live']}"
                    for r in bad
                ),
                ["An invalid index is not used by the planner and does not enforce "
                 "uniqueness. Rebuild it:",
                 "  REINDEX INDEX <name>;   -- or DROP and recreate"],
                {"invalid": bad})
    return (title, PASS, "every index in public is valid, ready and live.", [], {})


async def check_row_counts(
    conn, snapshot_path: str | None, allow_growth: bool, target: str, database_url: str
):
    """Compare live counts with the preflight snapshot over the snapshot's revision span.

    The tables legitimately absent before (created) or after (dropped) are derived from
    the snapshot's ``current_revision`` to ``target``, so a snapshot without that field
    cannot be compared and FAILs. ``allow_growth`` tolerates growth, including rows in a
    newly created table; loss and a missing table always fail.

    The snapshot must be of this run's database: its recorded host, port and database
    name (``database_identity``, else parsed from its redacted ``database_url``) must
    equal those of ``database_url``, or the check FAILs before comparing anything. A
    snapshot ``target`` that differs from ``target`` only WARNs: the comparison is still
    derived over ``current_revision..target``, but the preflight checks that produced
    the snapshot vetted a different span.
    """
    title = "Row counts match the preflight snapshot"
    counts = await _pf.snapshot_row_counts(conn)
    if not snapshot_path:
        return (title, WARN,
                f"no --snapshot given; counted {sum(counts.values()):,} rows across "
                f"{len(counts)} tables but had nothing to compare against.",
                ["Run preflight with --snapshot <path> before the migration, and pass the "
                 "same path here."],
                {"row_counts": counts})
    p = Path(snapshot_path)
    if not p.is_file():
        return (title, FAIL, f"snapshot {snapshot_path} does not exist.",
                ["The handoff file is the only record of the pre-migration counts. Without "
                 "it this run cannot show that no rows were lost."],
                {"row_counts": counts})
    try:
        payload = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        return (title, FAIL, f"snapshot {snapshot_path} is unreadable: {exc}", [],
                {"row_counts": counts})
    before_rev = payload.get("current_revision")
    if not before_rev:
        return (title, FAIL,
                f"snapshot {snapshot_path} records no current_revision, so the tables the "
                f"migration created or dropped on the way to {target} cannot be derived.",
                ["Re-take the snapshot with this tree's preflight (it records the stamped "
                 "revision) before migrating."],
                {"row_counts": counts})
    live_identity = _pf.database_identity(database_url)
    snap_identity = payload.get("database_identity")
    if not snap_identity and payload.get("database_url"):
        snap_identity = _pf.database_identity(payload["database_url"])
    if not snap_identity:
        return (title, FAIL,
                f"snapshot {snapshot_path} records no database identity, so it cannot be "
                "shown to be a snapshot of this database.",
                ["Re-take the snapshot with this tree's preflight against the database "
                 "you are migrating."],
                {"row_counts": counts})
    snap_identity = {k: snap_identity.get(k) for k in ("host", "port", "database")}
    if snap_identity != live_identity:
        return (title, FAIL,
                f"snapshot {snapshot_path} was taken against {_describe(snap_identity)}, "
                f"but this run is connected to {_describe(live_identity)}. Comparing "
                "them would be comparing two different databases.",
                ["Pass the snapshot preflight wrote for THIS database (a rehearsal "
                 "against a scratch DSN writes its own), or re-run preflight here before "
                 "migrating."],
                {"row_counts": counts, "snapshot_identity": snap_identity,
                 "live_identity": live_identity})
    snap_target = payload.get("target")
    target_note = None
    if snap_target != target:
        target_note = (
            f"snapshot target is {snap_target!r} but --target is {target!r}: the row "
            f"counts were compared over {before_rev}..{target}, but the preflight that "
            "wrote this snapshot vetted a different upgrade."
        )
    before = {k: int(v) for k, v in (payload.get("row_counts") or {}).items()}
    created = _pf.tables_created_between(before_rev, target)
    dropped = _pf.tables_dropped_between(before_rev, target)
    ok, problems = compare_row_counts(
        before,
        counts,
        allow_growth=allow_growth,
        expected_new=created,
        expected_dropped=dropped,
    )
    data = {
        "row_counts": counts,
        "snapshot_row_counts": before,
        "snapshot_revision": before_rev,
        "expected_new": sorted(created),
        "expected_dropped": sorted(dropped),
        "problems": problems,
        "snapshot_target": snap_target,
    }
    target_remediation = (
        [f"Re-run preflight with --target {target} before migrating, so its checks "
         "cover the upgrade that actually ran."]
        if target_note else []
    )
    if ok:
        detail = (
            f"{len(before)} tables, {sum(before.values()):,} rows, compared over "
            f"{before_rev}..{target}: no loss"
            + (", growth tolerated" if allow_growth else ", no growth")
            + (f", {len(created)} table(s) created empty" if created else "")
            + (f", {len(dropped)} table(s) dropped as planned" if dropped else "")
            + "."
        )
        if target_note:
            return (title, WARN, detail + " " + target_note, target_remediation, data)
        return (title, PASS, detail, [], data)
    lines = [f"{len(problems)} row-count problem(s):"] + [f"  {x}" for x in problems]
    if target_note:
        lines.append(target_note)
    return (title, FAIL,
            "\n".join(lines),
            ["Row loss is not something a migration in this chain can cause, so treat it as "
             "either the wrong snapshot file or a concurrent writer/deleter. Compare against "
             "the backup before doing anything else.",
             "Growth alone (not loss) can be accepted with --allow-row-growth, but only if "
             "you know a writer was live."] + target_remediation,
            data)


def _describe(identity: dict) -> str:
    """host:port/database, for naming a database in a report line without credentials."""
    return f"{identity.get('host')}:{identity.get('port')}/{identity.get('database')}"


async def check_orm_can_query(conn_url: str):
    """Import the real models at HEAD and run a real query per mapper.

    A schema that satisfies every catalog assertion above can still be unusable: the ORM
    selects every mapped column by name, so one missing column breaks every query against
    that model. This is the check that speaks for the application rather than the schema.
    """
    title = "The ORM at HEAD can query every model"
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    import src.models  # noqa: F401
    from src.database import Base

    mappers = sorted(Base.registry.mappers, key=lambda m: m.class_.__name__)
    engine = create_async_engine(conn_url, isolation_level="AUTOCOMMIT", pool_pre_ping=False)
    failures: list[str] = []
    checked: list[str] = []
    try:
        async with engine.connect() as c:
            for mapper in mappers:
                model = mapper.class_
                try:
                    await c.execute(select(model).limit(1))
                    checked.append(model.__name__)
                except Exception as exc:  # noqa: BLE001 - the message is the finding
                    first = str(exc).strip().splitlines()[0]
                    failures.append(f"{model.__name__} ({mapper.local_table}): {first}")
    finally:
        await engine.dispose()
    if failures:
        return (title, FAIL,
                "\n".join([f"{len(failures)} model(s) cannot be queried:"]
                          + [f"  {f}" for f in failures]),
                ["The application will fail on its first request against these models. Do "
                 "not start it. Fix the schema or restore."],
                {"failures": failures, "ok": checked})
    return (title, PASS, f"{len(checked)} models each returned from a real SELECT ... LIMIT 1.",
            [], {"ok": checked})


async def check_orm_drift(conn_url: str):
    """alembic autogenerate diff, filtered to "the DB is missing what the ORM needs"."""
    title = "No ORM drift (nothing the models require is absent from the database)"
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    import src.models  # noqa: F401
    from src.database import Base

    def _diff(sync_conn):
        return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)

    engine = create_async_engine(conn_url, isolation_level="AUTOCOMMIT", pool_pre_ping=False)
    try:
        async with engine.connect() as c:
            raw = await c.run_sync(_diff)
    finally:
        await engine.dispose()

    failures: list[str] = []
    ignored = 0
    unknown: list[str] = []
    for entry in raw:
        items = entry if isinstance(entry, list) else [entry]
        for item in items:
            op = item[0] if isinstance(item, (tuple, list)) else str(item)
            if op in DRIFT_FAIL_OPS:
                failures.append(f"{op}: {str(item)[:180]}")
            elif op in DRIFT_IGNORED_OPS and any(n in str(item) for n in DRIFT_ENFORCED_NAMES):
                failures.append(f"{op}: {str(item)[:180]}")
            elif op in DRIFT_IGNORED_OPS:
                ignored += 1
            else:
                unknown.append(f"{op}: {str(item)[:180]}")
    data = {"failures": failures, "ignored": ignored, "unclassified": unknown}
    if failures:
        return (title, FAIL,
                "\n".join([f"{len(failures)} drift finding(s) the models cannot tolerate:"]
                          + [f"  {f}" for f in failures]),
                ["Each add_column/add_index/add_table means the database lacks something a "
                 "model declares; modify_nullable/modify_type means it has it with the "
                 "wrong shape. Restore, or apply the missing DDL and re-run."],
                data)
    detail = (
        f"0 findings that matter; {ignored} pre-existing differences ignored (a remove_column "
        "for the legacy users.is_admin the User model replaces with a hybrid; measured: 1 on a "
        "correctly migrated database)."
    )
    if unknown:
        return (title, WARN, detail + f" {len(unknown)} unclassified op(s): {unknown}", [], data)
    return (title, PASS, detail, [], data)


async def run_postflight(args) -> Report:
    # warn_exit_code=0: postflight's contract is 0 = verified / 1 = failed, so a WARN is
    # reported loudly but does not fail the run.
    report = Report("postflight", warn_exit_code=0)
    url = resolve_database_url(args.database_url)
    report.extra["database_url"] = redact_url(url)
    report.extra["target"] = args.target

    engine, conn = await open_connection(url, args.statement_timeout_ms)
    try:
        # Every check is fenced: a verification script that dies with a traceback has
        # verified nothing, and reads to the operator as "the tool is broken" rather
        # than "the migration is broken".
        await report.add_guarded(
            "alembic_version is exactly the target revision",
            lambda: check_revision(conn, args.target),
        )

        async def _scripts():
            return await check_alembic_scripts(await current_revision(conn), args.target)

        await report.add_guarded("Exactly one alembic head, no duplicate revision ids", _scripts)
        await report.add_guarded(
            "Every table 0020/0022 creates exists", lambda: check_expected_tables(conn)
        )
        await report.add_guarded(
            "Every column 0019/0020/0023 adds exists, with the right type and nullability",
            lambda: check_expected_columns(conn),
        )
        await report.add_guarded(
            "Every index 0019/0020/0021/0022 creates exists, on the right columns",
            lambda: check_expected_indexes(conn),
        )
        await report.add_guarded(
            "Constraints 0019/0022 add exist with the right definition",
            lambda: check_expected_constraints(conn),
        )
        await report.add_guarded(
            "Enum types created up to the target have exactly the expected values",
            lambda: check_enums(conn, args.target),
        )
        await report.add_guarded(
            "No invalid indexes (pg_index.indisvalid / indisready / indislive)",
            lambda: check_index_validity(conn),
        )
        await report.add_guarded(
            "No unintended NULLs in the columns the migrations declare NOT NULL",
            lambda: check_no_unintended_nulls(conn),
        )
        await report.add_guarded(
            "No foreign-key orphans, and every FK is convalidated",
            lambda: check_fk_integrity(conn),
        )
        await report.add_guarded(
            "Row counts match the preflight snapshot",
            lambda: check_row_counts(
                conn, args.snapshot, args.allow_row_growth, args.target, url
            ),
        )
    finally:
        await conn.close()
        await engine.dispose()

    # These open their own engines: the ORM checks must run through SQLAlchemy's own
    # machinery, not this script's raw connection.
    await report.add_guarded(
        "No ORM drift (nothing the models require is absent from the database)",
        lambda: check_orm_drift(url),
    )
    await report.add_guarded(
        "The ORM at HEAD can query every model", lambda: check_orm_can_query(url)
    )
    return report


def build_parser():
    import argparse

    ap = argparse.ArgumentParser(
        prog="postflight",
        description="Post-migration verification. Exit 0 = verified, 1 = verification failed.",
    )
    add_common_arguments(ap)
    # Re-document --target: it moves the revision assertion, NOT the schema expectations.
    for action in ap._actions:  # noqa: SLF001 - argparse offers no public way to do this
        if action.dest == "target":
            action.help = (
                f"Revision alembic_version must equal (default {DEFAULT_TARGET}). The enum "
                "check and the row-count comparison are scoped to it; the table, column, "
                "index and constraint expectations describe 0023 and 0056, so "
                "--target 0019 will match the stamp and then correctly report everything "
                "0020-0023 and 0056 have not yet created."
            )
    ap.add_argument("--snapshot", default=None, help="Row-count snapshot written by preflight")
    ap.add_argument(
        "--allow-row-growth",
        action="store_true",
        help="Treat a table that GREW, or a newly created table holding rows, as "
        "acceptable (row loss and a missing table always fail).",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    import asyncio

    args = build_parser().parse_args(argv)
    report = asyncio.run(run_postflight(args))
    return _pf.emit(report, args)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    raise SystemExit(main())
