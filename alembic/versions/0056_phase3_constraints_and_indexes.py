"""Phase 3: job priority, one-active-job index, four uniques, two indexes, rubric_documents

Additive. OLD CODE AGAINST THE NEW SCHEMA IS SAFE except for one case the prechecks
exist to surface: the old web app still enqueues with a plain INSERT, which raises
IntegrityError on a second pending job of a per-user type — the old code already
refuses that case in Python (P0-06), so this only fires on a true race.
NEW CODE AGAINST THE OLD SCHEMA IS NOT SAFE: `Job.priority` is mapped, so every
`select(Job)` raises UndefinedColumn. Migrate before the new code serves, with the
worker idle (this touches `jobs`; §12).

Each unique object has a precheck that raises with the offending rows, naming the
remediation (spec §10.5): `scripts/migrate/remediate_0056.py`. NULL priority means
"enqueued before 0056 or without a stated priority" and is never backfilled.

`ix_agent_messages_agent_phase` is built inside the one transaction (C3); preflight
sizes it from agent_messages (PLANNED_OBJECTS).

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PER_USER = "('generate_profile','enrich_grants','industry_evidence')"

#: The partial-index predicate; identical to src.models.job.ONE_ACTIVE_PER_USER_TYPE_WHERE
#: (a test pins the equality), because `ON CONFLICT ... WHERE` only infers the index when
#: its predicate matches. It tests `job_type_text(type)` rather than `type` because 0039
#: and 0047 add `review_feedback_analysis`, `enrich_grants` and `industry_evidence` to
#: job_type_enum, and Postgres refuses to use an enum value in the transaction that added
#: it: a fresh chain (one transaction, 0039 through 0056) would fail on those literals.
#: `enum::text` is only STABLE, which an index predicate rejects, hence the IMMUTABLE
#: wrapper (safe: a label's text never changes).
ONE_ACTIVE_WHERE = (
    "status IN ('pending','processing') AND user_id IS NOT NULL "
    f"AND job_type_text(type) IN {_PER_USER}"
)

_CREATE_JOB_TYPE_TEXT = (
    "CREATE FUNCTION job_type_text(job_type_enum) RETURNS text "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS 'SELECT $1::text'"
)

PRECHECKS = (
    (
        "jobs",
        f"""SELECT user_id::text, type::text, array_agg(id::text ORDER BY enqueued_at, id)
              FROM jobs
             WHERE status IN ('pending','processing') AND user_id IS NOT NULL
               AND type::text IN {_PER_USER}
             GROUP BY user_id, type HAVING count(*) > 1""",
        "python scripts/migrate/remediate_0056.py --jobs [--apply]",
    ),
    (
        "slack_app_provisions",
        """SELECT agent_registry_id::text, array_agg(id::text ORDER BY created_at DESC, id)
             FROM slack_app_provisions GROUP BY agent_registry_id HAVING count(*) > 1""",
        "python scripts/migrate/remediate_0056.py --provisions [--apply]",
    ),
    (
        "publications",
        """SELECT user_id::text, pmid, count(*)
             FROM publications WHERE pmid IS NOT NULL
             GROUP BY user_id, pmid HAVING count(*) > 1""",
        "python scripts/migrate/remediate_0056.py --publications  (lists only; the dedupe "
        "runs repair_pi_corpus.py and needs the owner's go-ahead)",
    ),
    (
        "users.email case duplicates",
        """SELECT lower(email), array_agg(id::text ORDER BY created_at, id)
             FROM users WHERE email IS NOT NULL
             GROUP BY lower(email) HAVING count(*) > 1""",
        "python scripts/migrate/remediate_0056.py --emails  (lists only; resolve by hand)",
    ),
)


def _precheck() -> None:
    bind = op.get_bind()
    problems: list[str] = []
    for name, sql, remedy in PRECHECKS:
        rows = bind.execute(sa.text(sql)).fetchall()
        if rows:
            listed = "\n    ".join(str(tuple(r)) for r in rows[:50])
            more = f"\n    … and {len(rows) - 50} more" if len(rows) > 50 else ""
            problems.append(f"{name}: {len(rows)} duplicate group(s)\n    {listed}{more}\n  fix: {remedy}")
    if problems:
        raise RuntimeError("0056 prechecks failed; nothing was changed:\n" + "\n".join(problems))


def upgrade() -> None:
    _precheck()
    op.add_column("jobs", sa.Column("priority", sa.SmallInteger(), nullable=True))
    op.execute(_CREATE_JOB_TYPE_TEXT)
    op.create_index(
        "uq_jobs_one_active_per_user_type", "jobs", ["user_id", "type"], unique=True,
        postgresql_where=sa.text(ONE_ACTIVE_WHERE),
    )
    op.create_unique_constraint(
        "uq_slack_app_provisions_agent", "slack_app_provisions", ["agent_registry_id"]
    )
    op.create_unique_constraint("uq_publications_user_pmid", "publications", ["user_id", "pmid"])
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_index("ix_agent_messages_agent_phase", "agent_messages", ["agent_id", "phase"])
    op.create_index(
        "ix_chat_usage_streaming", "assessment_chat_usage", ["created_at"],
        postgresql_where=sa.text("status = 'streaming'"),
    )
    op.create_table(
        "rubric_documents",
        sa.Column("content_hash", sa.String(20), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("toml", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rubric_documents", if_exists=True)
    op.drop_index("ix_chat_usage_streaming", table_name="assessment_chat_usage", if_exists=True)
    op.drop_index("ix_agent_messages_agent_phase", table_name="agent_messages", if_exists=True)
    op.drop_index("uq_users_email_lower", table_name="users", if_exists=True)
    op.drop_constraint("uq_publications_user_pmid", "publications", type_="unique")
    op.drop_constraint("uq_slack_app_provisions_agent", "slack_app_provisions", type_="unique")
    op.drop_index("uq_jobs_one_active_per_user_type", table_name="jobs", if_exists=True)
    op.execute("DROP FUNCTION IF EXISTS job_type_text(job_type_enum)")
    op.drop_column("jobs", "priority")
