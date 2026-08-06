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

Three functions, one pipeline: ``resolve_agent_gate`` computes *what the gate
is* for the viewing agent, by calling the engine's own ``compute_gates``
(``src/services/cohorts.py``) — the same call ``_cohort_gate_context`` in
``src/routers/admin.py`` makes for the admin preview, so the page can never
compute a different gate than the engine would. ``gate_clause`` then turns that
gate into the SQL predicate above, and ``own_or_gated`` widens it with a PI's
own-post carve-out (see its docstring). The one deliberate difference from the
admin preview: ``resolve_agent_gate``'s roster is the active agents **plus the
viewing agent**, because ``/agent/{id}/conversations`` also admits an inactive
viewer, and ``compute_gates`` only returns a gate for agents in the roster it is
handed.
"""

from __future__ import annotations

import logging

from sqlalchemy import ColumnElement, and_, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import AgentMessage, AgentRegistry, Cohort, CohortMembership
from src.services.cohorts import compute_gates
from src.visibility import VISIBILITY_COLLAB_PRIVATE

logger = logging.getLogger(__name__)


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


def own_or_gated(gate: set[str] | None, agent_id: str) -> ColumnElement[bool]:
    """``gate_clause`` widened with a PI's own-post carve-out.

    A PI must always see their OWN bot's posts, even when that bot is active
    but not yet placed in a cohort — under ``policy="isolated"`` that agent's
    gate is the empty set (see ``resolve_agent_gate``/``compute_gates``), and
    ``gate_clause(set())`` admits nothing from the membership branch, so
    without this OR the PI's own posts would vanish the moment their bot is
    activated and before an admin has assigned it a cohort. This is a
    deliberate, safe divergence from ``_entry_allowed``: the engine never
    needs this clause because an agent is never asked to decide whether to
    act on its own post. Safe because it can only ever admit THIS agent's own
    rows, never another agent's.

    One expression serves three call sites that must never drift apart: the
    conversations feed's roots query, its reply-count query, and the thread
    expand endpoint's root re-resolution and reply fetch.
    """
    return or_(gate_clause(gate), AgentMessage.agent_id == agent_id)


async def resolve_agent_gate(db: AsyncSession, agent_id: str) -> set[str] | None:
    """The viewing agent's ``allowed_sender_ids``, via the engine's own computation.

    Same call the admin preview makes (``_cohort_gate_context``), with one
    deliberate difference: the roster is the active agents **plus the viewing
    agent**. ``/agent/{id}/conversations`` admits ``status in ("active",
    "inactive")``, but ``compute_gates`` only returns keys for the roster it is
    handed. Widening the roster here is what stops an inactive viewer falling
    through ``gates.get(agent_id)`` below to its default of ``None`` — i.e.
    silently getting an UNGATED feed — rather than an ergonomic nicety to dodge
    a ``KeyError`` (``dict.get`` never raises). Adding the viewer can only
    *raise* ``live_members``, which the preflight compares against zero — so it
    cannot turn a refusal into a silent roster-wide isolation.
    """
    settings = get_settings()
    roster = {
        r[0] for r in (await db.execute(
            select(AgentRegistry.agent_id).where(AgentRegistry.status == "active")
        )).all()
    }
    roster.add(agent_id)
    rows = (await db.execute(
        select(CohortMembership.cohort_id, CohortMembership.agent_id)
    )).all()
    cohort_count = (await db.execute(
        select(func.count()).select_from(Cohort)
    )).scalar() or 0

    gates, preflight_error = compute_gates(
        membership_rows=[(r[0], r[1]) for r in rows],
        agent_ids=sorted(roster),
        isolation_enabled=settings.cohort_isolation_enabled,
        policy=settings.cohort_default_policy,
        cohort_count=cohort_count,
        has_db=True,
    )
    if preflight_error is not None and settings.cohort_isolation_enabled:
        # This is the fail-open engine semantics `compute_gates`/`preflight_reason`
        # deliberately implement (src/services/cohorts.py) — do NOT change it here.
        # But it is silent by default: an admin can reach this state with one click
        # ("all / none" on every column of /admin/cohorts/topology, then save), and
        # nothing short of this line says so. Every gate this call resolves is
        # `None` while the condition holds, which means every PI conversations feed
        # and thread-expand read is UNGATED — the isolation the operator turned on
        # is not applying to any agent, not just this one.
        logger.warning(
            "[conversation_feed] preflight forced the cohort gate OFF for agent "
            "%r (%s) — PI-facing reads via resolve_agent_gate are consequently "
            "UNGATED for every agent until this is resolved",
            agent_id, preflight_error,
        )
    return gates.get(agent_id)
