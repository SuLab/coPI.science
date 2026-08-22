#!/usr/bin/env python3
"""Pre-migration safety gate for the 0018/0019 -> 0023 upgrade.

Run this BEFORE `alembic upgrade`, against the database you are about to migrate.
It answers one question: *will this migration succeed, and what will it cost?*

    docker compose exec -T -e DATABASE_URL=... app python scripts/migrate/preflight.py

Exit codes (contract; other tooling depends on these):

    0  safe to migrate           — every check PASSed
    1  BLOCKED, do not migrate   — at least one check BLOCKed
    2  warnings only             — operator judgement required

Every non-PASS item prints the exact remediation command or SQL. Nothing here
writes to the database: the connection runs in AUTOCOMMIT so that preflight can
never itself become the open transaction that stalls the migration.

Why each check exists is documented at the check itself. The three that are easy
to underestimate:

  * Check 4 (duplicate ``(simulation_run_id, message_ts)``) is the hard blocker.
    Migration 0019 adds ``uq_agent_messages_run_ts``; Postgres reports only ONE
    duplicated key per failed index build, so the migration must be re-run once
    per duplicate group unless you enumerate them all up front. Check 4 does that
    in a single pass.
  * Check 3 exists because revision id ``0019`` is ambiguous in this repo's
    history. ``alembic/versions/0019_add_cohorts.py`` on the ``cohort-agent-isolation``
    branch (commit b00b0e6) also claimed ``revision = "0019"``, revising 0018. A
    database migrated from that branch is stamped ``0019`` while the *content*
    columns 0019 adds were never applied — and the chain then dies at 0022 with
    ``relation "cohorts" already exists``.
  * Check 8 checks the migration *harness*, not the data. If ``alembic/env.py``
    emits any SQL on the connection before ``context.begin_transaction()``, the
    connection autobegins, ``MigrationContext.__init__`` sets
    ``_in_external_transaction = True``, ``begin_transaction()`` degrades to a
    ``nullcontext()`` and alembic never commits — so ``alembic upgrade`` logs a
    full successful chain, exits 0, and applies NOTHING.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # `python scripts/migrate/preflight.py` puts scripts/migrate/ on sys.path, NOT the
    # repo root, so a bare `import src...` would resolve to the STALE copy of src/ that
    # the Dockerfile's `pip install .` baked into site-packages (verified: it predates
    # src/models/cohort.py entirely). Put the repo root first so /app/src wins.
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

PASS = "PASS"
WARN = "WARN"
BLOCK = "BLOCK"

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_WARN = 2

DEFAULT_TARGET = "0035"
#: Revisions this migration path has been exercised from.
#:
#: 0020 and 0021 are here because origin/main's own alembic head is 0021 (PR19). A
#: deployment that tracks main is therefore stamped 0021, and the first version of this
#: list — ("0018", "0019") — hard-BLOCKED exactly that state. The framing that produced
#: it ("migrate from 0018 or 0019") described where production was at the time, not where
#: main is.
#:
#: 0023, 0024, 0025, 0026, 0027, 0028, 0029 and 0033 were each added here for the same
#: reason: production's stamp at the time its target moved past them (0023 -> 0024, then
#: 0024 -> 0025, then 0025 -> 0026, then 0026 -> 0027, then 0027 -> 0028, then
#: 0028 -> 0029, then 0029 -> 0030, then 0030 -> 0031, then 0031 -> 0032, then
#: 0032 -> 0033, then 0033 -> 0034 — see git history on this constant). Each stays
#: supported afterward; nothing here narrows. An earlier version of this comment claimed
#: 0026 was "already done" and handled by the current == target branch of
#: revision_status() instead of by membership in this tuple — that stopped being true the
#: moment DEFAULT_TARGET moved past 0026 (first to 0027, then to 0028, then to 0029, then
#: to 0030 here), and the same went stale for 0027 the moment DEFAULT_TARGET moved past
#: IT, and again for 0028/0029, and again for 0033 the moment DEFAULT_TARGET moved to
#: 0034 and left 0033 out. Concretely: with DEFAULT_TARGET at 0034 and 0033 absent from
#: this tuple, a database stamped 0033 is neither current == target nor a supported
#: start, so revision_status() BLOCKS the very migration (0034) this task adds. Adding it
#: here is the fix.
#:
#: Starting at 0020/0021 is strictly safer than starting at 0018: uq_agent_messages_run_ts
#: already exists, so duplicates cannot be present and there is no 0019 index build to
#: wait on. All that remains is 0022 (three empty tables), 0023 (three columns on the small
#: researcher_profiles), 0024 (one column on agents), 0025 (one new table,
#: opportunity_assessments), 0026 (drop grantbot_posted_foas), 0027 (one new table,
#: assessment_drops), 0028 (one column + one constraint on users), 0029 (two columns on
#: opportunity_assessments), 0030 (one new table, specialist_consults, plus two columns on
#: opportunity_assessments), 0031 (data-only, no DDL), 0032 (one nullable JSONB column
#: on llm_call_logs), 0033 (two composite indexes on thread_decisions plus 18
#: unindexed ondelete-FK columns — see issue #25 P1) and 0034 (two nullable columns plus
#: one foreign-key constraint on agents).
SUPPORTED_START_REVISIONS = (
    "0018", "0019", "0020", "0021", "0023", "0024", "0025", "0026", "0027", "0028", "0029",
    "0030", "0031", "0032", "0033", "0034",
)

#: Start revisions at which migration 0019 has already run, so the expensive
#: ACCESS EXCLUSIVE index build on agent_messages is behind us.
POST_0019_STARTS = ("0020", "0021")

#: Tables whose row counts are snapshotted for postflight. Empty = every user table.
SNAPSHOT_SCHEMA = "public"

# ---------------------------------------------------------------------------
# Sizing calibration — measured, not guessed.
# ---------------------------------------------------------------------------
# Method: build a fixture at 0018, bulk-load N synthetic agent_messages rows, then
# run the 15-statement 0019+0021 DDL block for agent_messages inside ONE transaction
# in psql with \timing on, and sum the statement times. postgres:15, the compose
# postgres container, on this developer's machine, 2026-08-04:
#
#     rows        DDL block total   post-migration total relation size
#     10,011          112.0 ms       3,608 kB   (from 2,016 kB, +79%)
#     100,011         747.2 ms      34 MB       (from 19 MB,    +79%)
#     1,000,011     7,901.8 ms     339 MB       (from 188 MB,   +80%)
#
# Cross-check via wall clock of `alembic upgrade head` on the same three fixtures:
# 1.77 s / 2.21 s / 8.37 s against a 1.43 s measured no-op baseline (docker exec +
# interpreter start + connect + version probe), i.e. 0.34 / 0.78 / 6.94 s of work.
# Both methods agree to ~10%.
#
# Least squares over the three DDL-block points gives ~0.0079 ms/row with a ~33 ms
# intercept; rounded to the constants below. These are for an idle server with a warm
# cache, which is the FLOOR. CONTENTION_FACTOR is the upper bound quoted to the
# operator: a production server is doing other work and the index build competes for
# I/O and maintenance_work_mem.
LOCK_WINDOW_FIXED_MS = 50.0
LOCK_WINDOW_PER_ROW_MS = 0.0080
LOCK_WINDOW_CONTENTION_FACTOR = 3.0
#: Beyond this, quote the estimate as an extrapolation rather than a measurement.
LOCK_WINDOW_CALIBRATED_MAX_ROWS = 1_000_000
#: Upper-bound lock window above which the operator should schedule a window.
LOCK_WINDOW_WARN_MS = 10_000.0
#: 0019+0021 add four indexes to agent_messages. Measured +79%/+79%/+80% of the
#: pre-migration total relation size at the three scales above.
INDEX_GROWTH_FRACTION = 0.80
#: Above this much *new* index data, tell the operator to check free space by hand;
#: preflight cannot see the filesystem from inside Postgres.
INDEX_GROWTH_WARN_BYTES = 1 << 30  # 1 GiB

# ---------------------------------------------------------------------------
# Backup thresholds
# ---------------------------------------------------------------------------
DEFAULT_BACKUP_MAX_AGE_HOURS = 24.0
#: A 0-byte or truncated file. Measured: the smallest *real* dump of this schema
#: (empty database, gzipped) is 6,914 bytes; a schema-only dump is ~38 kB and is
#: essentially CONSTANT regardless of data volume, which is why size alone can never
#: prove a dump carries data — hence the data-section scan below.
DEFAULT_BACKUP_MIN_BYTES = 1024
#: How much of a text/gzip dump to scan for data sections. Bounded so preflight
#: cannot be made slow by a huge dump.
BACKUP_SCAN_BYTES = 64 << 20  # 64 MiB of decompressed text
#: Default places to look when --backup-path is not given.
DEFAULT_BACKUP_DIRS = ("backups", "data/backups", "/backups", "/var/backups/copi")
BACKUP_GLOBS = ("*.sql", "*.sql.gz", "*.dump", "*.dmp", "*.pgdump", "*.custom", "*.bak")

# ---------------------------------------------------------------------------
# What the migration chain CREATES, per revision. Derived by reading 0019-0025;
# tests/unit/test_migration_checks.py re-derives this from the migration files and
# asserts it still matches, so it cannot silently drift.
#
# Note: postflight.py's own EXPECTED_TABLES/EXPECTED_COLUMNS/EXPECTED_INDEXES (schema
# verification after upgrade) are still pinned to the 0019-0023 chain only, and it scopes
# its read of PLANNED_OBJECTS accordingly (see postflight.VERIFIED_REVISIONS) — it has not
# been extended to verify 0024/0025's objects yet. That is a separate, pre-existing gap;
# this collision check (below) covers every revision up to DEFAULT_TARGET regardless.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedObject:
    """A database object a migration will CREATE (so it must not already exist)."""

    revision: str
    kind: str  # 'table' | 'column' | 'index' | 'constraint' | 'type'
    name: str
    table: str | None = None


PLANNED_OBJECTS: tuple[PlannedObject, ...] = (
    # 0019_agent_message_content
    PlannedObject("0019", "column", "content", "agent_messages"),
    PlannedObject("0019", "column", "sender_name", "agent_messages"),
    PlannedObject("0019", "column", "is_bot", "agent_messages"),
    PlannedObject("0019", "column", "posted_at", "agent_messages"),
    PlannedObject("0019", "column", "slack_ts", "agent_messages"),
    PlannedObject("0019", "column", "slack_channel_id", "agent_messages"),
    PlannedObject("0019", "column", "slack_thread_ts", "agent_messages"),
    PlannedObject("0019", "constraint", "uq_agent_messages_run_ts", "agent_messages"),
    PlannedObject("0019", "index", "ix_agent_messages_run_posted", "agent_messages"),
    PlannedObject("0019", "index", "ix_agent_messages_run_channel_posted", "agent_messages"),
    PlannedObject("0019", "index", "ix_agent_messages_run_slack_ts", "agent_messages"),
    # 0020_pi_dm_messages
    PlannedObject("0020", "table", "pi_dm_messages"),
    PlannedObject("0020", "type", "pi_dm_direction_enum"),
    PlannedObject("0020", "index", "ix_pi_dm_run_agent_posted", "pi_dm_messages"),
    PlannedObject("0020", "index", "ix_pi_dm_run_direction_posted", "pi_dm_messages"),
    # 0021_inbox_cursor_created_at_indexes
    PlannedObject("0021", "index", "ix_agent_messages_run_created", "agent_messages"),
    PlannedObject("0021", "index", "ix_pi_dm_run_direction_created", "pi_dm_messages"),
    # 0022_add_cohorts
    PlannedObject("0022", "table", "cohorts"),
    PlannedObject("0022", "table", "cohort_memberships"),
    PlannedObject("0022", "table", "cohort_audit_events"),
    PlannedObject("0022", "constraint", "uq_cohort_membership_cohort_agent", "cohort_memberships"),
    PlannedObject("0022", "index", "ix_cohort_memberships_cohort_id", "cohort_memberships"),
    PlannedObject("0022", "index", "ix_cohort_memberships_agent_id", "cohort_memberships"),
    PlannedObject("0022", "index", "ix_cohort_audit_events_cohort_id", "cohort_audit_events"),
    PlannedObject("0022", "index", "ix_cohort_audit_events_created_at", "cohort_audit_events"),
    # 0023_profile_synthesis_provenance
    PlannedObject("0023", "column", "synthesis_validated", "researcher_profiles"),
    PlannedObject("0023", "column", "evidence_pmid_count", "researcher_profiles"),
    PlannedObject("0023", "column", "evidence_pub_count", "researcher_profiles"),
    # 0024_add_agent_role
    PlannedObject("0024", "column", "role", "agents"),
    # 0025_add_opportunity_assessments
    PlannedObject("0025", "table", "opportunity_assessments"),
    PlannedObject(
        "0025", "index", "ix_opportunity_assessments_simulation_run_id",
        "opportunity_assessments",
    ),
    PlannedObject(
        "0025", "index", "ix_opportunity_assessments_agent_id", "opportunity_assessments",
    ),
    # 0027_add_assessment_drops. 0026 creates nothing (it is a pure DROP of
    # grantbot_posted_foas), so it has no entries here — only CREATEs can collide.
    PlannedObject("0027", "table", "assessment_drops"),
    PlannedObject(
        "0027", "index", "ix_assessment_drops_simulation_run_id", "assessment_drops",
    ),
    PlannedObject("0027", "index", "ix_assessment_drops_reason", "assessment_drops"),
    # 0028_add_user_role
    PlannedObject("0028", "column", "user_role", "users"),
    PlannedObject("0028", "constraint", "ck_users_user_role", "users"),
    # 0029_add_panel_incomplete
    PlannedObject("0029", "column", "panel_incomplete", "opportunity_assessments"),
    PlannedObject("0029", "column", "missing_domains", "opportunity_assessments"),
    # 0030_specialist_consults_rubric_version
    PlannedObject("0030", "table", "specialist_consults"),
    PlannedObject(
        "0030", "index", "ix_specialist_consults_simulation_run_id", "specialist_consults",
    ),
    PlannedObject(
        "0030", "index", "ix_specialist_consults_thread_id", "specialist_consults",
    ),
    PlannedObject(
        "0030", "index", "ix_specialist_consults_subject_agent_id", "specialist_consults",
    ),
    PlannedObject("0030", "column", "rubric_version", "opportunity_assessments"),
    PlannedObject("0030", "column", "rubric_content_hash", "opportunity_assessments"),
    # 0031 is data-only (a JSONB-null normalizing UPDATE) and creates nothing, so it
    # has no entry here — deliberately, not by omission.
    PlannedObject("0032", "column", "call_stats", "llm_call_logs"),
    PlannedObject("0033", "index", "ix_thread_decisions_agent_a_outcome", "thread_decisions"),
    PlannedObject("0033", "index", "ix_thread_decisions_agent_b_outcome", "thread_decisions"),
    PlannedObject("0033", "index", "ix_access_allowlist_added_by_user_id", "access_allowlist"),
    PlannedObject("0033", "index", "ix_agent_delegates_invitation_id", "agent_delegates"),
    PlannedObject("0033", "index", "ix_agent_delegates_user_id", "agent_delegates"),
    PlannedObject("0033", "index", "ix_agents_approved_by", "agents"),
    PlannedObject(
        "0033", "index", "ix_delegate_invitations_accepted_by_user_id", "delegate_invitations",
    ),
    PlannedObject(
        "0033", "index", "ix_delegate_invitations_invited_by_user_id", "delegate_invitations",
    ),
    PlannedObject(
        "0033", "index", "ix_email_notifications_agent_registry_id", "email_notifications",
    ),
    PlannedObject(
        "0033", "index", "ix_email_notifications_thread_decision_id", "email_notifications",
    ),
    PlannedObject(
        "0033", "index", "ix_private_channel_members_added_by_user_id", "private_channel_members",
    ),
    PlannedObject("0033", "index", "ix_private_channel_members_user_id", "private_channel_members"),
    PlannedObject("0033", "index", "ix_profile_revisions_changed_by_user_id", "profile_revisions"),
    PlannedObject("0033", "index", "ix_proposal_reviews_delegate_user_id", "proposal_reviews"),
    PlannedObject("0033", "index", "ix_proposal_reviews_reviewed_by_user_id", "proposal_reviews"),
    PlannedObject("0033", "index", "ix_proposal_reviews_user_id", "proposal_reviews"),
    PlannedObject(
        "0033", "index", "ix_slack_app_provisions_agent_registry_id", "slack_app_provisions",
    ),
    PlannedObject("0033", "index", "ix_cohorts_created_by", "cohorts"),
    PlannedObject("0033", "index", "ix_cohort_memberships_added_by", "cohort_memberships"),
    PlannedObject("0033", "index", "ix_cohort_audit_events_actor_id", "cohort_audit_events"),
    # 0034_agent_mute_tracking
    PlannedObject("0034", "column", "muted_at", "agents"),
    PlannedObject("0034", "column", "muted_by", "agents"),
    PlannedObject("0034", "constraint", "fk_agents_muted_by_users", "agents"),
    PlannedObject("0034", "index", "ix_agents_muted_by", "agents"),
)

REVISION_ORDER = (
    "0018", "0019", "0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027", "0028",
    "0029", "0030", "0031", "0032", "0033", "0034", "0035",
)


def planned_objects_between(current: str, target: str) -> tuple[PlannedObject, ...]:
    """Objects created by the revisions that will actually run for current -> target.

    A revision already applied cannot collide with itself, so its objects are excluded:
    at 0019 the content columns and ``uq_agent_messages_run_ts`` already exist and that
    is correct, not a collision.
    """
    try:
        lo = REVISION_ORDER.index(current)
    except ValueError:
        lo = 0
    try:
        hi = REVISION_ORDER.index(target)
    except ValueError:
        hi = len(REVISION_ORDER) - 1
    pending = set(REVISION_ORDER[lo + 1 : hi + 1])
    return tuple(o for o in PLANNED_OBJECTS if o.revision in pending)


# ---------------------------------------------------------------------------
# SQL — kept as module constants so the unit tests can pin them.
# ---------------------------------------------------------------------------

#: Every duplicate group in ONE pass, with the row ids. NULL message_ts is excluded
#: because Postgres UNIQUE treats NULLs as distinct (no NULLS NOT DISTINCT here), so
#: many NULL-ts rows in one run are legal — verified: three such rows coexist with
#: uq_agent_messages_run_ts, and the remediation that NULLs the extras migrates clean.
DUPLICATE_GROUPS_SQL = """
SELECT simulation_run_id::text AS run_id,
       message_ts,
       count(*) AS n,
       array_agg(id::text ORDER BY created_at, id) AS ids
  FROM agent_messages
 WHERE message_ts IS NOT NULL
 GROUP BY simulation_run_id, message_ts
