"""The activation gate (audit H4; coverage-plan P3, the no-migration subset).

Auto-created pending agents (manager Add-PI flow, 2026-08-24) broke the old
structural guarantee that a pending AgentRegistry row implies a completed
profile — `/agent/request` required one, the Add-PI flow mints the row before
the generation job even runs. An admin working a bulk install-links doc can
therefore reach "Approve & Activate" on an agent whose profile job died, whose
export never happened, or whose profile is the model's priors dressed up as a
researcher (the Kavran-class fabrication). This module is the refusal, called
from BOTH activation branches of ``admin_approve_agent`` — the pending→active
approval and the edit form's status dropdown (the bypass P3 warned about).

``pi_lab``-scoped: the hub and specialist roles have no PI profile by design.
The override is an explicit form field and is logged by the caller.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, Job, ResearcherProfile

logger = logging.getLogger(__name__)


async def activation_blockers(db: AsyncSession, agent: AgentRegistry) -> list[str]:
    """Reasons this agent must not be flipped to ``active``; [] when clear."""
    if agent.role != "pi_lab":
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
