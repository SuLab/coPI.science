"""llm_call_logs gain thread_phase and message_ordinal

Two additive nullable columns on llm_call_logs:

  thread_phase    : String(20), the phase-4 interview stage (explore|decide|
                    conclude) of a thread_reply/consult turn
  message_ordinal : Integer, the reply's ordinal in its thread

Both are stamped by the producer from the same phase4_guidance() call that
built the prompt. Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA
IS SAFE. NULL on every pre-0045 row and on new_post/memory turns — never
backfilled: those turns were never asked to name a phase or ordinal, and a
guess would be indistinguishable from a measurement.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_call_logs",
        sa.Column("thread_phase", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "llm_call_logs",
        sa.Column("message_ordinal", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_call_logs", "message_ordinal")
    op.drop_column("llm_call_logs", "thread_phase")
