"""Unit tests for src/services/delegate_slack_ids.py (issue #22 C1)."""

import uuid

from sqlalchemy.dialects import postgresql

from src.services.delegate_slack_ids import (
    append_delegate_slack_id_stmt,
    remove_delegate_slack_id_stmt,
)

AGENT_ID = uuid.uuid4()


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_append_uses_the_atomic_array_append_form():
    sql = _sql(append_delegate_slack_id_stmt(AGENT_ID, "U123"))
    # Exact compiled fragment (SQLAlchemy 2.0.5x, postgresql dialect, literal binds) —
    # a NULL column is coalesced to an empty VARCHAR[] before array_append runs on it.
    assert (
        "delegate_slack_ids=array_append(coalesce(agents.delegate_slack_ids, "
        "ARRAY[]::VARCHAR[]), 'U123')" in sql
    )
    assert f"agents.id = '{AGENT_ID}'" in sql


def test_append_guards_against_duplicating_an_id_already_present():
    # C1-b is a lost-update race, not a dedup bug, but the guard is what makes a
    # RETRIED lookup (e.g. after a transient Slack error) idempotent rather than
    # appending the same id twice. Exact compiled fragment, not a loose substring
    # check — pins the guard's actual shape (NULL-or-not-already-present).
    sql = _sql(append_delegate_slack_id_stmt(AGENT_ID, "U123"))
    assert (
        "agents.delegate_slack_ids IS NULL OR NOT ('U123' = ANY (agents.delegate_slack_ids))"
        in sql
    )


def test_remove_uses_the_atomic_array_remove_form():
    sql = _sql(remove_delegate_slack_id_stmt(AGENT_ID, "U123"))
    assert sql == (
        f"UPDATE agents SET delegate_slack_ids=array_remove(agents.delegate_slack_ids, 'U123') "
        f"WHERE agents.id = '{AGENT_ID}'"
    )
