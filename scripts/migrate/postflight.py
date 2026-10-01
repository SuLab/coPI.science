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
    chain and the 0056 objects only (VERIFIED_REVISIONS), whatever the target.

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
)

EXPECTED_TABLES = (
    "pi_dm_messages", "cohorts", "cohort_memberships", "cohort_audit_events", "rubric_documents",
)

#: Revisions this script's EXPECTED_TABLES/EXPECTED_COLUMNS/EXPECTED_INDEXES below
#: actually verify. Pinned: 0019-0023 and 0056. Not pinned (documented omission):
#: 0024-0055 objects, which preflight's PLANNED_OBJECTS collision check and
#: tests/integration/test_model_migration_parity.py cover. Bump this tuple (and add the
#: corresponding EXPECTED_* entries) to extend the pins. The row-count and enum checks
#: do not read it: they are scoped by revision span instead.
VERIFIED_REVISIONS: tuple[str, ...] = ("0019", "0020", "0021", "0022", "0023", "0056")

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
}

EXPECTED_ENUMS: dict[str, tuple[str, ...]] = {
    "pi_dm_direction_enum": ("inbound", "outbound"),
    "sim_command_enum": ("start", "stop"),
    "sim_command_status_enum": ("pending", "done", "failed", "stale"),
}

#: enum type name -> the revision that creates it, from preflight.PLANNED_OBJECTS.
_TYPE_REVISION: dict[str, str] = {
    o.name: o.revision for o in _pf.PLANNED_OBJECTS if o.kind == "type"
}


def expected_enums_for(target: str) -> dict[str, tuple[str, ...]]:
    """The EXPECTED_ENUMS entries a database at ``target`` must already have.

    A type whose creating revision is after ``target`` is not expected yet: 0042's two
    types must not fail a ``--target 0041`` run. An unknown ``target``, or a type with no
    PLANNED_OBJECTS entry, keeps the entry, so the check errs towards expecting more.
    """
    order = _pf.REVISION_ORDER
    if target not in order:
        return dict(EXPECTED_ENUMS)
    hi = order.index(target)
    return {
        name: labels
        for name, labels in EXPECTED_ENUMS.items()
        if _TYPE_REVISION.get(name) not in order or order.index(_TYPE_REVISION[name]) <= hi
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
