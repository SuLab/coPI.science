"""specialist_consults: read_state, established, and the first rubric stamp.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-28 00:00:00.000000

Four additive nullable columns.

``read_state`` splits "we could not read this reply" out of ``verdict_signal``,
which until now carried both meanings: ``parse_opinion`` defaults an unreadable
reply to ``caution``, so a defaulted opinion was byte-indistinguishable from a
genuinely cautious one and only a WARNING line recorded the difference. NULL
means "written before this revision" — a third state, deliberately not
backfilled as ``parsed``, since guessing would manufacture exactly the
confidence the column exists to stop asserting.

``established`` is the specialist contract's first positive-evidence field.
Three of the nine ``clear`` opinions ever emitted filed a positive finding
inside the ``concerns`` array with a hedge appended, because ``concerns`` and
``questions_to_ask`` were the only content fields and both are negative-valence.

``rubric_version``/``rubric_content_hash`` are the stamp consults have never
had. ``opportunity_assessments`` has carried one since 0030; without it on this
table there is no way to tell which rubric — or, after the stage-bar change,
which bars — a stored consult was judged against. NULL on every pre-0038 row.

Deploy order: additive and nullable, so OLD code against the NEW schema is safe.
The reverse is not — the new code maps all four, so ``select(SpecialistConsult)``
at src/services/assessment_detail.py (read by both assessment detail pages,
admin's and manager's) and the engine's ``_record_specialist_consult`` INSERT
(src/agent/simulation.py) raise/fail against a pre-0038 database. Build,
migrate from a one-off container, then start — the same ordering as
0028/0030/0036/0037 (see CLAUDE.md).

NOT affected: the discussions panel cards at src/services/thread_panel.py
select an explicit column list rather than the whole mapped class, and that
list does not name any of these four columns, so that page keeps working
against either schema. (An earlier draft of this docstring claimed it would
raise too; verified against the actual query and corrected.)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL means "written before 0038" — a third state, deliberately never
    # backfilled as "parsed". See the module docstring.
    op.add_column(
        "specialist_consults",
        sa.Column("read_state", sa.String(length=10), nullable=True),
    )
    # The specialist contract's first positive-evidence field. NULL on every
    # row until a later task starts writing it.
    op.add_column(
        "specialist_consults",
        sa.Column("established", postgresql.JSONB(), nullable=True),
    )
    # Which rubric this consult was judged against. NULL on every pre-0038 row,
    # deliberately never backfilled — no prior consult recorded which rubric it
    # was judged against, so there is nothing to backfill from.
    op.add_column(
        "specialist_consults",
        sa.Column("rubric_version", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "specialist_consults",
        sa.Column("rubric_content_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("specialist_consults", "rubric_content_hash")
    op.drop_column("specialist_consults", "rubric_version")
    op.drop_column("specialist_consults", "established")
    op.drop_column("specialist_consults", "read_state")