HAVING count(*) > 1
 ORDER BY count(*) DESC, message_ts
"""

#: Preferred remediation: the extra rows are duplicate ingestions of one message, and
#: (run, ts) is an idempotency key. Verified end to end: applying this to a fixture
#: with three duplicate groups (sizes 3, 2, 2) let `alembic upgrade head` reach 0023.
DEDUPE_DELETE_SQL = """\
-- Remediation A (preferred): keep the earliest row of each duplicate group.
BEGIN;
WITH ranked AS (
  SELECT id, row_number() OVER (
           PARTITION BY simulation_run_id, message_ts
           ORDER BY created_at, id) AS rn
    FROM agent_messages
   WHERE message_ts IS NOT NULL
)
DELETE FROM agent_messages WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
COMMIT;"""

#: Alternative that deletes nothing. NULL is exempt from the unique constraint, so the
#: extra rows survive with their canonical id cleared. Verified end to end: 18 rows in,
#: 18 rows out, 11 keeping a message_ts, upgrade reached 0023.
DEDUPE_NULL_SQL = """\
-- Remediation B (loses no rows; the extras lose their canonical message_ts, so they
-- can no longer be matched to a Slack message or upserted idempotently).
BEGIN;
WITH ranked AS (
  SELECT id, row_number() OVER (
           PARTITION BY simulation_run_id, message_ts
           ORDER BY created_at, id) AS rn
    FROM agent_messages
   WHERE message_ts IS NOT NULL
)
UPDATE agent_messages SET message_ts = NULL WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
COMMIT;"""

BLOCKING_SESSIONS_SQL = """
SELECT a.pid,
       a.state,
       a.usename,
       a.application_name,
       coalesce(extract(epoch FROM (now() - a.xact_start)), 0)::float AS xact_age_s,
       coalesce(extract(epoch FROM (now() - a.query_start)), 0)::float AS query_age_s,
       coalesce(a.query, '') AS query,
       EXISTS (
         SELECT 1 FROM pg_locks l
          WHERE l.pid = a.pid
            -- to_regclass, not 'agent_messages'::regclass: the cast raises
            -- UndefinedTable on a database where the table does not exist yet, which
            -- would make preflight crash instead of reporting. NULL compares false.
            AND l.relation = to_regclass('public.agent_messages')
       ) AS holds_agent_messages_lock
  FROM pg_stat_activity a
 WHERE a.datname = current_database()
   AND a.pid <> pg_backend_pid()
   AND a.backend_type = 'client backend'
   -- Exclude our own tooling by name as well as by pid, so a concurrently-running
   -- preflight/postflight is not reported as a blocker of the migration it is checking.
   AND coalesce(a.application_name, '') <> :app_name
   AND (a.xact_start IS NOT NULL OR a.state <> 'idle')
 ORDER BY a.xact_start NULLS LAST
