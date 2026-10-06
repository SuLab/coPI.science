"""The activation gate (audit H4; coverage-plan P3, the no-migration subset).

Auto-created pending agents (manager Add-PI flow, 2026-08-24) broke the old
structural guarantee that a pending AgentRegistry row implies a completed
profile — `/agent/request` required one, the Add-PI flow mints the row before
the generation job even runs. An admin working a bulk install-links doc can
therefore reach "Approve & Activate" on an agent whose profile job died, whose
export never happened, or whose profile is the model's priors dressed up as a
researcher (the Kavran-class fabrication). This module is the refusal, called
from every activation path: both branches of ``admin_approve_agent``,
``manager_activate_agent``, the manager unmute (``agent_mute``) and an active agent's
role change. Two kinds of check: ``activation_blockers`` (profile quality; the admin's
logged override waives them) and ``persona_blockers`` (the persona file the engine will
read exists and has a Research Summary, and the owner may use the PI surfaces; never
waived, spec 2026-10-05 §6.4, D31).

``pi_lab``-scoped: the hub and specialist roles have no PI profile by design.
The override is an explicit form field; ``activate_agent`` logs it with the actor.

``ensure_activation_allowed`` adds the roster rule of D14 (spec §6.8 C-05): no path may
leave two ``active`` hub-role agents, and a role change on an active agent is checked
against the NEW role. Inactive agents may hold a hub role. The hub check runs under
a transaction-scoped advisory lock (``HUB_ROSTER_LOCK_KEY``) taken before the count, so
two concurrent activations serialize and the second sees the first's committed row.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import hub_role_names, requires_linked_user, star_role
from src.models import AgentRegistry, Job, ResearcherProfile, User
from src.models.user import PI_SURFACE_ROLES
from src.services.advisory_locks import HUB_ROSTER_LOCK_KEY
from src.services.persona_lifecycle import persona_has_research_summary
from src.services.profile_publish import persona_file_text

logger = logging.getLogger(__name__)


async def activation_blockers(
    db: AsyncSession, agent: AgentRegistry, *, role: str | None = None
) -> list[str]:
    """Reasons this agent must not be flipped to ``active`` as ``role`` (default: its
    current role); [] when clear."""
    if not requires_linked_user(role if role is not None else agent.role):
        return []

    if agent.user_id is None:
        return [
            "not linked to a user account — there is no profile to stand "
            "behind this lab"
        ]

    blockers: list[str] = []
    profile = (
        await db.execute(
            select(ResearcherProfile).where(
                ResearcherProfile.user_id == agent.user_id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        blockers.append(
            "no ResearcherProfile exists (profile generation has not "
            "completed for this PI)"
        )
    elif profile.evidence_state != "grounded":
        blockers.append(
            f"profile evidence_state is {profile.evidence_state!r} — the "
            "stored profile is not grounded in any publication abstract"
        )
    if profile is not None and not (profile.research_summary or "").strip():
        # A summary blanked by an edit (spec 2026-10-05 §6.4, P2) leaves the profile
        # "grounded" while the persona has nothing to say.
        blockers.append("the profile has no Research Summary")

    latest_job = (
        await db.execute(
            select(Job)
            .where(Job.user_id == agent.user_id, Job.type == "generate_profile")
            .order_by(Job.enqueued_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_job is not None and latest_job.status == "dead":
        blockers.append(
            f"the newest generate_profile job is DEAD after "
            f"{latest_job.attempts} attempts: "
            f"{(latest_job.last_error or 'no error recorded')[:120]}"
        )
    return blockers


#: The persona checks no override waives (spec 2026-10-05 §6.4, D31).
PERSONA_FILE_MISSING = "persona file profiles/public/{agent_id}.md is missing"
PERSONA_FILE_NO_SUMMARY = "persona file profiles/public/{agent_id}.md has no Research Summary"
OWNER_NOT_PI = "the owner's account ({role}) may not use the PI surfaces"


async def persona_blockers(
    db: AsyncSession, agent: AgentRegistry, *, role: str | None = None,
) -> list[str]:
    """What no override waives before ``agent`` may be ``active`` as ``role`` (default its
    own): the owner may use the PI surfaces (``PI_SURFACE_ROLES``), and the persona file the
    engine reads, ``profiles/public/<agent_id>.md``, exists and has a Research Summary
    (``persona_has_research_summary``). [] for a role that needs no linked user (the hub and
    the specialists) and for an agent with no user (``activation_blockers`` refuses that).
    Reads the file; writes nothing."""
    if not requires_linked_user(role if role is not None else agent.role) or agent.user_id is None:
        return []
    blockers: list[str] = []
    owner_role = await db.scalar(select(User.user_role).where(User.id == agent.user_id))
    if owner_role is not None and owner_role not in PI_SURFACE_ROLES:
        blockers.append(OWNER_NOT_PI.format(role=owner_role))
    text = persona_file_text(agent.agent_id)
    if text is None:
        blockers.append(PERSONA_FILE_MISSING.format(agent_id=agent.agent_id))
    elif not persona_has_research_summary(text):
        blockers.append(PERSONA_FILE_NO_SUMMARY.format(agent_id=agent.agent_id))
    return blockers


#: The hub-limit refusal; ``agent_id`` is the slug of the hub already active.
HUB_ALREADY_ACTIVE = "another hub-role agent ({agent_id}) is already active; deactivate it first"


async def ensure_activation_allowed(
    db: AsyncSession, agent: AgentRegistry, *, new_role: str, new_status: str,
    override: bool = False,
) -> list[str]:
    """Reasons ``agent`` must not end up ``new_status`` with ``new_role``; [] when allowed.

    Only ``new_status == "active"`` is gated. The profile blockers of
    ``activation_blockers`` are checked for ``new_role``; ``override`` waives them
    (``activate_agent`` logs what it waived), never ``persona_blockers`` and never the hub
    limit. A hub ``new_role`` takes
    ``HUB_ROSTER_LOCK_KEY`` for the rest of the caller's transaction BEFORE counting other
    active hubs. Writes nothing; the caller applies the change and commits.
    """
    if new_status != "active":
        return []
    blockers = [] if override else await activation_blockers(db, agent, role=new_role)
    blockers += await persona_blockers(db, agent, role=new_role)
    if star_role(new_role) == "hub":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": HUB_ROSTER_LOCK_KEY}
        )
        other = await db.scalar(
            select(AgentRegistry.agent_id)
            .where(
                AgentRegistry.status == "active",
                AgentRegistry.role.in_(hub_role_names()),
                AgentRegistry.id != agent.id,
            )
            .limit(1)
        )
        if other is not None:
            blockers.append(HUB_ALREADY_ACTIVE.format(agent_id=other))
    if blockers:
        logger.warning(
            "Refused activation of agent %s (%s) as %s: %s",
            agent.agent_id, agent.id, new_role, "; ".join(blockers),
        )
    return blockers


async def activate_agent(
    db: AsyncSession, agent: AgentRegistry, *, actor: User, override: bool
) -> list[str]:
    """Check ``ensure_activation_allowed`` and flip ``agent`` to ``active``, or refuse.

    Refusal leaves ``agent`` untouched and returns the blocker list. Success
    sets ``status`` (NOT committed — the caller owns the transaction) and
    returns ``[]``.

    ``approved_at``/``approved_by`` are stamped ONLY on the pending→active
    transition, which is what the pre-refactor ``admin_approve_agent`` did.
    They record who first vouched for this agent; re-activating from
    ``inactive`` (a manager unmute) or ``suspended`` must not overwrite that
    provenance with whoever happened to flip the switch back on. The call site
    for both branches of ``admin_approve_agent`` and for the manager unmute
    (``src/services/agent_mute.py``). ``manager_activate_agent`` runs
    ``ensure_activation_allowed`` itself and flips the status with a conditional
    UPDATE, so a concurrent change is never overwritten (D-07). ``persona_blockers``
    apply even with ``override``.
    """
    if override:
        # The override waives the profile blockers only; log what it waived, with
        # the actor, as before. The persona checks and the hub limit are never waived.
        waived = await activation_blockers(db, agent)
        if waived:
            logger.warning(
                "Activation OVERRIDE by %s for agent %s (%s) despite: %s",
                actor.id, agent.agent_id, agent.id, "; ".join(waived),
            )
    blockers = await ensure_activation_allowed(
        db, agent, new_role=agent.role, new_status="active", override=override,
    )
    if blockers:
        return blockers
    was_pending = agent.status == "pending"
    agent.status = "active"
    if was_pending:
        agent.approved_at = datetime.now(UTC)
        agent.approved_by = actor.id
    return []
