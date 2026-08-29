"""Reconstructing the interview thread behind a stored verdict.

Deliberately dependency-free beyond stdlib + SQLAlchemy + `src.models`: this
module is imported by the worker's review-bot job handler (Task 10), and the
worker must never pull in `src.services.blackbird_rubric` or
`src.services.rubric_revisions` — both fail-fast parse a TOML document under
`prompts/` at import time. Once the worker bind-mounts `prompts/` (Task 14), a
mid-edit or malformed rubric document would otherwise crash-loop the whole
worker process (`restart: unless-stopped`), taking profile generation and
email notifications down with it. Kept free-standing, a bad TOML degrades only
the one job that reads it as data.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentMessage, OpportunityAssessment

# Hard bound on how many rows of one interview thread a single load will
# return. Mirrors `assessment_detail.MESSAGE_SCAN_LIMIT` — kept as a private
# constant here rather than imported, since importing from `assessment_detail`
# would reintroduce exactly the coupling this module exists to avoid.
MESSAGE_SCAN_LIMIT = 500


async def load_interview_thread(
    db: AsyncSession, assessment: OpportunityAssessment
) -> tuple[str | None, list[AgentMessage]]:
    """The interview thread this verdict came out of.

    Anchored on the message the verdict was POSTED as: ``slack_ts`` is written
    from the Slack post (or the locally minted ts when Slack is off), and it
    can land in either ``AgentMessage.slack_ts`` or ``AgentMessage.message_ts``
    depending on which side minted it — so both are tried.

    Returns ``(None, [])`` whenever the thread cannot be reconstructed, which
    is a NORMAL outcome, not an error: ``--fresh`` wipes ``agent_messages`` and
    never wipes ``opportunity_assessments``, so an older verdict legitimately
    outlives its own transcript. The caller renders the verdict either way.
    """
    if not assessment.slack_ts:
        return None, []
    anchor = (
        await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == assessment.simulation_run_id,
                AgentMessage.channel_name == assessment.channel_name,
                or_(
                    AgentMessage.slack_ts == assessment.slack_ts,
                    AgentMessage.message_ts == assessment.slack_ts,
                ),
            )
            .order_by(AgentMessage.posted_at, AgentMessage.created_at)
            .limit(1)
        )
    ).scalars().first()
    if anchor is None:
        return None, []
    # The thread's id is the ROOT's ts: a reply carries it in thread_ts, and the
    # root itself carries None there and is its own thread.
    thread_id = anchor.thread_ts or anchor.message_ts
    if thread_id is None:
        return None, [anchor]
    rows = (
        await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == assessment.simulation_run_id,
                AgentMessage.channel_name == anchor.channel_name,
                or_(
                    AgentMessage.thread_ts == thread_id,
                    AgentMessage.message_ts == thread_id,
                ),
            )
            .order_by(AgentMessage.posted_at, AgentMessage.created_at)
            .limit(MESSAGE_SCAN_LIMIT)
        )
    ).scalars().all()
    return thread_id, list(rows)