"""

#: Set on our own connections and excluded from BLOCKING_SESSIONS_SQL.
APPLICATION_NAME = "copi_migration_check"

ROW_COUNT_SQL = """
SELECT c.relname AS table_name
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = :schema
   AND c.relkind = 'r'
 ORDER BY c.relname
"""

# ---------------------------------------------------------------------------
# Pure helpers (no database, no I/O) — unit tested in tests/unit/test_migration_checks.py
# ---------------------------------------------------------------------------


def exit_code_for(statuses: list[str]) -> int:
    """BLOCK dominates WARN dominates PASS."""
    if BLOCK in statuses:
        return EXIT_BLOCKED
    if WARN in statuses:
        return EXIT_WARN
    return EXIT_OK


def worst_status(statuses: list[str]) -> str:
    if BLOCK in statuses:
        return BLOCK
    if WARN in statuses:
        return WARN
    return PASS


def worst_status_exit(statuses: list[str]) -> int:
    """The exit code the statuses *mean*, ignoring any per-script remapping of WARN."""
    return exit_code_for(statuses)


def estimate_lock_window_ms(rows: int) -> tuple[float, float]:
    """(floor, ceiling) milliseconds of ACCESS EXCLUSIVE on agent_messages.

    The whole 0019..0023 chain runs in ONE transaction (env.py does not pass
    ``transaction_per_migration``), so the lock 0019's first ``ADD COLUMN`` takes is
    held until the final commit: the lock window is the chain, not the index build.
    """
    floor_ms = LOCK_WINDOW_FIXED_MS + LOCK_WINDOW_PER_ROW_MS * max(rows, 0)
    return floor_ms, floor_ms * LOCK_WINDOW_CONTENTION_FACTOR


def sizing_status(rows: int, ceiling_ms: float) -> tuple[str, str]:
    """PASS for a sub-10s window; WARN once a maintenance window is warranted."""
    if rows > LOCK_WINDOW_CALIBRATED_MAX_ROWS:
        return (
            WARN,
            f"{rows:,} rows is beyond the calibrated range (<= "
            f"{LOCK_WINDOW_CALIBRATED_MAX_ROWS:,}); the estimate is an extrapolation.",
        )
    if ceiling_ms > LOCK_WINDOW_WARN_MS:
        return (
            WARN,
            f"worst-case lock window {ceiling_ms / 1000:.1f}s exceeds "
            f"{LOCK_WINDOW_WARN_MS / 1000:.0f}s — schedule a window, do not migrate hot.",
        )
    return PASS, f"worst-case lock window {ceiling_ms / 1000:.1f}s."


def revision_status(current: str | None, target: str) -> tuple[str, str]:
    """Classify the DB's stamped revision against the supported starting points."""
    if current is None:
        return (
            BLOCK,
            "no alembic_version row (or no alembic_version table): this database has "
            "never been stamped, so alembic would replay the chain from 0001 over "
            "whatever schema is already there.",
        )
    if current == target:
        return PASS, f"already at the target revision {target}; migration is a no-op."
    if current in SUPPORTED_START_REVISIONS:
        return PASS, f"at {current}, a supported starting point."
    return (
        BLOCK,
        f"at {current}, which is not a supported starting point "
        f"({', '.join(SUPPORTED_START_REVISIONS)}) nor the target {target}.",
    )


def resolve_lock_timeout_ms(env: dict[str, str], env_py_source: str | None) -> tuple[str, str]:
    """What lock_timeout the migration will actually run with, and where it came from."""
    if "ALEMBIC_LOCK_TIMEOUT_MS" in env:
        return env["ALEMBIC_LOCK_TIMEOUT_MS"], "ALEMBIC_LOCK_TIMEOUT_MS in the environment"
    if env_py_source:
        m = re.search(
            r"""ALEMBIC_LOCK_TIMEOUT_MS["']?\s*,\s*["'](\d+)["']""", env_py_source
        )
        if m:
            return m.group(1), "alembic/env.py default"
        if "lock_timeout" not in env_py_source:
            return "0", "alembic/env.py sets no lock_timeout (Postgres default: wait forever)"
    return "unknown", "could not determine"


def harness_findings(env_py_source: str) -> list[str]:
    """Statements in ``do_run_migrations`` that autobegin a transaction too early.

    If the connection is already in a transaction when ``context.configure()`` builds
    the MigrationContext, ``_in_external_transaction`` is set and
    ``begin_transaction()`` returns a ``nullcontext()``. Alembic then assumes the caller
    owns the transaction; env.py's caller (``run_async_migrations``) exits its
    ``connect()`` block without committing, so the DDL is rolled back and
    ``alembic upgrade`` still exits 0 with a full "Running upgrade" log.

    Verified on this repo: with ``lock_timeout`` set this way, ``alembic upgrade 0018``
    reported nine successful revisions and left the database with zero tables; with
    ``ALEMBIC_LOCK_TIMEOUT_MS=0`` (which skips the statement) the same command left 25
    tables and version 0018.
    """
    autobegin_methods = {"execute", "exec_driver_sql", "scalar", "scalars", "begin"}
    try:
        tree = ast.parse(env_py_source)
    except SyntaxError as exc:  # pragma: no cover - defensive
        return [f"could not parse alembic/env.py: {exc}"]

    findings: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name != "do_run_migrations":
            continue
        conn_names = {a.arg for a in func.args.args}
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute):
                continue
            # Stop looking once we reach the demarcation call itself.
            if fn.attr in {"configure", "begin_transaction"}:
                continue
            if fn.attr in autobegin_methods and isinstance(fn.value, ast.Name):
                if fn.value.id in conn_names:
                    findings.append(
                        f"alembic/env.py:{node.lineno} calls "
                        f"{fn.value.id}.{fn.attr}(...) inside do_run_migrations before "
                        "context.begin_transaction(), which autobegins a transaction and "
                        "makes alembic skip its own commit"
                    )
    return findings


def legacy_inventory_status(recoverable: int, unrecoverable: int) -> tuple[str, str]:
    """Rows that end up with ``content = ''``: Slack-recoverable vs gone for good."""
    if recoverable == 0 and unrecoverable == 0:
        return PASS, "no rows will be left with an empty content column."
    parts = []
    if recoverable:
        parts.append(f"{recoverable:,} Slack-recoverable (channel_id NOT LIKE 'local:%')")
    if unrecoverable:
        parts.append(f"{unrecoverable:,} PERMANENTLY UNRECOVERABLE (channel_id LIKE 'local:%')")
    return WARN, "; ".join(parts) + "."


#: An `active` session holding a transaction younger than this will release its locks on
#: its own before it matters; older than this and it is indistinguishable from a stuck
#: one. Idle-in-transaction is BLOCKed at any age, because nothing will end it.
DEFAULT_MAX_TOLERABLE_XACT_AGE_S = 5.0


def blocking_sessions_status(
    sessions: list[dict], max_xact_age_s: float = DEFAULT_MAX_TOLERABLE_XACT_AGE_S
) -> tuple[str, str]:
    """BLOCK on idle-in-transaction (any age) or a long open transaction; WARN otherwise.

    An idle-in-transaction reader is enough to stall the whole table: verified — one
    ``BEGIN; SELECT count(*) FROM agent_messages;`` left idle made
    ``alembic upgrade head`` from 0018 fail on ``lock_timeout`` after 3s, and while the
    ACCESS EXCLUSIVE request sat ungranted in the queue a brand-new
    ``SELECT count(*)`` from a third session timed out too, because a pending
    ACCESS EXCLUSIVE queues AHEAD of new readers.
    """
    in_txn = [s for s in sessions if (s.get("xact_age_s") or 0) > 0]
    idle_in_txn = [s for s in in_txn if str(s.get("state", "")).startswith("idle in transaction")]
    if idle_in_txn:
        return (
            BLOCK,
            f"{len(idle_in_txn)} session(s) idle in transaction. A pending "
            "ACCESS EXCLUSIVE request queues ahead of new readers, so this migration "
            "would stall every query on agent_messages until the idle transaction ends.",
        )
    long_txn = [s for s in in_txn if (s.get("xact_age_s") or 0) > max_xact_age_s]
    if long_txn:
        return (
            BLOCK,
            f"{len(long_txn)} session(s) with a transaction open longer than "
            f"{max_xact_age_s:.0f}s. Any of them can hold a lock that conflicts with "
            "0019's ACCESS EXCLUSIVE on agent_messages.",
        )
    if in_txn:
        return (
            WARN,
            f"{len(in_txn)} session(s) with a transaction open for under "
            f"{max_xact_age_s:.0f}s. Short enough to release on its own, but it means the "
            "database is still being used; stop the writers before migrating.",
        )
    if sessions:
        return (
            WARN,
            f"{len(sessions)} active session(s) with no open transaction. They will be "
            "blocked (not blocking) for the duration of the lock window; a writer that "
            "retries is fine, one that raises is not.",
        )
    return PASS, "no other client sessions on this database."


@dataclass
class BackupFacts:
    """Everything the backup check needs to decide, gathered by ``inspect_backup``."""

    path: str | None = None
    exists: bool = False
    size_bytes: int = 0
    age_hours: float | None = None
    fmt: str = "unknown"  # 'plain' | 'gzip' | 'custom' | 'unknown'
    scanned: bool = False
    has_agent_messages_ddl: bool = False
    has_agent_messages_data: bool = False
    read_error: str | None = None
    override_reason: str | None = None
    live_agent_messages_rows: int = 0


