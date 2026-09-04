"""Atomic UPDATE statements for AgentRegistry.delegate_slack_ids (issue #22 C1).

Both routes that mutate this ARRAY column (agent_page.py's connect-slack / delegate-remove,
invite.py's accept-invitation) used to read the column into a Python list, mutate it, and
reassign the whole column — a lost-update race across two awaited Slack lookups. These build
the SQL-side equivalent (`array_append`/`array_remove`) so two concurrent accepts can't drop
each other's id.

Caller note: these are ORM-enabled UPDATE statements, so executing one via
``await session.execute(stmt)`` triggers SQLAlchemy's auto-synchronize and *expires*
``delegate_slack_ids`` on any in-session instance matching the WHERE clause — do not pass
``synchronize_session=False`` (that would leave the in-session instance's attribute stale
instead). Do not read ``agent.delegate_slack_ids`` off that instance afterwards either — the
attribute is expired, and touching it re-triggers a lazy load that needs its own await
context. Re-``select`` (with ``execution_options(populate_existing=True)`` if the instance
might still be cached elsewhere) to see the post-update value instead.
"""

from uuid import UUID

from sqlalchemy import Update, func, or_, update
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.sql.sqltypes import String

from src.models import AgentRegistry


def append_delegate_slack_id_stmt(agent_registry_id: UUID, slack_user_id: str) -> Update:
    """UPDATE ... SET delegate_slack_ids = array_append(...) WHERE id = :id AND the id isn't
    already present (idempotent against a retried Slack lookup)."""
    return (
        update(AgentRegistry)
        .where(
            AgentRegistry.id == agent_registry_id,
            or_(
                AgentRegistry.delegate_slack_ids.is_(None),
                ~AgentRegistry.delegate_slack_ids.any(slack_user_id),
            ),
        )
        .values(
            delegate_slack_ids=func.array_append(
                func.coalesce(AgentRegistry.delegate_slack_ids, array([], type_=String)),
                slack_user_id,
            )
        )
    )


def remove_delegate_slack_id_stmt(agent_registry_id: UUID, slack_user_id: str) -> Update:
    """UPDATE ... SET delegate_slack_ids = array_remove(...) WHERE id = :id."""
    return (
        update(AgentRegistry)
        .where(AgentRegistry.id == agent_registry_id)
        .values(delegate_slack_ids=func.array_remove(AgentRegistry.delegate_slack_ids, slack_user_id))
    )
