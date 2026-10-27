"""Two enum values for the independent interview and project controls

The interviews and the projects of a run start, wind down and stop on their own
(src/agent/sessions.py). thread_outcome_enum gains 'stopped', the outcome of a thread an
operator's Stop ended, and sim_command_enum gains 'system', the command the admin panel
writes to start, wind down or stop one system of the live run.

OLD CODE ON THE NEW SCHEMA is safe: only new code writes either value. The exception is a
ROLLBACK after either value was written: a pre-0074 ORM cannot map a 'stopped' decision or a
'system' command. The old engine's Rebuild swallows the error into an empty closed set, so a
resumed old engine would reopen the stopped threads, and the old panel's command list 500s.

NEW CODE ON THE OLD SCHEMA fails: a 'stopped' decision raises InvalidTextRepresentation,
which Threads._record_decision re-queues, and the panel's per-system routes 500. Migrate
before the new images serve, and rebuild the agent image with it (the engine imports
src.models).

No row is written and nothing is back-filled. Downgrade leaves both enum values.

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-27
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PG15 allows ADD VALUE inside the chain's one transaction as long as nothing in
    # that transaction uses the value; nothing here does. IF NOT EXISTS keeps ci.sh's
    # up->down->up round trip idempotent, because downgrade() cannot drop an enum
    # value — the same pattern and the same reason as 0063's `abandoned`.
    op.execute("ALTER TYPE thread_outcome_enum ADD VALUE IF NOT EXISTS 'stopped'")
    op.execute("ALTER TYPE sim_command_enum ADD VALUE IF NOT EXISTS 'system'")


def downgrade() -> None:
    # Intentionally a no-op. PostgreSQL cannot remove a value from an enum type without
    # rewriting it, and any thread_decisions or simulation_commands row already written
    # with 'stopped' or 'system' would have nothing to fall back to. The upgrade is
    # idempotent, so an up->down->up round trip is clean.
    pass
