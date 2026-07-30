"""Cohort gate semantics — one implementation, two callers.

The simulation engine applies the gate; the admin UI previews it. They must never
disagree, so the decision logic lives here as pure functions over plain data rather
than inside ``SimulationEngine``. That is also what makes the semantics testable
without an engine, a database, or a running loop.

See .notes/cohort-system-v2.md §5 (gate semantics) and §12 (admin preview).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# Policy values for settings.cohort_default_policy.
POLICY_OPEN = "open"
POLICY_ISOLATED = "isolated"


def preflight_reason(
    *,
    isolation_enabled: bool,
    policy: str,
    cohort_count: int,
    has_db: bool,
    live_members: int | None = None,
) -> str | None:
    """Return why isolation must be forced OFF, or None when it may run.

    Enabling isolation must never silently silence the roster:

    1. Without a database handle memberships cannot be read, so the flag would
       appear to work while doing nothing.
    2. Under ``policy="isolated"``, if no agent on the live roster has any cohort
       membership, every agent is uncohorted and therefore isolated — roster-wide
       silence. That covers both the obvious case (zero cohorts, the state a fresh
       deployment is in) and the easy mistake of creating a cohort and never adding
       anyone to it. Counting *live members* rather than cohorts is the check that
       actually corresponds to the hazard.

    ``live_members`` is the number of roster agents with at least one membership.
    It defaults to None for callers that only have the cohort count, in which case
    the cohort count is used as the weaker proxy.

    See v2 §5.3.
    """
    if not isolation_enabled:
        return None
    if not has_db:
        return (
            "no database session available — cohort memberships cannot be read, "
            "so isolation would silently do nothing"
        )
    if policy == POLICY_ISOLATED:
        effective = cohort_count if live_members is None else live_members
        if effective == 0:
            detail = (
                "zero cohorts defined" if cohort_count == 0
                else f"{cohort_count} cohort(s) defined but no live agent is a member"
            )
            return (
                f"cohort_default_policy='isolated' with {detail} would isolate every "
                "agent (roster-wide silence). Add agents to a cohort, or use "
                "cohort_default_policy='open'"
            )
    return None


def compute_gates(
    *,
    membership_rows: Iterable[tuple[Hashable, str]],
    agent_ids: Sequence[str],
    isolation_enabled: bool,
    policy: str,
    cohort_count: int,
    has_db: bool = True,
) -> tuple[dict[str, set[str] | None], str | None]:
    """Compute each agent's ``allowed_sender_ids`` plus any preflight refusal.

    ``membership_rows`` is ``(cohort_id, agent_id)`` pairs — the whole
    ``cohort_memberships`` table. ``agent_ids`` is the *live roster*: agents absent
    from it are ignored, and memberships naming an agent that is not running have no
    effect (they simply do not appear in anyone's mate set).

    Returns ``(gates, preflight_error)`` where a gate value is:

    - ``None``  — no filtering for this agent (gate off);
    - a set     — the bot senders this agent may act on. Empty only under
      ``policy="isolated"`` for an uncohorted agent.

    Truth table (v2 §5.1 / §5.2):

    ============================  ===================  =======================
    isolation / policy            agent has cohorts?   gate
    ============================  ===================  =======================
    disabled                      —                    None
    enabled, preflight refused    —                    None
    enabled                       yes                  union of co-members
    enabled, policy "open"        no                   None
    enabled, policy "isolated"    no                   set()
    ============================  ===================  =======================
    """
    # Materialise first: the preflight needs to know how many LIVE agents actually
    # have a membership, not just how many cohorts exist.
    members_by_cohort: dict[Hashable, set[str]] = {}
    cohorts_by_agent: dict[str, set[Hashable]] = {}
    for cohort_id, agent_id in membership_rows:
        members_by_cohort.setdefault(cohort_id, set()).add(agent_id)
        cohorts_by_agent.setdefault(agent_id, set()).add(cohort_id)
    live_members = sum(1 for aid in agent_ids if cohorts_by_agent.get(aid))

    reason = preflight_reason(
        isolation_enabled=isolation_enabled,
        policy=policy,
        cohort_count=cohort_count,
        has_db=has_db,
        live_members=live_members,
    )
    if not isolation_enabled or reason is not None:
        return {aid: None for aid in agent_ids}, reason

    isolate_uncohorted = policy == POLICY_ISOLATED

    # Under policy "open", an uncohorted agent is unrestricted — and that has to hold
    # in BOTH directions. Its own gate is None, so it may act on anyone; but a cohorted
    # agent's gate is the union of its co-members, which would never contain it. The
    # result was an agent that could react and never be replied to: it could not hold a
    # conversation, which is the opposite of "unrestricted". Adding the uncohorted
    # agents to every cohorted agent's mate set implements the §5.1 row
    # ("`A` has no cohort memberships, and policy = open -> Yes") and makes the
    # relation symmetric.
    #
    # Found by a real multi-turn run: an uncohorted agent opened two threads and no
    # cohorted agent ever replied. The gate-computation tests all passed, and the
    # symmetry test skipped the case (it compared only pairs where BOTH gates were
    # sets). See v2 §5.2.
    unrestricted: set[str] = (
        set() if isolate_uncohorted
        else {aid for aid in agent_ids if not cohorts_by_agent.get(aid)}
    )

    gates: dict[str, set[str] | None] = {}
    for aid in agent_ids:
        cohort_ids = cohorts_by_agent.get(aid)
        if not cohort_ids:
            # policy "open": unrestricted. Never an empty set here — that was the
            # inverted v1 behaviour that silenced uncohorted agents (v2 §5.4).
            gates[aid] = set() if isolate_uncohorted else None
            continue
        mates: set[str] = set()
        for cid in cohort_ids:
            mates |= members_by_cohort.get(cid, set())
        gates[aid] = mates | unrestricted
    return gates, None


def summarise_gates(gates: Mapping[str, set[str] | None]) -> dict[str, Any]:
    """Counts for logging and the admin banner."""
    gated = [aid for aid, g in gates.items() if g is not None]
    isolated = sorted(aid for aid, g in gates.items() if g is not None and not g)
    return {
        "total": len(gates),
        "gated": len(gated),
        "isolated": isolated,
        "unrestricted": sorted(aid for aid, g in gates.items() if g is None),
    }


async def record_cohort_audit_event(
    db: Any,
    *,
    action: str,
    cohort_name: str,
    cohort_id: uuid.UUID | None = None,
    agent_id: str | None = None,
    actor: Any | None = None,
    simulation_run_id: uuid.UUID | None = None,
    topology: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """Append one row to ``cohort_audit_events``.

    Denormalises ``cohort_name`` and the actor's email so the trail survives the
    cohort being deleted and the user row going away. Never raises: an audit write
    failing must not take down the mutation it describes — but it is logged loudly,
    because a silently missing trail is worse than a noisy one.
    """
    try:
        from src.models import CohortAuditEvent

        db.add(CohortAuditEvent(
            cohort_id=cohort_id,
            cohort_name=cohort_name,
            agent_id=agent_id,
            action=action,
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", None),
            simulation_run_id=simulation_run_id,
            topology=topology,
        ))
        if commit:
            await db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[cohort] audit write failed (%s on %s): %s", action, cohort_name, exc)
