"""Add the ``abandoned`` thread outcome, and refusal provenance columns

Plan: docs/plans/2026-10-09-sim-drain-hang-rca-and-remediation.md Phase 1.

Run 652a6102 posted 20 pitches and produced 19 verdicts, then could not end: the
empty-reply abandon branch cleared ``has_pending_reply`` and wrote its
``assessment_drops`` row but never closed the thread, so the interview stayed
``status="active"`` in both participants' ``active_threads``,
``_open_interview_count()`` never reached 0 and the ``--max-proposals`` drain
gate never fired. The three terminal abandon branches now call ``_close_thread``,
which needs an outcome that is neither ``proposal`` nor ``no_proposal`` nor
``timeout``.

``timeout`` is deliberately NOT reused: ``outcome='timeout'`` with
``closed_by_role IS NULL`` is the unique signature of the ``max_thread_messages``
close, and ``ThreadDecision.closed_by_role`` exists precisely to end that
ambiguity. Reuse would also make ``src/services/directory.py`` and
``Funnel.terminal`` report an abandoned interview as a completed one.

Also additive, both nullable, both written only by the engine:

- ``opportunity_assessments.served_by_model`` (varchar(100), NULL): which model
  actually produced the verdict. NULL means "never recorded" — every row written
  before this migration, and any row whose call predates the refusal-fallback
  wiring. Needed because the server-side refusal fallback can answer a declined
  request on a DIFFERENT model inside the same call, and a verdict's provenance
  must not be guessable only from the run's configured model.
- ``assessment_drops.refusal_category`` (varchar(40), NULL): the Messages API's
  own ``stop_details.category`` for a drop caused by a refusal. NULL means
  "never asked" — the engine's model path did not read ``stop_details`` at all
  until this change, which is why run 652a6102's one lost verdict has no
  recorded reason.

OLD CODE ON THE NEW SCHEMA is safe: nothing old writes or compares the new enum
value, every reader renders ``outcome`` as text, and the two new columns are
nullable and unread. NEW CODE ON THE OLD SCHEMA is not, in two ways: a close
with ``outcome='abandoned'`` raises ``InvalidTextRepresentation``, which
``Threads._record_decision`` swallows and re-queues forever, ending the run with
``LOST n thread decision(s) at shutdown``; and ``OpportunityAssessment`` /
``AssessmentDrop`` map the new columns, so every verdict write and every drop
write raises ``UndefinedColumn`` on the engine's best-effort paths — i.e.
silently. Migrate BEFORE the new agent image runs, and rebuild the agent image
with it (the engine imports ``src.models``).
"""
import sqlalchemy as sa

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PG15 allows ADD VALUE inside the chain's one transaction as long as nothing in
    # that transaction uses the value; nothing here does. IF NOT EXISTS keeps ci.sh's
    # up->down->up round trip idempotent, because downgrade() cannot drop an enum
    # value — the same pattern and the same reason as 0058's `company_discovery`.
    op.execute("ALTER TYPE thread_outcome_enum ADD VALUE IF NOT EXISTS 'abandoned'")

    op.add_column(
        "opportunity_assessments",
        sa.Column("served_by_model", sa.String(100), nullable=True),
    )
    op.add_column(
        "assessment_drops",
        sa.Column("refusal_category", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assessment_drops", "refusal_category")
    op.drop_column("opportunity_assessments", "served_by_model")
    # Intentionally a no-op. PostgreSQL cannot remove a value from an enum type
    # without rewriting it, and any `thread_decisions` row already written with
    # 'abandoned' would have nothing to fall back to. The upgrade is idempotent,
    # so an up->down->up round trip is clean.
    pass
