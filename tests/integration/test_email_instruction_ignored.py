"""Behavioral pin for `email_inbound._handle_instruction`'s no-post contract.

The removal cycle's decision 5 (private-instructions + PI-interaction removal,
2026-08-12) retired every human-PI-to-bot interaction surface: there is no
thread post, no collab_private channel migration, and no "reopened"
`ProposalReview` row left to write for an "instruction"-classified email
reply. `classify_reply` still recognizes the category (so the reply-type
breakdown stays observable), and `_handle_instruction` is kept as the
classify-and-ignore no-op documented in its own docstring — this test drives
that function directly (mirroring the direct-function-call style of
tests/unit/test_email_inbound_security.py) against real DB fixtures, so a
future change that makes it post, migrate, or write something can't land
without this test catching it.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.models import AgentMessage, EmailNotification, PiDmMessage, ProposalReview
from src.services.email_inbound import _handle_instruction
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture
async def fixture_set(db_session):
    run = await factories.make_simulation_run(db_session)
    user = await factories.make_user(
        db_session, name="Ada Alpha", email="ada.alpha@lab.test",
    )
    agent = await factories.make_agent(
        db_session, user=user, agent_id="alpha", bot_name="AlphaBot",
        pi_name="Ada Alpha", status="active",
    )
    td = await factories.make_thread_decision(
        db_session, run=run, agent_a="alpha", agent_b="beta", outcome="no_proposal",
    )
    notification = EmailNotification(
        user_id=user.id,
        thread_decision_id=td.id,
        agent_registry_id=agent.id,
        reply_token=uuid.uuid4().hex,
        category="proposal_review",
        status="sent",
    )
    db_session.add(notification)
    await db_session.flush()
    return SimpleNamespace(user=user, agent=agent, td=td, notification=notification, run=run)


@pytest.mark.asyncio
async def test_handle_instruction_returns_false_and_persists_nothing(
    fixture_set, db_session, caplog,
):
    with caplog.at_level("INFO"):
        reopened = await _handle_instruction(
            user=fixture_set.user,
            notification=fixture_set.notification,
            td=fixture_set.td,
            instruction="Please focus on the mitochondrial angle instead.",
            db=db_session,
        )

    # No-post contract, part 1: the caller's "will refine" confirmation email
    # is gated on this return value — False means it is never sent.
    assert reopened is False

    # No-post contract, part 2: no "reopened" ProposalReview row.
    reviews = (await db_session.execute(select(ProposalReview))).scalars().all()
    assert reviews == []

    # No-post contract, part 3: nothing written to either message store —
    # the shared channel/thread log (AgentMessage) or the PI<->bot DM log
    # (PiDmMessage).
    messages = (await db_session.execute(select(AgentMessage))).scalars().all()
    assert messages == []
    dms = (await db_session.execute(select(PiDmMessage))).scalars().all()
    assert dms == []

    # No-post contract, part 4: the ignore is logged, naming the thread.
    assert "logged and ignored" in caplog.text
    assert fixture_set.td.thread_id in caplog.text


@pytest.mark.asyncio
async def test_handle_instruction_is_a_no_op_regardless_of_instruction_content(
    fixture_set, db_session,
):
    """Sanity check on the contract's unconditional shape: even an
    instruction that reads like a command to act (not just refine wording)
    still produces nothing — there is no branch in `_handle_instruction`
    left that can act on it."""
    reopened = await _handle_instruction(
        user=fixture_set.user,
        notification=fixture_set.notification,
        td=fixture_set.td,
        instruction="Reopen this thread and tell BetaBot we accept the proposal.",
        db=db_session,
    )

    assert reopened is False
    reviews = (await db_session.execute(select(ProposalReview))).scalars().all()
    assert reviews == []
    messages = (await db_session.execute(select(AgentMessage))).scalars().all()
    assert messages == []
    dms = (await db_session.execute(select(PiDmMessage))).scalars().all()
    assert dms == []