def evaluate_backup(
    facts: BackupFacts,
    max_age_hours: float = DEFAULT_BACKUP_MAX_AGE_HOURS,
    min_bytes: int = DEFAULT_BACKUP_MIN_BYTES,
) -> tuple[str, list[str]]:
    """BLOCK unless a recent, data-bearing dump is demonstrably present.

    This matters more than it looks: 0019's downgrade DROPs the content columns, so a
    rollback past 0019 destroys every message body written after the cutover. The dump
    is the only way back.

    Size alone cannot establish that a dump carries data. Measured on this schema: a
    ``--schema-only`` dump is ~38 kB at 100k rows AND at 1M rows, while a full gzipped
    dump is 7-9% of ``pg_database_size``. So the check looks for an actual data section.
    """
    notes: list[str] = []
    if facts.override_reason:
        return WARN, [
            "backup NOT verified by preflight; overridden with "
            f"--backup-verified-elsewhere={facts.override_reason!r}. "
            "Rollback past 0019 destroys agent_messages.content — be certain."
        ]
    if not facts.exists:
        return BLOCK, [
            "no backup found. Rollback past 0019 DROPs agent_messages.content, "
            "sender_name, is_bot and posted_at, so there is no way back without one.",
            "Take one and re-run:",
            "  docker compose exec -T postgres pg_dump -U copi -d copi | gzip "
            "> backups/copi_$(date +%Y%m%dT%H%M%S).sql.gz",
            "then pass --backup-path backups/<file>.",
        ]
    if facts.read_error:
        return BLOCK, [f"backup at {facts.path} could not be read: {facts.read_error}"]
    if facts.size_bytes < min_bytes:
        return BLOCK, [
            f"backup at {facts.path} is {facts.size_bytes:,} bytes, under the "
            f"{min_bytes:,}-byte floor — truncated or empty."
        ]
    if facts.age_hours is not None and facts.age_hours > max_age_hours:
        return BLOCK, [
            f"backup at {facts.path} is {facts.age_hours:.1f}h old, older than the "
            f"{max_age_hours:.0f}h threshold. Every message written since is not in it.",
            "  docker compose exec -T postgres pg_dump -U copi -d copi | gzip "
            "> backups/copi_$(date +%Y%m%dT%H%M%S).sql.gz",
        ]
    if facts.fmt == "custom":
        return WARN, [
            f"backup at {facts.path} is a pg_dump custom-format archive. Neither "
            "pg_restore nor psql is installed in the app image, so preflight cannot "
            "confirm it carries data. Verify by hand on the postgres container:",
            "  docker compose exec -T postgres pg_restore -l /path/to/dump "
            "| grep -c 'TABLE DATA'",
        ]
    if facts.fmt == "unknown":
        return BLOCK, [
            f"backup at {facts.path} is not a recognisable pg_dump output (not plain "
            "SQL, not gzip, no PGDMP magic). Point --backup-path at a real dump."
        ]
    if not facts.has_agent_messages_ddl:
        return BLOCK, [
            f"backup at {facts.path} contains no agent_messages definition at all — it "
            "is not a dump of this database."
        ]
    if facts.live_agent_messages_rows > 0 and not facts.has_agent_messages_data:
        return BLOCK, [
            f"backup at {facts.path} defines agent_messages but contains NO data section "
            f"for it, while the live table holds {facts.live_agent_messages_rows:,} rows. "
            "This is a --schema-only dump; restoring it would lose every message.",
            "  docker compose exec -T postgres pg_dump -U copi -d copi | gzip "
            "> backups/copi_$(date +%Y%m%dT%H%M%S).sql.gz",
        ]
    notes.append(
        f"{facts.path} — {facts.size_bytes:,} bytes, "
        f"{'age unknown' if facts.age_hours is None else f'{facts.age_hours:.1f}h old'}, "
        f"format {facts.fmt}, agent_messages data section present."
    )
    return PASS, notes


def compare_row_counts(
    before: dict[str, int],
    after: dict[str, int],
    allow_growth: bool = False,
    expected_new: tuple[str, ...] | frozenset[str] = (),
) -> tuple[bool, list[str]]:
    """Compare a preflight snapshot against a postflight count. Shared by both scripts.

    Shrinkage is always a failure. Growth is a failure unless ``allow_growth``, because
    the migration itself inserts no rows: if a count went up, a writer was live during
    the migration and the lock-window analysis was wrong.

    ``expected_new`` names the tables the chain CREATES (0020's pi_dm_messages, 0022's
    three cohort tables). Those are absent from the preflight snapshot by construction,
    so flagging them would be crying wolf — but a table appearing that the chain does
    not create is still a finding.
    """
    problems: list[str] = []
    expected_new = frozenset(expected_new)
    for table in sorted(set(before) | set(after)):
        b = before.get(table)
        a = after.get(table)
        if b is None:
            if table in expected_new:
                continue
            problems.append(f"{table}: table did not exist before the migration, now {a:,} rows")
            continue
        if a is None:
            problems.append(f"{table}: existed before with {b:,} rows, now MISSING")
            continue
        if a < b:
            problems.append(f"{table}: {b:,} rows before, {a:,} after — {b - a:,} rows LOST")
        elif a > b and not allow_growth:
            problems.append(
                f"{table}: {b:,} rows before, {a:,} after — grew by {a - b:,}; the "
                "migration inserts nothing, so a writer was live"
            )
    return (not problems), problems


def normalize_async_url(url: str) -> str:
    """Force the asyncpg driver, whatever dialect spelling the caller used."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def redact_url(url: str) -> str:
    """Hide the password so the report is safe to paste into a ticket."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def scan_dump_text(text: str, table: str = "agent_messages") -> tuple[bool, bool]:
    """(defines the table, carries a data section for it) for a plain-SQL dump body."""
    has_ddl = bool(re.search(rf"CREATE TABLE (?:\w+\.)?{re.escape(table)}\b", text))
    has_data = bool(
        re.search(rf"^COPY (?:\w+\.)?{re.escape(table)}\b[^\n]*FROM stdin;", text, re.M)
        or re.search(rf"^INSERT INTO (?:\w+\.)?{re.escape(table)}\b", text, re.M)
    )
    return has_ddl, has_data


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    number: int
    title: str
    status: str
    detail: str = ""
    remediation: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            "data": self.data,
        }


class Report:
    #: Verdict wording per script. postflight has no "operator judgement" tier: its
    #: contract is 0 = verified / 1 = failed, so warnings there exit 0.
    VERDICTS = {
        "preflight": {
            EXIT_OK: "SAFE TO MIGRATE",
            EXIT_BLOCKED: "BLOCKED — DO NOT MIGRATE",
            EXIT_WARN: "WARNINGS ONLY — operator judgement required",
        },
        "postflight": {
            EXIT_OK: "VERIFIED",
            EXIT_BLOCKED: "VERIFICATION FAILED",
            EXIT_WARN: "VERIFIED WITH WARNINGS",
        },
    }

    def __init__(self, kind: str, warn_exit_code: int = EXIT_WARN) -> None:
        self.kind = kind
        self.warn_exit_code = warn_exit_code
        self.checks: list[CheckResult] = []
        self.extra: dict = {}

    def add(
        self,
        title: str,
        status: str,
        detail: str = "",
        remediation: list[str] | None = None,
        data: dict | None = None,
    ) -> CheckResult:
        res = CheckResult(
            number=len(self.checks) + 1,
            title=title,
            status=status,
            detail=detail,
            remediation=list(remediation or []),
            data=dict(data or {}),
        )
        self.checks.append(res)
        return res

    async def add_guarded(self, title: str, factory):
        """Run a check, turning any unexpected exception into a BLOCK item.

        A safety gate that dies with a traceback gives the operator no verdict at all,
        which is strictly worse than a loud failure: the temptation is then to migrate
        anyway because "the checker is broken". Every check is therefore fenced.
        """
        import inspect

        try:
            result = factory()
            if inspect.isawaitable(result):
                result = await result
            self.add(*result)
        except Exception as exc:  # noqa: BLE001 - the exception IS the finding
            self.add(
                title,
                BLOCK,
                f"this check could not be completed: {type(exc).__name__}: "
                f"{str(exc).strip().splitlines()[0] if str(exc).strip() else exc}",
                [
                    "Treat an incomplete check as a failed one. Re-run with the full "
                    "traceback to see why:",
                    "  python scripts/migrate/preflight.py --json  # then read the "
                    "stderr traceback",
                ],
                {"exception": type(exc).__name__},
            )

    @property
    def statuses(self) -> list[str]:
        return [c.status for c in self.checks]

    def exit_code(self) -> int:
        if BLOCK in self.statuses:
            return EXIT_BLOCKED
        if WARN in self.statuses:
            return self.warn_exit_code
        return EXIT_OK

    def render_text(self) -> str:
        lines: list[str] = []
        for c in self.checks:
            lines.append(f"{c.number:2d}. [{c.status:5s}] {c.title}")
            if c.detail:
                for para in c.detail.splitlines():
                    lines.append(f"          {para}")
            for r in c.remediation:
                for i, para in enumerate(r.splitlines()):
                    lines.append(("       --> " if i == 0 else "           ") + para)
        n_block = self.statuses.count(BLOCK)
        n_warn = self.statuses.count(WARN)
        lines.append("")
        table = self.VERDICTS.get(self.kind, self.VERDICTS["preflight"])
        # With warn_exit_code=0 a WARN run still exits 0, so pick the wording off the
        # statuses rather than the exit code or a warning would read as a clean pass.
        if BLOCK in self.statuses:
            verdict = table[EXIT_BLOCKED]
        elif WARN in self.statuses:
            verdict = table[EXIT_WARN]
        else:
            verdict = table[EXIT_OK]
        lines.append(
            f"{verdict}  ({len(self.checks)} checks, {n_block} "
            f"{'FAIL' if self.kind == 'postflight' else 'BLOCK'}, {n_warn} WARN, "
            f"exit {self.exit_code()})"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "exit_code": self.exit_code(),
            "verdict": {EXIT_OK: "ok", EXIT_BLOCKED: "blocked", EXIT_WARN: "warn"}[
                worst_status_exit(self.statuses)
            ],
            "checks": [c.to_dict() for c in self.checks],
            **self.extra,
        }


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def resolve_database_url(cli_value: str | None) -> str:
    """--database-url, else DATABASE_URL, else the app's configured URL."""
    if cli_value:
        return normalize_async_url(cli_value)
    env_value = os.environ.get("DATABASE_URL")
    if env_value:
        return normalize_async_url(env_value)
    from src.config import get_settings  # imported lazily: keeps the pure logic importable

    return normalize_async_url(get_settings().database_url)


