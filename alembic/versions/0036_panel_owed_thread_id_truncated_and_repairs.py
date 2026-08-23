"""Panel-owed truth, interview thread_id, truncated consults, cached-token columns, and two data repairs.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-22 00:00:00.000000

Five additive columns, one foreign-key rule corrected, and two data repairs. From
docs/audits/2026-08-22-correctness/README.md (§1.1, §1.3, §1.7, §2.10, §2.11,
§2.12) and docs/plans/2026-08-22-correctness-remediation.md (A1.1, A1.2).

``opportunity_assessments.panel_owed`` exists because the assessment page's
"Specialist panel: verified" box is currently a claim nobody checked.
``panel_state`` derives it by RE-EVALUATING ``panel_is_owed`` at read time, so a
verdict the floor never looked at renders identically to one it looked at and
cleared — and the predicate has widened twice this month, which silently
re-labels rows written under the older rule. Storing the fact at write time gives
the read path something durable to consult, and gives the third state — "we do
not know" — somewhere to live. The three states are documented on the mapped
column; the one that matters here is NULL, which every pre-0036 row gets and
which is deliberately NOT backfilled: guessing would manufacture exactly the
verification this column exists to stop asserting.

``opportunity_assessments.thread_id`` names the interview a verdict came from.
One interview yields exactly one assessment, but nothing in the table records
which interview, so the invariant is unenforceable and unrehydratable: after a
restart ``_assessed_threads`` is empty and the engine cannot tell a fresh verdict
from a re-capture. Indexed because that rehydration is a per-run lookup. NOT
made unique with ``simulation_run_id``: it is NULL on all 63 historical rows, so
a unique index would have to be partial, and that is a decision for the task that
starts writing the column, not for this one.

``specialist_consults.truncated`` distinguishes a consult that finished from one
whose reply was cut off mid-sentence. ``tools.py`` already refuses to credit a
``refusal``-truncated consult to the specialist floor in-process, but the DB row
keeps no trace, so ``_seed_consults_from_db`` rehydrates it after a restart as a
complete consult and the floor is satisfied by an opinion nobody finished
reading. Three such consults exist on run 8b64a0e0. NULL means "written before
0036, so unknown" and must be read as "not truncated" so those three keep
crediting the floor exactly as they do today — the alternative retroactively
invalidates history on no evidence.

``llm_call_logs.cache_read_input_tokens`` / ``.cache_creation_input_tokens`` are
the input tokens Anthropic bills separately from ``usage.input_tokens``, which
EXCLUDES anything served from or written to the prompt cache. Nothing read them,
so 109 of 141 live rows record fewer input tokens than the system prompt alone
can account for, and one records **2** for a 30 KB prompt. They are new columns
rather than a fix to ``input_tokens`` for the reason ``agent_activity.py`` already
gives about ``latency_ms`` and 0035's ``wall_ms``: summing into an existing column
makes the numbers already in it mean two different things depending on when they
were written. Summing is the reader's job.

``private_channel_members_user_id_fkey`` is recreated ON DELETE CASCADE. It was
``ON DELETE SET NULL`` under
``CHECK ((agent_id IS NULL) <> (user_id IS NULL))``, and on a PI membership row
``agent_id`` is already NULL — so the cascade's own UPDATE drove both owner
columns to NULL, violated the CHECK, and made ANY user delete for a
private-channel member raise (reproduced on a throwaway DB; both
``POST /profile/delete-account`` and the admin delete 500). SET NULL was never
coherent for this column: the row's entire content is the member it names, so a
row with no owner is not a degraded record, it is an unrepresentable one.
``added_by_user_id`` stays SET NULL — nulling the adder violates nothing and the
row is still meaningful without them. The table has 0 production rows today,
which is why this swap is free now and would only get dearer.

Data repair (a) recovers de-risking milestones that
``scripts/backfill_dropped_verdicts.py`` dropped: it read ``derisking_milestones``
from the sidecar, but the contract key is ``suggested_derisking_milestones``
(``phase4-thread-reply.md``), which the engine itself reads correctly. The two
backfilled rows — identifiable as the only rows with a NULL ``slack_ts`` — are
exactly the two whose ``derisking_milestones`` is a JSON ``null`` while their
``raw_verdict`` still holds 8 and 9 milestones. 17 milestones, recoverable in
place.

Data repair (b) is 0031's normalization applied to the other ELEVEN nullable JSON
columns. 0031 collapsed the two encodings of "absent" on
``opportunity_assessments.missing_domains``; nothing stopped the next column from
reintroducing them, and 0035's ``assessment_drops.raw_verdict`` duly did — it
holds 15 SQL NULLs and 2 JSONB ``null``s for one logical state, so "which
refusals kept their verdict?" returns the 2 rows that kept nothing. The
Python-side half is ``JSONB(none_as_null=True)`` on all eleven mapped columns,
plus ``tests/unit/test_json_none_as_null.py``, which walks ``Base.metadata`` and
is the drift alarm whose absence allowed the recurrence.

**Repair order is load-bearing.** ``derisking_milestones`` is one of the eleven
columns (b) normalizes, and (a)'s predicate is
``jsonb_typeof(derisking_milestones) = 'null'``. Run (b) first and (a) matches
ZERO rows while reporting success — all 17 milestones lost, silently, on a
migration that exits 0. (a) FIRST, always. Both statements are idempotent and
safe to re-run, which is what makes the round trip in ``scripts/ci.sh`` — upgrade
-> downgrade -> upgrade — survive the second upgrade.

Migrate-before-serve applies, the same one-way constraint as
0028/0030/0032/0034/0035: once the models map these five columns, every
``select()`` over those three tables names them in its column list, and against a
pre-0036 database that raises UndefinedColumn. Old code against the new schema is
safe — all five are nullable with no backfill. Build, migrate from a one-off
container, then start.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Every nullable JSON/JSONB column that lacked ``none_as_null=True`` before this
#: revision, paired with the ``*_typeof`` function its PHYSICAL type accepts.
#: Postgres has no implicit cast between ``json`` and ``jsonb``, so
#: ``jsonb_typeof(a_json_column)`` is a hard "function does not exist" error —
#: hence the per-column function rather than one loop over one name.
#: ``opportunity_assessments.missing_domains`` (fixed by 0031) and
#: ``llm_call_logs.call_stats`` (born correct in 0032) are deliberately absent:
#: they already store SQL NULL, so including them would only be a no-op.
#: This list is derived from a walk over ``Base.metadata``, not from prose — two
#: earlier audits enumerated it by hand and both missed the same three
#: (``researcher_profiles.pending_profile``, ``.user_submitted_texts`` and
#: ``cohort_audit_events.topology``, the three that are ``json`` rather than
#: ``jsonb`` and therefore do not turn up in a grep for JSONB).
_JSON_NULL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("opportunity_assessments", "gating", "jsonb_typeof"),
    ("opportunity_assessments", "scores", "jsonb_typeof"),
    ("opportunity_assessments", "red_flags", "jsonb_typeof"),
    ("opportunity_assessments", "derisking_milestones", "jsonb_typeof"),
    ("opportunity_assessments", "raw_verdict", "jsonb_typeof"),
    ("assessment_drops", "raw_verdict", "jsonb_typeof"),
    ("specialist_consults", "concerns", "jsonb_typeof"),
    ("specialist_consults", "questions_to_ask", "jsonb_typeof"),
    ("cohort_audit_events", "topology", "json_typeof"),
    ("researcher_profiles", "pending_profile", "json_typeof"),
    ("researcher_profiles", "user_submitted_texts", "json_typeof"),
)


def upgrade() -> None:
    # ---- 1. Additive DDL -------------------------------------------------
    op.add_column(
        "opportunity_assessments",
        sa.Column("panel_owed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("thread_id", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "ix_opportunity_assessments_thread_id",
        "opportunity_assessments",
        ["thread_id"],
    )
    op.add_column(
        "specialist_consults",
        sa.Column("truncated", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True),
    )

    # ---- 2. private_channel_members.user_id: SET NULL -> CASCADE ---------
    # The FK was created unnamed inside 0011's create_table, so Postgres named it
    # by its own convention. There is no ALTER for a referential action: the
    # constraint has to be dropped and recreated, which is cheap here (the table
    # is tiny, and empty in production) but does take a brief ACCESS EXCLUSIVE
    # lock on both sides.
    op.drop_constraint(
        "private_channel_members_user_id_fkey",
        "private_channel_members",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "private_channel_members_user_id_fkey",
        "private_channel_members",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ---- 3. Data repair (a): recover the backfilled milestones -----------
    # MUST precede repair (b) — see the module docstring. `slack_ts IS NULL`
    # identifies the rows written by scripts/backfill_dropped_verdicts.py rather
    # than by a live interview, and the JSON-`null` test confines this to rows
    # that have nothing to lose. `raw_verdict ? 'key'` is a jsonb key-existence
    # test, so a row whose raw_verdict is SQL NULL is skipped rather than erroring.
    # Idempotent: after it runs, derisking_milestones is an array, so
    # jsonb_typeof no longer returns 'null' and a re-run matches nothing.
    op.execute(
        """
        UPDATE opportunity_assessments
           SET derisking_milestones = raw_verdict->'suggested_derisking_milestones'
         WHERE slack_ts IS NULL
           AND jsonb_typeof(derisking_milestones) = 'null'
           AND raw_verdict ? 'suggested_derisking_milestones'
        """
    )

    # ---- 4. Data repair (b): JSON scalar `null` -> SQL NULL --------------
    # Exactly 0031's statement, eleven more times. `<fn>(col) = 'null'` matches
    # ONLY the JSON null scalar: on a SQL NULL input the function returns SQL
    # NULL (so the predicate is unknown, not true) and on `[]`/`{}` it returns
    # 'array'/'object'. Empty containers therefore survive, which is required:
    # `concerns = []` is "consulted and raised nothing" and `red_flags = []` is
    # "screened and found none", both of which say strictly more than SQL NULL's
    # "not recorded". 0031 makes the same point about `missing_domains = []`,
    # where collapsing the two would have folded the unverified panel state into
    # the verified one.
    #
    # Interpolated rather than parameterized because identifiers cannot be bound
    # parameters; every value comes from the module constant above and none from
    # input, so there is nothing to inject.
    for table, column, typeof_fn in _JSON_NULL_COLUMNS:
        op.execute(
            f"UPDATE {table} SET {column} = NULL "
            f"WHERE {typeof_fn}({column}) = 'null'"
        )


def downgrade() -> None:
    """Reverses the DDL. The two data repairs are deliberately not reversed.

    Restoring the FK to ON DELETE SET NULL is NOT optional cosmetics: the round
    trip in ``scripts/ci.sh`` is upgrade -> downgrade -> upgrade, and the second
    upgrade's ``drop_constraint`` needs the constraint to be there under that
    exact name. Leaving CASCADE in place would also make the downgrade a
    non-inverse, which is the whole property the round trip exists to check.

    Neither repair is undone, for 0031's reason: after normalization a repaired
    row and a row that was always SQL NULL are byte-identical, so nothing
    distinguishes them and re-encoding every SQL NULL as a JSON ``null`` would
    corrupt the majority of rows that never carried it in order to restore an
    encoding that was a bug. The milestone repair is likewise not undone —
    deleting recovered data to reach an older schema is not a downgrade anyone
    wants — and it cannot break the return trip: re-running the upgrade finds
    ``derisking_milestones`` already an array and matches nothing.
    """
    op.drop_constraint(
        "private_channel_members_user_id_fkey",
        "private_channel_members",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "private_channel_members_user_id_fkey",
        "private_channel_members",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_column("llm_call_logs", "cache_creation_input_tokens")
    op.drop_column("llm_call_logs", "cache_read_input_tokens")
    op.drop_column("specialist_consults", "truncated")
    op.drop_index(
        "ix_opportunity_assessments_thread_id",
        table_name="opportunity_assessments",
    )
    op.drop_column("opportunity_assessments", "thread_id")
    op.drop_column("opportunity_assessments", "panel_owed")
