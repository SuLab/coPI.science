"""What a PI may see in their agent's conversations feed.

The simulation engine gates what each agent may *act on* (``_entry_allowed`` in
``src/agent/message_log.py``); this module gates what that agent's PI may *read*
on the web page. They are the same rule, and they must never disagree — the same
constraint ``src/services/cohorts.py`` was written under, and for the same reason.

``_entry_allowed`` filters ``LogEntry`` objects already in memory. The page cannot
do that: the filter has to run in SQL, before ``LIMIT``, or ``#general`` traffic
from every other cohort consumes the window and the page comes back near-empty.
So the rule is expressed twice — once as a predicate, once as a WHERE fragment —
and ``tests/integration/test_conversation_feed.py`` asserts the two agree on
every row of the engine's own decision table.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, false, or_, true

from src.models import AgentMessage
from src.visibility import VISIBILITY_COLLAB_PRIVATE


def gate_clause(gate: set[str] | None) -> ColumnElement[bool]:
    """The cohort gate as a SQL predicate over ``AgentMessage``.

    Mirrors ``_entry_allowed`` clause for clause, in the same order, so the two
    can be diffed by eye:

    - ``gate is None`` — no filtering for this agent (isolation off, or policy
      "open" and the agent is uncohorted);
    - the author is a **human** — keyed on ``is_bot``, *not* on a NULL
      ``agent_id``. ``agent_messages.agent_id`` is nullable, so a bot-authored row
      with a NULL ``agent_id`` would otherwise pass through the human bypass;
    - the row is in a ``collab_private`` channel — a PI explicitly paired those
      agents, and an admin-level grouping must not veto an explicit human pairing;
    - a bot row with a NULL ``agent_id`` cannot be attributed to a cohort, so it
      fails closed;
    - otherwise the author must share a cohort with the viewing agent.

    ``gate`` is an EMPTY set for an uncohorted agent under
    ``cohort_default_policy="isolated"``. That is the one input where the
    membership branch must be dropped entirely rather than rendered as an empty
    ``IN`` — hence the ``if gate else false()``.
    """
    if gate is None:
        return true()
    return or_(
        AgentMessage.is_bot.is_(False),
        AgentMessage.visibility == VISIBILITY_COLLAB_PRIVATE,
        and_(
            AgentMessage.agent_id.is_not(None),
            AgentMessage.agent_id.in_(gate),
        ) if gate else false(),
    )