async def open_connection(url: str, statement_timeout_ms: int):
    """An AUTOCOMMIT connection with bounded waits.

    AUTOCOMMIT matters: a checker that held an open transaction would itself become the
    thing that stalls the migration it just blessed. lock_timeout keeps our own catalog
    probes from queueing behind somebody else's DDL.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=False,
        connect_args={"server_settings": {"application_name": APPLICATION_NAME}},
    )
    conn = await engine.connect()
    await conn.execute(text(f"SET statement_timeout = {int(statement_timeout_ms)}"))
    await conn.execute(text("SET lock_timeout = 1000"))
    await conn.execute(text("SET idle_in_transaction_session_timeout = 5000"))
    return engine, conn


async def fetch_all(conn, sql: str, **params) -> list[dict]:
    from sqlalchemy import text

    result = await conn.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def fetch_one_value(conn, sql: str, **params):
    from sqlalchemy import text

    result = await conn.execute(text(sql), params)
    row = result.first()
    return None if row is None else row[0]


async def current_revision(conn) -> str | None:
    """The stamped revision, or None if the table is missing or empty."""
    exists = await fetch_one_value(
        conn, "SELECT to_regclass('public.alembic_version') IS NOT NULL"
    )
    if not exists:
        return None
    return await fetch_one_value(conn, "SELECT version_num FROM alembic_version LIMIT 1")


async def table_exists(conn, name: str) -> bool:
    return bool(await fetch_one_value(conn, f"SELECT to_regclass('public.{name}') IS NOT NULL"))


async def column_exists(conn, table: str, column: str) -> bool:
    return bool(
        await fetch_one_value(
            conn,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name=:c)",
            t=table,
            c=column,
        )
    )


async def existing_object_names(conn) -> dict[str, set[str]]:
    """One catalog sweep for every kind of name the chain will try to create."""
    out: dict[str, set[str]] = {"table": set(), "index": set(), "constraint": set(), "type": set()}
    for row in await fetch_all(
        conn,
        # relkind::text, NOT relkind. pg_class.relkind is Postgres' internal "char" type
        # and asyncpg decodes it to BYTES (b'r', b'i'), so `row["k"] == "r"` is silently
        # always False and this check would fail OPEN — passing every table and index
        # collision. Verified: without the cast, a pre-existing
        # ix_agent_messages_run_posted (which really does abort migration 0019 with
        # DuplicateTableError) was reported as no collision at all.
        "SELECT c.relname AS n, c.relkind::text AS k FROM pg_class c "
        "JOIN pg_namespace ns ON ns.oid = c.relnamespace WHERE ns.nspname='public'",
    ):
        if row["k"] == "r":
            out["table"].add(row["n"])
        elif row["k"] == "i":
            out["index"].add(row["n"])
    for row in await fetch_all(
        conn,
        "SELECT conname AS n FROM pg_constraint con "
        "JOIN pg_namespace ns ON ns.oid = con.connamespace WHERE ns.nspname='public'",
    ):
        out["constraint"].add(row["n"])
    for row in await fetch_all(
        conn,
        "SELECT t.typname AS n FROM pg_type t JOIN pg_namespace ns ON ns.oid = t.typnamespace "
        "WHERE ns.nspname='public' AND t.typtype='e'",
    ):
        out["type"].add(row["n"])
    return out


async def snapshot_row_counts(conn) -> dict[str, int]:
    """Exact counts for every user table. Exact, not reltuples: reltuples is an estimate
    that a fresh table reports as -1, which would make the postflight comparison a
    coin toss."""
    tables = [r["table_name"] for r in await fetch_all(conn, ROW_COUNT_SQL, schema=SNAPSHOT_SCHEMA)]
    counts: dict[str, int] = {}
    for t in tables:
        counts[t] = int(await fetch_one_value(conn, f'SELECT count(*) FROM public."{t}"'))
    return counts


# ---------------------------------------------------------------------------
# Backup discovery
# ---------------------------------------------------------------------------


def find_backup(path_arg: str | None) -> Path | None:
    """A file, the newest matching file in a directory, or the newest in the defaults."""
    candidates: list[Path] = []
    if path_arg:
        p = Path(path_arg)
        if p.is_file():
            return p
        if p.is_dir():
            for pattern in BACKUP_GLOBS:
                candidates.extend(p.glob(pattern))
        else:
            return None
    else:
        for d in DEFAULT_BACKUP_DIRS:
            base = Path(d) if Path(d).is_absolute() else REPO_ROOT / d
            if base.is_dir():
                for pattern in BACKUP_GLOBS:
                    candidates.extend(base.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def inspect_backup(path: Path | None, live_rows: int, override: str | None) -> BackupFacts:
    facts = BackupFacts(live_agent_messages_rows=live_rows, override_reason=override)
    if path is None:
        return facts
    facts.path = str(path)
    if not path.is_file():
        return facts
    facts.exists = True
    st = path.stat()
    facts.size_bytes = st.st_size
    facts.age_hours = max(0.0, (time.time() - st.st_mtime) / 3600.0)
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
        if head[:2] == b"\x1f\x8b":
            facts.fmt = "gzip"
            with gzip.open(path, "rt", errors="replace") as gz:
                body = gz.read(BACKUP_SCAN_BYTES)
        elif head[:5] == b"PGDMP":
            facts.fmt = "custom"
            return facts
        else:
            with path.open("rt", errors="replace") as fh:
                body = fh.read(BACKUP_SCAN_BYTES)
            facts.fmt = "plain" if ("CREATE TABLE" in body or "PostgreSQL database dump" in body) else "unknown"
            if facts.fmt == "unknown":
                return facts
        facts.scanned = True
        facts.has_agent_messages_ddl, facts.has_agent_messages_data = scan_dump_text(body)
    except (OSError, EOFError, UnicodeError) as exc:
        # EOFError, not OSError, is what a TRUNCATED gzip raises ("Compressed file ended
        # before the end-of-stream marker was reached") — and a truncated dump is exactly
        # the case this check exists for, so crashing on it would be the worst outcome.
        # gzip.BadGzipFile is an OSError subclass and is covered by the first arm.
        facts.read_error = f"{type(exc).__name__}: {exc}"
    return facts


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


async def run_preflight(args) -> Report:
    report = Report("preflight")
    url = resolve_database_url(args.database_url)
    report.extra["database_url"] = redact_url(url)
    report.extra["target"] = args.target
    report.extra["generated_at"] = time.time()

    engine, conn = await open_connection(url, args.statement_timeout_ms)
    try:
        # --- 1. current revision -------------------------------------------------
        try:
            rev = await current_revision(conn)
        except Exception as exc:  # noqa: BLE001
            report.add(
                "Stamped alembic revision is a supported starting point",
                BLOCK,
                f"could not read alembic_version: {type(exc).__name__}: {exc}",
                ["Check connectivity and permissions; nothing else here can be trusted "
                 "until this works."],
                {},
            )
            return report
        status, reason = revision_status(rev, args.target)
        remediation: list[str] = []
        if status == BLOCK:
            remediation = [
                "Confirm what is actually in the database before doing anything:",
                "  SELECT * FROM alembic_version;",
                "If the database is genuinely empty, create it from scratch instead of "
                "migrating: alembic upgrade head.",
                "If it is stamped at an unexpected revision, bring it to 0018 or 0019 "
                "first and re-run this preflight.",
            ]
        report.add(
            "Stamped alembic revision is a supported starting point",
            status,
            f"alembic_version = {rev!r}; target {args.target}. {reason}",
            remediation,
            {"current_revision": rev, "target": args.target},
        )
        report.extra["current_revision"] = rev

        # --- 2. single head, no duplicate revision ids ---------------------------
        await report.add_guarded(
            "Exactly one alembic head, no duplicate revision ids",
            lambda: check_alembic_scripts(rev, args.target),
        )

        # --- 3. is this 0019 the RIGHT 0019? -------------------------------------
        await report.add_guarded(
            "The 0019 stamp is the content 0019, not the cohort-branch 0019",
            lambda: check_ambiguous_revision(conn, rev),
        )

        # --- 4. THE HARD BLOCKER: duplicate (run, message_ts) -------------------
        await report.add_guarded(
            "No duplicate (simulation_run_id, message_ts) in agent_messages",
            lambda: check_duplicate_run_ts(conn, rev, args.max_duplicate_groups),
        )

        # --- 5. objects the chain will create that already exist ----------------
        await report.add_guarded(
            "Objects the pending revisions create do not already exist",
            lambda: check_name_collisions(conn, rev, args.target),
        )

        # --- 6. rows that make a downgrade past 0019 impossible -----------------
        await report.add_guarded(
            "Rows that would block a downgrade past 0019 (agent_messages.agent_id IS NULL)",
            lambda: check_downgrade_blockers(conn, rev),
        )

        # --- 7. blocking sessions ------------------------------------------------
        await report.add_guarded(
            "No sessions that would block (or be blocked by) the ACCESS EXCLUSIVE lock",
            lambda: check_blocking_sessions(conn, args.max_xact_age_s),
        )

        # --- 8. migration harness commits what it applies -----------------------
        await report.add_guarded(
            "Migration harness commits what it applies (alembic/env.py)",
            check_migration_harness,
        )

        # --- 9. sizing / expected lock window ------------------------------------
        rows = 0
        try:
            sizing = await check_sizing(conn, rev)
            report.add(*sizing)
            rows = sizing[4].get("agent_messages_rows", 0)
        except Exception as exc:  # noqa: BLE001
            report.add(
                "Sizing and expected lock window",
                BLOCK,
                f"this check could not be completed: {type(exc).__name__}: {exc}",
                [],
                {},
            )

        # --- 10. index growth headroom -------------------------------------------
        await report.add_guarded(
            "Disk headroom for the indexes 0019/0021 add",
            lambda: check_index_growth(conn, rev, args.target),
        )

        # --- 11. legacy-row inventory --------------------------------------------
        await report.add_guarded(
            "Legacy-row inventory (rows that will have content = '')",
            lambda: check_legacy_inventory(conn, rev),
        )

        # --- 12. backup ----------------------------------------------------------
        await report.add_guarded(
            "Recent, non-trivial backup exists", lambda: check_backup(args, rows)
        )

        # --- 13. row-count snapshot for postflight -------------------------------
        report.extra["lock_timeout_ms"], report.extra["lock_timeout_source"] = (
            resolve_lock_timeout_ms(dict(os.environ), read_env_py())
        )

        async def _snapshot_check():
            counts = await snapshot_row_counts(conn)
            report.extra["row_counts"] = counts
            status_, detail_, rem_ = write_snapshot(args, report, counts, rev)
            return (
                "Row-count snapshot written for postflight",
                status_,
                detail_,
                rem_,
                {"tables": len(counts), "total_rows": sum(counts.values())},
            )

        await report.add_guarded("Row-count snapshot written for postflight", _snapshot_check)
    finally:
        await conn.close()
        await engine.dispose()
    return report


async def check_alembic_scripts(rev: str | None, target: str):
    """Mirror scripts/ci.sh's alembic guard, but relate it to the live DB.

    Two migrations sharing a revision id is invisible to git and to pytest, and at
    deploy time a targeted ``upgrade <rev>`` applies whichever file sorts last while
    stamping the database as fully migrated.
    """
    versions = sorted((REPO_ROOT / "alembic" / "versions").glob("*.py"))
    ids: dict[str, list[str]] = {}
    downs: dict[str, str | None] = {}
    for f in versions:
        src = f.read_text()
        m = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', src, re.M)
        d = re.search(r'^down_revision(?::[^=]*)?\s*=\s*(?:["\']([^"\']+)["\']|None)', src, re.M)
        if not m:
            continue
        ids.setdefault(m.group(1), []).append(f.name)
        downs[m.group(1)] = d.group(1) if (d and d.group(1)) else None
    dupes = {k: v for k, v in ids.items() if len(v) > 1}
    parents = {v for v in downs.values() if v}
    heads = sorted(set(ids) - parents)
    detail = f"{len(versions)} migration files, {len(ids)} revision ids, heads={heads}."
    if dupes:
        return (
            "Exactly one alembic head, no duplicate revision ids",
            BLOCK,
            detail + f" DUPLICATE revision ids: {dupes}",
            [
                "Renumber the newer migration onto the current head before deploying:",
                "  grep -h '^revision' alembic/versions/*.py | sort | uniq -d",
                "A targeted `alembic upgrade <rev>` on a duplicate-id tree stamps the "
                "database as migrated while silently skipping one of the two files.",
            ],
            {"duplicates": dupes, "heads": heads},
        )
    if len(heads) != 1:
        return (
            "Exactly one alembic head, no duplicate revision ids",
            BLOCK,
            detail + " Expected exactly one head.",
            ["  python -m alembic heads", "Renumber the newer migration onto the current head."],
            {"heads": heads},
        )
    if heads[0] != target:
        return (
            "Exactly one alembic head, no duplicate revision ids",
            WARN,
            detail + f" The single head is {heads[0]}, not the requested target {target}.",
            [f"Pass --target {heads[0]}, or migrate to {target} deliberately with "
             f"`alembic upgrade {target}`."],
            {"heads": heads},
        )
    if rev is not None and rev not in ids:
        return (
            "Exactly one alembic head, no duplicate revision ids",
            BLOCK,
            detail + f" The database is stamped {rev!r}, which no migration file defines.",
            ["The stamp does not exist in this tree — you are pointing at a database "
             "migrated by a different branch. Do not migrate it from here."],
            {"heads": heads, "current_revision": rev},
        )
    return (
        "Exactly one alembic head, no duplicate revision ids",
        PASS,
        detail,
        [],
        {"heads": heads},
    )


async def check_ambiguous_revision(conn, rev: str | None):
    """A DB stamped 0019 might have been migrated by one of the OTHER 0019s.

    THREE different files in this repository's history declared ``revision = "0019"``
    revising 0018. Enumerated from git, not memory — every blob under
    ``alembic/versions/`` in ``git rev-list --all`` was parsed for its declared id:

    * ``0019_agent_message_content.py`` (a7659b4) — the one this chain expects. Adds
      ``agent_messages.content`` and six other columns.
    * ``0019_add_cohorts.py`` (b00b0e6, branch cohort-agent-isolation) — creates
      ``cohorts``/``cohort_memberships``. Verified on a fixture in that exact state:
      ``alembic upgrade head`` runs 0020 and 0021 happily (0021's index only touches
      columns that exist at 0018) and then dies at 0022 with ``relation "cohorts"
      already exists`` — having never applied 0019's content columns, which the app at
      HEAD requires.
    * ``0019_add_hidden_to_proposals.py`` (4037b79, branch coPI-podcast) — adds
      ``hidden`` to ``thread_decisions`` and ``matchmaker_proposals``. Not an ancestor
      of main or of this branch, so it is the least likely, but it is on origin and
      therefore deployable.

    Naming the wrong culprit sends the operator to the wrong remediation, so probe for
    each signature separately rather than assuming the alternative is the cohort one.
    """
    title = "The 0019 stamp is the content 0019, not one of the other 0019s"
    if rev != "0019":
        return (title, PASS, f"not applicable (stamped {rev!r}).", [], {})
    has_content = await column_exists(conn, "agent_messages", "content")
    has_cohorts = await table_exists(conn, "cohorts")
    has_hidden = await column_exists(conn, "thread_decisions", "hidden")
    data = {
        "agent_messages.content": has_content,
        "cohorts": has_cohorts,
        "thread_decisions.hidden": has_hidden,
    }
    if has_content:
        return (
            title,
            PASS,
            "agent_messages.content is present, so 0019_agent_message_content was the "
            "0019 that ran.",
            [],
            data,
        )

    # Which one actually ran? Say so, and give the matching remediation.
    if has_cohorts:
        culprit = "0019_add_cohorts (the duplicate id from branch cohort-agent-isolation)"
        drops = ["  DROP TABLE IF EXISTS cohort_memberships, cohorts CASCADE;"]
        extra = [
            "Do not run `alembic upgrade head` as-is: it will apply 0020 and 0021, then "
            "abort at 0022 on the already-existing cohorts tables, and the content "
            "columns will still be missing.",
        ]
    elif has_hidden:
        culprit = "0019_add_hidden_to_proposals (the duplicate id from branch coPI-podcast)"
        drops = [
            "  -- Only if you are sure nothing reads them; leaving them is harmless:",
            "  ALTER TABLE thread_decisions DROP COLUMN IF EXISTS hidden;",
            "  ALTER TABLE matchmaker_proposals DROP COLUMN IF EXISTS hidden;",
        ]
        extra = [
            "Those two `hidden` columns are additive and orphaned — nothing in this "
            "branch references them. The chain will migrate correctly with them left in "
            "place, so prefer leaving them alone over dropping data.",
        ]
    else:
        culprit = "an unrecognised 0019"
        drops = ["  -- nothing known to drop; investigate before proceeding"]
        extra = [
            "Neither the cohorts tables nor thread_decisions.hidden is present, so this "
            "matches none of the three 0019s in this repository's history.",
            "STOP and inspect the schema by hand. A 0019 stamp with none of the known "
            "signatures means this database's history is not one this tooling has seen, "
            "and no remediation below is known to be correct for it.",
        ]

    return (
        title,
        BLOCK,
        "stamped 0019 but agent_messages.content does NOT exist: this database was "
        f"migrated by {culprit}, so 0019's content columns were never applied.",
        [
            *extra,
            "Re-stamp to 0018, undo what that other 0019 created, then migrate the "
            "whole chain:",
            "  UPDATE alembic_version SET version_num = '0018';",
            *drops,
            "  -- then: alembic upgrade 0023",
            "Take the backup FIRST if you run any of those DROPs; they are destructive.",
        ],
        data,
    )


async def check_duplicate_run_ts(conn, rev: str | None, max_groups: int):
    """THE hard blocker. 0019 adds ``uq_agent_messages_run_ts``."""
    title = "No duplicate (simulation_run_id, message_ts) in agent_messages"
    if not await table_exists(conn, "agent_messages"):
        return (title, WARN, "agent_messages does not exist; nothing to check.", [], {})
    already = await fetch_one_value(
        conn,
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_agent_messages_run_ts' "
        "AND conrelid='agent_messages'::regclass)",
    )
    groups = await fetch_all(conn, DUPLICATE_GROUPS_SQL)
    n_rows_at_risk = sum(int(g["n"]) - 1 for g in groups)
    data = {
        "constraint_already_present": bool(already),
        "duplicate_group_count": len(groups),
        "rows_over_and_above_one_per_group": n_rows_at_risk,
        "groups": [
            {"run_id": g["run_id"], "message_ts": g["message_ts"], "n": int(g["n"]), "ids": g["ids"]}
            for g in groups[:max_groups]
        ],
        "groups_truncated": max(0, len(groups) - max_groups),
    }
    if already:
        return (
            title,
            PASS,
            "uq_agent_messages_run_ts already exists, so the database has been enforcing "
            "this since 0019 and no duplicate can be present "
            f"(scan confirms {len(groups)} groups).",
            [],
            data,
        )
    if not groups:
        return (
            title,
            PASS,
            "0 duplicate groups. NULL message_ts rows are excluded on purpose: Postgres "
            "UNIQUE treats NULLs as distinct, so they cannot violate the new constraint "
            "(verified — three NULL-ts rows in one run coexist with the constraint).",
            [],
            data,
        )
    listed = min(len(groups), max_groups)
    lines = [
        f"{len(groups)} duplicate group(s), {n_rows_at_risk} row(s) over the one row per "
        "group the constraint allows. Postgres names only ONE key per failed index build, "
        "so without this list the migration must be run once per group. "
        + (
            "All groups:"
            if listed == len(groups)
            else f"First {listed} of {len(groups)} (raise --max-duplicate-groups for the rest):"
        ),
    ]
    for g in groups[:max_groups]:
        lines.append(
            f"  run {g['run_id']} ts {g['message_ts']} x{int(g['n'])}: {', '.join(g['ids'])}"
        )
    if len(groups) > max_groups:
        lines.append(f"  ... and {len(groups) - max_groups} more (raise --max-duplicate-groups)")
    return (
        title,
        BLOCK,
        "\n".join(lines),
        [
            "Inspect before you change anything:",
            DUPLICATE_GROUPS_SQL.strip(),
            DEDUPE_DELETE_SQL,
            DEDUPE_NULL_SQL,
            "Then re-run this preflight; it must report 0 groups.",
        ],
        data,
    )


async def check_name_collisions(conn, rev: str | None, target: str):
    """Objects the pending revisions CREATE must not already exist.

    None of these creations is guarded. Verified failures on real fixtures:
    a hand-made ``ix_agent_messages_run_posted`` gives
    ``DuplicateTableError: relation ... already exists``, and an orphaned
    ``pi_dm_direction_enum`` type with no pi_dm_messages table gives
    ``DuplicateObjectError: type ... already exists`` — 0020 creates the type inline in
    ``create_table`` with no ``checkfirst``.
    """
    title = "Objects the pending revisions create do not already exist"
    planned = planned_objects_between(rev or "0018", target)
    if not planned:
        return (title, PASS, f"nothing to create for {rev} -> {target}.", [], {})
    existing = await existing_object_names(conn)
    collisions: list[PlannedObject] = []
    for obj in planned:
        if obj.kind == "column":
            if obj.table in existing["table"] and await column_exists(conn, obj.table, obj.name):
                collisions.append(obj)
        elif obj.name in existing.get(obj.kind, set()):
            collisions.append(obj)
    data = {
        "planned": len(planned),
        "collisions": [
            {"revision": o.revision, "kind": o.kind, "name": o.name, "table": o.table}
            for o in collisions
        ],
    }
    if not collisions:
        return (
            title,
            PASS,
            f"{len(planned)} object(s) will be created by revisions after {rev}; none exists yet.",
            [],
            data,
        )
    lines = [f"{len(collisions)} name collision(s) — each aborts the chain at its revision:"]
    rem = ["Drop the pre-existing objects (they were not created by alembic), or stamp "
           "past the revision that creates them if they are genuinely equivalent:"]
    for o in collisions:
        lines.append(f"  {o.revision} would create {o.kind} {o.name}"
                     + (f" on {o.table}" if o.table else "") + " — already present")
        if o.kind == "index":
            rem.append(f"  DROP INDEX IF EXISTS {o.name};")
        elif o.kind == "table":
            rem.append(f"  DROP TABLE IF EXISTS {o.name} CASCADE;  -- destructive, check first")
        elif o.kind == "constraint":
            rem.append(f"  ALTER TABLE {o.table} DROP CONSTRAINT IF EXISTS {o.name};")
        elif o.kind == "type":
            rem.append(f"  DROP TYPE IF EXISTS {o.name};")
        elif o.kind == "column":
            rem.append(f"  -- {o.table}.{o.name} already exists; verify its type matches "
                       "the migration before stamping past it")
    return (title, BLOCK, "\n".join(lines), rem, data)


async def check_downgrade_blockers(conn, rev: str | None):
    """``agent_messages.agent_id IS NULL`` rows: WARN, and rollback becomes impossible.

    0019's downgrade does ``ALTER COLUMN agent_id SET NOT NULL``. Any PI/human row
    (agent_id NULL by design from 0019 on) makes that statement fail, so once such a row
    exists a downgrade past 0019 cannot run at all. At 0018 the column is still NOT NULL,
    so the count is necessarily 0 and this check is informational.
    """
    title = "Rows that would block a downgrade past 0019 (agent_messages.agent_id IS NULL)"
    if not await table_exists(conn, "agent_messages"):
        return (title, PASS, "agent_messages does not exist.", [], {})
    nullable = await fetch_one_value(
        conn,
        "SELECT NOT attnotnull FROM pg_attribute WHERE attrelid='agent_messages'::regclass "
        "AND attname='agent_id'",
    )
    if not nullable:
        return (
            title,
            PASS,
            "agent_id is still NOT NULL at this revision, so no such row can exist. "
            "Note that once 0019 has run, every PI/human message will have agent_id NULL "
            "and this becomes a one-way door.",
            [],
            {"agent_id_nullable": False, "null_agent_id_rows": 0},
        )
    n = int(await fetch_one_value(conn, "SELECT count(*) FROM agent_messages WHERE agent_id IS NULL"))
    data = {"agent_id_nullable": True, "null_agent_id_rows": n}
    if n == 0:
        return (title, PASS, "0 rows with agent_id IS NULL.", [], data)
    return (
        title,
        WARN,
        f"{n:,} row(s) have agent_id IS NULL (PI/human messages). 0019's downgrade runs "
        "ALTER COLUMN agent_id SET NOT NULL, which these rows make fail: ROLLBACK PAST "
        "0019 IS IMPOSSIBLE while they exist. This is a WARN, not a BLOCK — it does not "
        "affect the upgrade.",
        [
            "Nothing to fix before the upgrade. Know that your only rollback is a restore "
            "from the backup, not `alembic downgrade`.",
            "To see them:",
            "  SELECT id, simulation_run_id, sender_name, channel_name, message_ts\n"
            "    FROM agent_messages WHERE agent_id IS NULL ORDER BY created_at;",
        ],
        data,
    )


async def check_blocking_sessions(conn, max_xact_age_s: float = DEFAULT_MAX_TOLERABLE_XACT_AGE_S):
    """Report every session that could hold a conflicting lock."""
    title = "No sessions that would block (or be blocked by) the ACCESS EXCLUSIVE lock"
    sessions = await fetch_all(conn, BLOCKING_SESSIONS_SQL, app_name=APPLICATION_NAME)
    status, detail = blocking_sessions_status(sessions, max_xact_age_s)
    lines = [detail]
    for s in sessions:
        lines.append(
            f"  pid {s['pid']} state={s['state']} app={s['application_name']!r} "
            f"xact_age={s['xact_age_s']:.1f}s query_age={s['query_age_s']:.1f}s "
            f"holds_lock_on_agent_messages={s['holds_agent_messages_lock']}"
        )
        lines.append(f"      query: {str(s['query'])[:200]}")
    rem: list[str] = []
    if status != PASS:
        rem = [
            "Stop the writers first — the agent simulation is the main one, and it must be "
            "stopped GRACEFULLY or the in-flight turn's messages are lost:",
            # blackbird-agent-run, NOT agent-run: the unprefixed name belongs to the
            # OTHER deployment on this host (project copi-python), and stopping it
            # would halt that deployment's production simulation.
            # -t 420, not -t 30: shutdown is cooperative (request_stop() only flips a
            # flag; the durable flush needs the main loop in main.py's finally-block to
            # RETURN), and a 16000-token thread_reply final call can run ~4-5 minutes
            # uninterrupted. `docker stop` returns as soon as the container exits, so a
            # generous -t costs nothing on the common path.
            "  docker stop -t 420 blackbird-agent-run",
            "  docker compose -f docker-compose.prod.yml stop blackbird-app worker",
            "Then re-check, and only terminate what is left if you know what it is:",
            "  SELECT pid, state, now()-xact_start AS age, query FROM pg_stat_activity\n"
            "   WHERE datname = current_database() AND xact_start IS NOT NULL;",
            "  SELECT pg_terminate_backend(<pid>);",
        ]
    return (
        title,
        status,
        "\n".join(lines),
        rem,
        {"sessions": sessions, "session_count": len(sessions)},
    )


def read_env_py() -> str | None:
    p = REPO_ROOT / "alembic" / "env.py"
    try:
        return p.read_text()
    except OSError:
        return None


def check_migration_harness():
    """Does ``alembic upgrade`` actually commit what it applies?

    See ``harness_findings``. This is the only check here that can fail while every row
    of data is perfect, and it is the most dangerous failure of the lot because the
    symptom is a *successful-looking* migration.
    """
    title = "Migration harness commits what it applies (alembic/env.py)"
    src = read_env_py()
    if src is None:
        return (
            title,
            WARN,
            "could not read alembic/env.py, so the harness could not be checked.",
            ["Run preflight from a checkout of the tree you are migrating with."],
            {},
        )
    findings = harness_findings(src)
    lock_ms, lock_src = resolve_lock_timeout_ms(dict(os.environ), src)
    data = {"findings": findings, "lock_timeout_ms": lock_ms, "lock_timeout_source": lock_src}
    if not findings:
        return (
            title,
            PASS,
            f"do_run_migrations issues no SQL before context.begin_transaction(). "
            f"Effective lock_timeout: {lock_ms} ms ({lock_src}).",
            [],
            data,
        )
    return (
        title,
        BLOCK,
        "\n".join(
            [
                "`alembic upgrade` on this tree will log a full successful chain, exit 0, "
                "and COMMIT NOTHING:",
                *(f"  {f}" for f in findings),
                "Mechanism: the early statement autobegins a transaction; "
                "MigrationContext.__init__ then sets _in_external_transaction=True, "
                "begin_transaction() degrades to nullcontext(), alembic leaves the commit "
                "to the caller, and run_async_migrations()'s connect() block rolls back on "
                "exit.",
                f"Effective lock_timeout: {lock_ms} ms ({lock_src}).",
            ]
        ),
        [
            "Set the timeout on the ENGINE instead of on the connection, so no statement "
            "runs before alembic demarcates its transaction — e.g. pass it in the DSN or "
            "via connect_args, or issue it inside do_run_migrations AFTER "
            "context.begin_transaction() has been entered.",
            "Verify the fix the only way that counts — on a throwaway database, confirm "
            "the version actually moved:",
            "  DATABASE_URL=<throwaway> python -m alembic upgrade head",
            "  psql -c 'SELECT version_num FROM alembic_version'",
            "Until it is fixed you can neutralise it with ALEMBIC_LOCK_TIMEOUT_MS=0, which "
            "skips the offending statement (but then the migration waits for locks forever "
            "— see check on blocking sessions).",
            "Run postflight after every migration regardless: it is what catches this.",
        ],
        data,
    )


async def check_sizing(conn, rev: str | None = None):
    """agent_messages row count, size, and the estimated lock window.

    The estimate is a function of the 0019 index build, so it only applies when 0019 is
    still pending. Starting from 0020/0021 that cost is already paid and the remaining
    chain (0022's three empty tables, 0023's three columns on a small table) does not
    scale with agent_messages at all — quoting the row-scaled number there would tell an
    operator to book an outage they do not need.
    """
    title = "Sizing and expected lock window"
    if not await table_exists(conn, "agent_messages"):
        return (title, WARN, "agent_messages does not exist.", [], {"agent_messages_rows": 0})
    rows = int(await fetch_one_value(conn, "SELECT count(*) FROM agent_messages"))
    if rev in POST_0019_STARTS:
        heap = int(await fetch_one_value(conn, "SELECT pg_relation_size('agent_messages')"))
        return (
            title,
            PASS,
            f"agent_messages: {rows:,} rows, heap {heap / 1e6:.1f} MB — but 0019 has "
            f"already run at {rev}, so its ACCESS EXCLUSIVE index build is behind you. "
            f"What remains is 0022 (three empty tables) and 0023 (three columns on "
            f"researcher_profiles); neither scales with agent_messages. Measured at ~2s "
            f"at every size tested.",
            [],
            {
                "agent_messages_rows": rows,
                "agent_messages_heap_bytes": heap,
                "estimated_lock_window_ms_low": 0,
                "estimated_lock_window_ms_high": 2000,
                "index_build_already_done": True,
            },
        )
    heap = int(await fetch_one_value(conn, "SELECT pg_relation_size('agent_messages')"))
    total = int(await fetch_one_value(conn, "SELECT pg_total_relation_size('agent_messages')"))
    dbsize = int(await fetch_one_value(conn, "SELECT pg_database_size(current_database())"))
    lo, hi = estimate_lock_window_ms(rows)
    status, note = sizing_status(rows, hi)
    data = {
        "agent_messages_rows": rows,
        "agent_messages_heap_bytes": heap,
        "agent_messages_total_relation_bytes": total,
        "database_bytes": dbsize,
        "estimated_lock_window_ms_low": lo,
        "estimated_lock_window_ms_high": hi,
    }
    detail = (
        f"agent_messages: {rows:,} rows, heap {heap / 1e6:.1f} MB, total relation "
        f"{total / 1e6:.1f} MB; database {dbsize / 1e6:.1f} MB.\n"
        f"Estimated ACCESS EXCLUSIVE window {lo / 1000:.1f}s (idle server, warm cache) to "
        f"{hi / 1000:.1f}s (busy server). {note}\n"
        f"The whole 0019..0023 chain runs in ONE transaction, so the lock is held for the "
        f"entire chain, not just the index build. Calibrated at 10k/100k/1M rows: "
        f"112 ms / 747 ms / 7,902 ms."
    )
    rem = []
    if status != PASS:
        rem = [
            "Announce the window and stop the writers for its duration:",
            # See the note above: the unprefixed `agent-run` is org1's container.
            # -t 420: cooperative shutdown means the durable flush only runs once the
            # main loop returns, and a 16000-token thread_reply final call can run
            # ~4-5 minutes uninterrupted — a larger -t is free insurance since `docker
            # stop` returns as soon as the container actually exits.
            "  docker stop -t 420 blackbird-agent-run && "
            "docker compose -f docker-compose.prod.yml stop blackbird-app worker",
            "There is no CONCURRENTLY option available here: alembic runs the whole chain "
            "in one transaction and CREATE INDEX CONCURRENTLY cannot run inside one.",
        ]
    return (title, status, detail, rem, data)


async def check_index_growth(conn, rev: str | None, target: str):
    """The four new indexes cost about +80% of the current relation size."""
    title = "Disk headroom for the indexes 0019/0021 add"
    planned = planned_objects_between(rev or "0018", target)
    new_idx = [o for o in planned if o.kind == "index" and o.table == "agent_messages"]
    if not new_idx or not await table_exists(conn, "agent_messages"):
        return (title, PASS, "no new agent_messages indexes for this transition.", [], {})
    total = int(await fetch_one_value(conn, "SELECT pg_total_relation_size('agent_messages')"))
    need = int(total * INDEX_GROWTH_FRACTION)
    data = {
        "new_indexes": [o.name for o in new_idx],
        "current_total_relation_bytes": total,
        "estimated_additional_bytes": need,
    }
    detail = (
        f"{len(new_idx)} new index(es) on agent_messages: {', '.join(o.name for o in new_idx)}. "
        f"Measured growth at three scales was +79%, +79%, +80% of the pre-migration total "
        f"relation size, so budget about {need / 1e6:.0f} MB of new index data plus sort "
        f"space for the build."
    )
    if need > INDEX_GROWTH_WARN_BYTES:
        return (
            title,
            WARN,
            detail + " That is over 1 GiB; preflight cannot see the filesystem from inside "
            "Postgres, so confirm free space by hand.",
            ["  docker compose exec postgres df -h /var/lib/postgresql/data"],
            data,
        )
    return (title, PASS, detail, [], data)


async def check_legacy_inventory(conn, rev: str | None):
    """Rows that end up with ``content = ''``, split by whether Slack still has them."""
    title = "Legacy-row inventory (rows that will have content = '')"
    if not await table_exists(conn, "agent_messages"):
        return (title, PASS, "agent_messages does not exist.", [], {})
    has_content = await column_exists(conn, "agent_messages", "content")
    if has_content:
        where = "content = ''"
    else:
        # Pre-0019: the column does not exist yet, so EVERY row gets the server_default ''.
        where = "TRUE"
    recoverable = int(
        await fetch_one_value(
            conn,
            f"SELECT count(*) FROM agent_messages WHERE {where} AND channel_id NOT LIKE 'local:%'",
        )
    )
    unrecoverable = int(
        await fetch_one_value(
            conn,
            f"SELECT count(*) FROM agent_messages WHERE {where} AND channel_id LIKE 'local:%'",
        )
    )
    status, note = legacy_inventory_status(recoverable, unrecoverable)
    data = {
        "content_column_present": bool(has_content),
        "empty_content_slack_recoverable": recoverable,
        "empty_content_unrecoverable": unrecoverable,
    }
    detail = note + (
        ""
        if has_content
        else " (agent_messages.content does not exist yet, so every existing row will take "
        "the server_default '' — the message bodies were never in this database.)"
    )
    rem: list[str] = []
    if recoverable:
        rem.append(
            "The Slack-side rows can be recovered, with Slack tokens available, by:\n"
            "  docker compose exec app python scripts/backfill_slack_history_to_db.py\n"
            "It upserts on (simulation_run_id, message_ts) and is safe to re-run."
        )
    if unrecoverable:
        rem.append(
            f"The {unrecoverable:,} 'local:' rows were never mirrored anywhere. Their bodies "
            "do not exist; they will read as empty messages forever. Do not let anyone "
            "'fix' this by inventing content."
        )
    return (title, status, detail, rem, data)


def check_backup(args, live_rows: int):
    """A recent, data-bearing dump must exist: rollback past 0019 is destructive."""
    title = "Recent, non-trivial backup exists"
    path = find_backup(args.backup_path)
    facts = inspect_backup(path, live_rows, args.backup_verified_elsewhere)
    status, notes = evaluate_backup(facts, args.backup_max_age_hours, args.backup_min_bytes)
    detail = notes[0] if notes else ""
    rem = list(notes[1:])
    return (
        title,
        status,
        detail,
        rem,
        {
            "path": facts.path,
            "exists": facts.exists,
            "size_bytes": facts.size_bytes,
            "age_hours": facts.age_hours,
            "format": facts.fmt,
            "has_agent_messages_data": facts.has_agent_messages_data,
            "override_reason": facts.override_reason,
        },
    )


def write_snapshot(args, report: Report, counts: dict[str, int], rev: str | None):
    """Hand off to postflight. The snapshot is the only thing postflight cannot re-derive."""
    payload = {
        "kind": "preflight-snapshot",
        "generated_at": time.time(),
        "database_url": redact_url(report.extra.get("database_url", "")),
        "current_revision": rev,
        "target": args.target,
        "row_counts": counts,
    }
    detail = (
        f"{len(counts)} tables, {sum(counts.values()):,} rows total (exact counts, not "
        "reltuples)."
    )
    if not args.snapshot:
        return (
            WARN,
            detail + " No --snapshot path given, so postflight cannot compare row counts.",
            [
                "Re-run with --snapshot to enable the postflight row-count comparison:",
                "  python scripts/migrate/preflight.py --snapshot "
                "/app/logs/migration_snapshot.json",
                "(the counts are also in the --json output under row_counts)",
            ],
        )
    try:
        p = Path(args.snapshot)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        return (BLOCK, f"could not write snapshot to {args.snapshot}: {exc}", [
            "postflight cannot verify row counts without it; pick a writable path."
        ])
    return (PASS, detail + f" Written to {args.snapshot}.", [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--database-url",
        default=None,
        help="Target database. Default: $DATABASE_URL, else the app's configured URL.",
    )
    ap.add_argument("--target", default=DEFAULT_TARGET, help=f"Target revision (default {DEFAULT_TARGET})")
    ap.add_argument("--json", action="store_true", help="Also print a machine-readable JSON report")
    ap.add_argument(
        "--json-out", default=None, help="Write the JSON report to this path as well as stdout"
    )
    ap.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=60_000,
        help="Bound on each of this script's own queries (default 60000)",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="preflight",
        description="Pre-migration safety gate. Exit 0 = safe, 1 = BLOCKED, 2 = warnings only.",
    )
    add_common_arguments(ap)
    ap.add_argument("--snapshot", default=None, help="Write the row-count snapshot postflight reads")
    ap.add_argument(
        "--backup-path",
        default=None,
        help="Backup file, or a directory to take the newest dump from. "
        f"Default: newest match under {', '.join(DEFAULT_BACKUP_DIRS)}",
    )
    ap.add_argument(
        "--backup-max-age-hours",
        type=float,
        default=DEFAULT_BACKUP_MAX_AGE_HOURS,
        help=f"Reject a backup older than this (default {DEFAULT_BACKUP_MAX_AGE_HOURS:.0f})",
    )
    ap.add_argument(
        "--backup-min-bytes",
        type=int,
        default=DEFAULT_BACKUP_MIN_BYTES,
        help=f"Reject a backup smaller than this (default {DEFAULT_BACKUP_MIN_BYTES})",
    )
    ap.add_argument(
        "--backup-verified-elsewhere",
        default=None,
        metavar="REASON",
        help="Downgrade the backup check to WARN, recording REASON in the report. "
        "For managed snapshots preflight cannot see. Not a way to skip having a backup.",
    )
    ap.add_argument(
        "--max-duplicate-groups",
        type=int,
        default=200,
        help="Cap on duplicate groups listed individually (default 200)",
    )
    ap.add_argument(
        "--max-xact-age-s",
        type=float,
        default=DEFAULT_MAX_TOLERABLE_XACT_AGE_S,
        help="An `active` session with a transaction older than this BLOCKs "
        f"(default {DEFAULT_MAX_TOLERABLE_XACT_AGE_S:.0f}). Idle-in-transaction always BLOCKs.",
    )
    return ap


def emit(report: Report, args) -> int:
    print(f"=== {report.kind}: {report.extra.get('database_url')} "
          f"(target {report.extra.get('target')}) ===")
    print(report.render_text())
    payload = report.to_dict()
    if args.json:
        print("--- JSON ---")
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return report.exit_code()


def main(argv: list[str] | None = None) -> int:
    import asyncio

    args = build_parser().parse_args(argv)
    report = asyncio.run(run_preflight(args))
    return emit(report, args)


if __name__ == "__main__":
    raise SystemExit(main())
