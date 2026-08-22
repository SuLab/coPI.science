"""Add assessment_drops.raw_verdict, llm_call_logs.wall_ms, thread_decisions.closed_by_role.

All three nullable and additive.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-22 00:00:00.000000

``assessment_drops.raw_verdict`` is the fix for the loss measured on run
8b64a0e0: ``_record_assessment_drop`` took only a ``reason`` and a human
``detail`` string, so a refused sidecar's JSON was destroyed at the moment it
was refused. Two verdicts went that way — markham (which recomputes to 3.04,
the highest score of that run, and the only ``route-to-incubation`` it
produced) and weeraratna — and both were recoverable only *incidentally*,
because ``llm_call_logs.response_text`` happens to keep the whole response.
With this column a refusal is non-destructive on its own terms, whatever the
gate policy of the day happens to be. See
docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md (C2).

``llm_call_logs.wall_ms`` is the turn's real wall time. ``latency_ms`` is the
LAST API call's latency, not the turn's — verified equal to the last call in
532 of 532 rows on that run, and equal to the sum in only the 334 that were
single-call — so the stored total understated the true 289.4 min by 25%.
``latency_ms`` is deliberately left alone rather than redefined:
``_rebuild_state_from_db`` rebuilds ``api_call_count`` as a row COUNT and the
rate limiter's ``call_times`` as one entry per row, and the token columns'
per-turn-cumulative semantics are pinned to that arrangement.

``thread_decisions.closed_by_role`` records WHICH ROLE ended the interview.
``_check_thread_outcome`` tests for the ⏸️ marker on whoever just replied, with
no role gate, and ⏸️ is an explicit instruction to BOTH roles
(``thread_guidance.py``'s ``_SCOUT_HUB[CONCLUDE]`` for the hub's decline, and
``_PI_LAB[DECIDE]``/``[CONCLUDE]`` for a lab withdrawing). On run 8b64a0e0
seven interviews were closed by the PI's own bot mid-screen, none of which
produced a verdict — and they were indistinguishable in this table from the one
genuine ``max_thread_messages`` timeout, which is what made the first count of
them wrong. Recording the role does not change behaviour; it makes the funnel
answerable. Whether a lab's ⏸️ should end the hub's screen at all is a prompt
change needing sign-off, deliberately not decided here.

Migrate-before-serve applies, the same one-way constraint as
0028/0030/0032/0034: once ``AssessmentDrop`` and ``LlmCallLog`` map these
columns, every ``select()`` over those tables names them in its column list,
and against a pre-migration database that raises UndefinedColumn. Old code
against the new schema is safe — both are nullable with no backfill.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assessment_drops",
        sa.Column("raw_verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("wall_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "thread_decisions",
        sa.Column("closed_by_role", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("thread_decisions", "closed_by_role")
    op.drop_column("llm_call_logs", "wall_ms")
    op.drop_column("assessment_drops", "raw_verdict")
