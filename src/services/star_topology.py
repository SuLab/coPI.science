"""Star-topology maintenance — every pi_lab gets its hub-and-spoke cohort.

The running topology is one cohort per lab, named ``hub-{agent_id}``, holding
{the lab's slug, the hub's slug, grantbot}. The 62 original spokes were created
by ad-hoc SQL on 2026-08-05/08-11 and left no ``cohort_audit_events`` rows;
this module is the committed, audited, idempotent replacement. The stakes:
``SimulationEngine._validate_star_topology`` hard-fails run STARTUP for any
live pi_lab whose gate lacks a scout_hub (under the production settings
``COHORT_ISOLATION_ENABLED=true`` / ``COHORT_DEFAULT_POLICY=isolated``, an
uncohorted agent's gate is empty) — seven agents activated on 2026-08-26 were
in exactly that state, which would have crashed the next run.

Rules, each pinned by tests/integration/test_star_topology.py:

- creates only what is missing (cohort and/or members); removes NOTHING —
  a lab-to-lab contamination is reported as an anomaly for a human, because
  deleting someone's membership row on a heuristic is how topology history
  gets falsified;
- every write lands a ``cohort_audit_events`` row via the same helper the
  admin routes use;
- slugs come from ``AgentRegistry`` verbatim (``mukherjeeclavin``, not a
  re-derivation from the PI's name);
- refuses a roster without exactly one ``scout_hub`` rather than guessing;
- does not commit — the caller owns the transaction boundary.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import hub_role_names, spoke_role_names, star_role
from src.config import get_settings
from src.models import (
    COHORT_ACTION_AGENT_ADDED,
    COHORT_ACTION_CREATED,
    AgentRegistry,
    Cohort,
    CohortMembership,
)
from src.services.cohorts import record_cohort_audit_event

logger = logging.getLogger(__name__)

# ``grantbot`` is deliberately membership-only: no AgentRegistry row and no
# process in this stack, but it sits in every one of the 62 hand-made spokes,
# and the engine documents and tolerates the ghost (simulation.py
# ``_roles_by_agent``, post_types.py ``eligible_targets``). New spokes match
# the existing shape so the topology stays uniform; dropping it everywhere is
# an owner decision, not something this maintainer does by halves.
EXTRA_SPOKE_MEMBERS: tuple[str, ...] = ("grantbot",)

_NAME_MAX = 48  # Cohort.name is String(48)

# A deleted PI's agent is suspended and its slug retired; minting a spoke for
# it would be noise. pending/inactive labs DO get one — the spoke being ready
# before activation is the whole point.
_SKIPPED_STATUSES = frozenset({"suspended"})


def star_centre_candidates(agents: Collection[AgentRegistry]) -> list[AgentRegistry]:
    """The hub-role rows that can be the star's centre: the active ones when any is
    active (D14 allows at most one), else every non-suspended one. A parked second
    hub row, which D14 permits, must not make the centre ambiguous, and a suspended
    hub is retired and never the centre."""
    hubs = [a for a in agents if star_role(a.role) == "hub" and a.status not in _SKIPPED_STATUSES]
    return [a for a in hubs if a.status == "active"] or hubs


@dataclass
class StarSpokeReport:
    """What ensure_star_spokes did (apply=True) or would do (apply=False)."""

    applied: bool
    created_cohorts: list[str] = field(default_factory=list)
    added_members: list[tuple[str, str]] = field(default_factory=list)
    complete: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


async def ensure_star_spokes(
    db: AsyncSession,
    *,
    apply: bool,
    actor: Any | None = None,
    only: Collection[str] | None = None,
) -> StarSpokeReport:
    """Ensure every non-suspended pi_lab has its ``hub-{slug}`` spoke cohort.

    With ``apply=False`` nothing is written; the report is the plan.
    With ``apply=True`` rows are added and audited but NOT committed.
    ``only`` restricts the sweep to the named lab slugs (the per-agent admin
    button); a named slug that is not an eligible pi_lab is an anomaly, not a
    spoke. The single-hub invariant is always checked against the WHOLE
    roster — a scoped call must not wire a lab to an ambiguous centre.
    """
    agents = (await db.execute(select(AgentRegistry))).scalars().all()

    spokes = spoke_role_names()
    hubs = star_centre_candidates(agents)
    if len(hubs) != 1:
        raise ValueError(
            f"expected exactly one scout_hub agent, found {len(hubs)} "
            f"({sorted(a.agent_id for a in hubs)}) — refusing to guess the "
            "star's centre"
        )
    hub = hubs[0]

    eligible = {
        a.agent_id
        for a in agents
        if a.role in spokes and a.status not in _SKIPPED_STATUSES
    }
    all_lab_slugs = {a.agent_id for a in agents if a.role in spokes}

    report = StarSpokeReport(applied=apply)
    if only is not None:
        for slug in sorted(set(only) - eligible):
            report.anomalies.append(
                f"{slug}: not a non-suspended pi_lab agent — no spoke to ensure"
            )
        eligible &= set(only)
    lab_slugs = sorted(eligible)

    cohorts_by_name = {
        c.name: c for c in (await db.execute(select(Cohort))).scalars().all()
    }
    membership_rows = (
        await db.execute(
            select(CohortMembership.cohort_id, CohortMembership.agent_id)
        )
    ).all()
    members_by_cohort: dict[Any, set[str]] = {}
    for cohort_id, agent_id in membership_rows:
        members_by_cohort.setdefault(cohort_id, set()).add(agent_id)

    for lab in lab_slugs:
        name = f"hub-{lab}"
        if len(name) > _NAME_MAX:
            report.anomalies.append(
                f"{lab}: cohort name {name!r} is {len(name)} chars "
                f"(Cohort.name holds {_NAME_MAX}); skipped — rename the agent "
                "slug or create the cohort by hand"
            )
            continue

        want = {lab, hub.agent_id, *EXTRA_SPOKE_MEMBERS}
        cohort = cohorts_by_name.get(name)
        have: set[str] = (
            members_by_cohort.get(cohort.id, set()) if cohort is not None else set()
        )

        foreign_labs = sorted((have - want) & all_lab_slugs)
        if foreign_labs:
            report.anomalies.append(
                f"{name}: contains other pi_lab member(s) {foreign_labs} — a "
                "lab-to-lab link the startup validator will refuse; left in "
                "place for a human to resolve"
            )

        if cohort is None:
            report.created_cohorts.append(name)
            if apply:
                cohort = Cohort(
                    name=name,
                    description=f"Star spoke: {hub.bot_name} <-> {lab}",
                    created_by=getattr(actor, "id", None),
                )
                db.add(cohort)
                await db.flush()
                await record_cohort_audit_event(
                    db,
                    action=COHORT_ACTION_CREATED,
                    cohort_id=cohort.id,
                    cohort_name=name,
                    actor=actor,
                )

        missing = sorted(want - have)
        if missing:
            for member in missing:
                report.added_members.append((name, member))
                if apply:
                    db.add(
                        CohortMembership(
                            cohort_id=cohort.id,
                            agent_id=member,
                            added_by=getattr(actor, "id", None),
                        )
                    )
                    await record_cohort_audit_event(
                        db,
                        action=COHORT_ACTION_AGENT_ADDED,
                        cohort_id=cohort.id,
                        cohort_name=name,
                        agent_id=member,
                        actor=actor,
                    )
        elif name not in report.created_cohorts:
            report.complete.append(name)

    if apply:
        await db.flush()
    logger.info(
        "ensure_star_spokes(apply=%s): %d cohort(s) created, %d membership(s) "
        "added, %d complete, %d anomaly(ies)",
        apply,
        len(report.created_cohorts),
        len(report.added_members),
        len(report.complete),
        len(report.anomalies),
    )
    return report


async def ensure_lab_spoke(
    db: AsyncSession, *, agent_id: str, role: str, actor: Any | None,
) -> list[str]:
    """Wire one lab that is going live into its ``hub-{agent_id}`` spoke; the reasons
    the activation must be refused, [] when it may proceed.

    Every path that makes an agent ``active`` calls this after applying its own
    writes and before committing — the manager's Activate and Unmute, the admin
    approve form and the admin role change — so a lab never goes live without the
    cohort ``_validate_star_topology`` requires at run start (a manager-activated
    lab used to go live uncohorted: isolated mid-run, and the next start failed).
    A role that is not a spoke is a no-op. ``agent_id`` and ``role`` are passed as
    values because the unmute path has expired its row by then.

    A problem (an ambiguous centre — two active hubs, or no active hub and two
    parked ones — a lab-to-lab membership, an over-long cohort name, or a
    concurrent write of the same spoke) refuses the activation only while cohort
    isolation is enabled, the only configuration in which an unwired lab is
    unreachable or fails run start; otherwise it is logged. A roster with no
    non-suspended scout_hub is logged and allowed: no lab can reach a hub then and
    no run can start under isolation whatever this returns, so refusing one lab
    protects nothing; the admin's ensure-star-spokes wires every lab once the hub
    exists. Writes audited rows inside a savepoint; does not commit.
    """
    if star_role(role) != "spoke":
        return []
    # Also the autoflush of the caller's pending edits (an admin's slug rename), so
    # their IntegrityError surfaces here, outside the savepoint, to the caller.
    hubs = await db.scalar(
        select(func.count(AgentRegistry.id)).where(
            AgentRegistry.role.in_(hub_role_names()),
            AgentRegistry.status.notin_(tuple(_SKIPPED_STATUSES)),
        )
    )
    if not hubs:
        logger.warning("No scout_hub on the roster: %s goes live without a star spoke", agent_id)
        return []
    try:
        async with db.begin_nested():
            report = await ensure_star_spokes(db, apply=True, actor=actor, only={agent_id})
        problems = list(report.anomalies)
    except ValueError as exc:
        problems = [str(exc)]
    except IntegrityError:
        # Cohort.name and the membership pair are unique: someone wired this spoke
        # at the same moment. The savepoint is gone; the caller refuses or retries.
        problems = [f"hub-{agent_id} was written concurrently; try again"]
    if problems and not get_settings().cohort_isolation_enabled:
        logger.warning(
            "Star spoke for %s not ensured (cohort isolation is off): %s",
            agent_id, "; ".join(problems),
        )
        return []
    return problems
