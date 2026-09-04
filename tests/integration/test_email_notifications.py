"""Issue #20 COR-5/COR-13 residual (Task 20.9c): the engine's implicit
`rating=-1` review row (written by `_persist_implicit_proposal_review` when a PI
merely engages a proposal thread) must not silence the PI-facing readers in
`src/services/email_notifications.py`. This module covers
`_get_unreviewed_proposals_for_user`, which decides whether a proposal is
included in the review-request email sweep.

Database is REAL (the rolled-back `db_session` from tests/conftest.py).
"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import src.services.email_notifications as en
from src.models import ProposalReview, User
from tests import factories

pytestmark = pytest.mark.integration


async def _eager(db_session, user_id) -> User:
    """Re-fetch with User.agent loaded — `_get_unreviewed_proposals_for_user`
    reads `user.agent`, and a lazy load on an AsyncSession raises MissingGreenlet.
    `factories.make_user`'s returned object does not have the relationship loaded."""
    return (
        await db_session.execute(
            select(User).options(selectinload(User.agent)).where(User.id == user_id)
        )
    ).scalar_one()


async def test_unreviewed_query_ignores_implicit_minus_one_reviews(db_session):
    user = await factories.make_user(db_session, email="pi.implicit@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=td.id,
            agent_id=agent.agent_id,
            user_id=user.id,
            rating=-1,
        )
    )
    await db_session.flush()

    pi = await _eager(db_session, user.id)
    rows = await en._get_unreviewed_proposals_for_user(pi, db_session)

    assert [r[0].id for r in rows] == [td.id], (
        "a proposal whose only review is the engine's implicit rating=-1 marker "
        "was dropped from the unreviewed list — the review-request email would "
        "never be sent"
    )


async def test_unreviewed_query_still_excludes_explicit_reviews(db_session):
    """Control: an explicit (non -1) review still marks the proposal reviewed."""
    user = await factories.make_user(db_session, email="pi.explicit@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=td.id,
            agent_id=agent.agent_id,
            user_id=user.id,
            rating=4,
        )
    )
    await db_session.flush()

    pi = await _eager(db_session, user.id)
    rows = await en._get_unreviewed_proposals_for_user(pi, db_session)

    assert rows == []
