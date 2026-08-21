"""Add llm_call_logs.call_stats — the per-API-call breakdown of a logged turn

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-21 00:00:00.000000

One ``llm_call_logs`` row is ONE TURN, and a turn is 1..7 real API calls: up to
``max_tool_rounds`` tool rounds, the terminating (or forced-final) call, and at
most one ``max_tokens`` retry. ``input_tokens``/``output_tokens``/``latency_ms``
are the sums over all of them, which is right for billing and useless for the
question the table gets asked during an incident: *which* call truncated, and how
many tokens did the model actually want? Measured before this migration: 78.6% of
``thread_reply`` rows were multi-call, and ``stop_reason`` — read in four places
in src/services/llm.py — was logged in none of them, so sizing the thread_reply
ceiling had to be done by inference from log text and got the event count wrong
by 2 of 9.

``call_stats`` is a JSONB ARRAY with one object per real API call, in call order::

    [{"seq": 1, "kind": "round",  "max_tokens": 16000, "input_tokens": 12043,
      "output_tokens": 4118, "thinking_tokens": 2604, "stop_reason": "tool_use",
      "latency_ms": 31204.6}, ...]

``kind`` is one of ``round`` (a tool-use round), ``final`` (the terminating call
whose reply carried no tool_use), ``forced_final`` (the no-tools call after the
tool loop ended) or ``retry`` (a max_tokens retry, always immediately after the
entry it retries). ``thinking_tokens`` is nullable per entry: the SDK reports it
via ``usage.output_tokens_details``, which is itself Optional.

JSONB rather than reusing ``messages_json``: that column is ``json`` (not
``jsonb``, so not queryable without a cast) and it is the message contract read
by src/services/assessment_detail.py — overloading it would couple two unrelated
consumers. And a new column rather than a new TABLE, or one row per API call,
because ``SimulationEngine._rebuild_state`` reconstructs ``api_call_count`` as a
row ``COUNT(*)`` and the sliding-window limiter's ``call_times`` as one entry per
row; live booking is one per turn (+1 on retry), so row-per-call would inflate
both rebuilt ledgers and over-throttle every agent after every restart.

Additive and nullable, and no code READS the column, so deploy ordering is
unconstrained in both directions beyond the usual rule that the new code maps it
(the ORM names ``call_stats`` in the SELECT list of every ``select(LlmCallLog)``,
so migrate before the new code serves — same one-way constraint as 0028/0030, for
the same reason). Old code against the new schema is safe: it never names the
column and the column has no NOT NULL to satisfy.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_call_logs",
        sa.Column("call_stats", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_call_logs", "call_stats")
