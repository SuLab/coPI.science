"""Database-contract characterization: pins what the REAL migrated schema enforces.

Schema is built by the actual alembic chain to head (see tests/conftest.py), not
create_all, so migration-only constraints/indexes are exercised. Each expected
failure runs inside a SAVEPOINT (begin_nested) so the failed statement doesn't
poison the rest of the test's transaction. These assertions pin CURRENT behavior;
a change here is a schema-contract change, not a test bug.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    LlmCallLog,
    PrivateChannelMember,
    ProposalReview,
    ResearcherProfile,
    SimulationRun,
    ThreadDecision,
    User,
)
from tests import factories

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# 1. Native-enum rejection. asyncpg's InvalidTextRepresentationError surfaces as
#    the generic sqlalchemy.exc.DBAPIError (not the finer DataError psycopg gives).
# --------------------------------------------------------------------------

async def test_simulation_run_status_enum_rejects_unknown(db_session):
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(SimulationRun(status="paused", config={}))
            await db_session.flush()


async def test_agent_channel_type_enum_rejects_unknown(db_session):
    run = await factories.make_simulation_run(db_session)
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                AgentChannel(
                    simulation_run_id=run.id,
                    channel_id="C1",
                    channel_name="c",
                    channel_type="private",  # not thematic|collaboration
                    created_by_agent="agent1",
                )
            )
            await db_session.flush()


async def test_thread_decision_outcome_enum_rejects_unknown(db_session):
    run = await factories.make_simulation_run(db_session)
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                ThreadDecision(
                    simulation_run_id=run.id,
                    thread_id="1.0",
                    channel="c",
                    agent_a="a",
                    agent_b="b",
                    outcome="rejected",  # not proposal|no_proposal|timeout
                )
            )
            await db_session.flush()


async def test_agent_message_phase_accepts_arbitrary_string(db_session):
    # phase was migrated enum -> String(30) in 0003: no enum constraint remains.
    run = await factories.make_simulation_run(db_session)
    msg = await factories.make_agent_message(db_session, run=run, phase="some_new_phase")
    got = await db_session.get(AgentMessage, msg.id)
    assert got.phase == "some_new_phase"


# --------------------------------------------------------------------------
# 2. Unique constraints
# --------------------------------------------------------------------------

async def test_users_orcid_unique(db_session):
    u = await factories.make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(User(name="Dup", orcid=u.orcid, email="other@example.edu"))
            await db_session.flush()


async def test_users_email_unique(db_session):
    u = await factories.make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(User(name="Dup", orcid="9999-9999-9999-9999", email=u.email))
            await db_session.flush()


async def test_agents_agent_id_unique(db_session):
    a = await factories.make_agent(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(AgentRegistry(agent_id=a.agent_id, bot_name="B", pi_name="P"))
            await db_session.flush()


async def test_agents_user_id_unique(db_session):
    u = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=u, agent_id="ag-a")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AgentRegistry(agent_id="ag-b", bot_name="B", pi_name="P", user_id=u.id)
            )
            await db_session.flush()


async def test_researcher_profile_user_id_unique(db_session):
    u = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=u)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ResearcherProfile(user_id=u.id))
            await db_session.flush()


# --------------------------------------------------------------------------
# 3. NOT NULL
# --------------------------------------------------------------------------

async def test_users_name_not_null(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(User(name=None, orcid="1111-2222-3333-4444"))
            await db_session.flush()


async def test_users_orcid_not_null(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(User(name="No Orcid", orcid=None))
            await db_session.flush()


async def test_agent_message_phase_not_null(db_session):
    run = await factories.make_simulation_run(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AgentMessage(
                    simulation_run_id=run.id,
                    agent_id="a",
                    channel_id="C1",
                    channel_name="c",
                    phase=None,
                )
            )
            await db_session.flush()


# --------------------------------------------------------------------------
# 4. private_channel_members CHECK + partial unique indexes
# --------------------------------------------------------------------------

async def test_pcm_check_rejects_both_null(db_session):
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                PrivateChannelMember(
                    agent_channel_id=ch.id, agent_id=None, user_id=None, role="bot"
                )
            )
            await db_session.flush()


async def test_pcm_check_rejects_both_set(db_session):
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    u = await factories.make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                PrivateChannelMember(
                    agent_channel_id=ch.id, agent_id="agent1", user_id=u.id, role="bot"
                )
            )
            await db_session.flush()


async def test_pcm_check_accepts_exactly_one(db_session):
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    m = await factories.make_private_channel_member(db_session, channel=ch)
    assert m.agent_id is not None and m.user_id is None


async def test_pcm_partial_unique_agent(db_session):
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    await factories.make_private_channel_member(db_session, channel=ch, agent_id="dupbot")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                PrivateChannelMember(
                    agent_channel_id=ch.id, agent_id="dupbot", user_id=None, role="bot"
                )
            )
            await db_session.flush()


async def test_pcm_partial_unique_user(db_session):
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    u = await factories.make_user(db_session)
    await factories.make_private_channel_member(
        db_session, channel=ch, agent_id=None, user_id=u.id, role="pi"
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                PrivateChannelMember(
                    agent_channel_id=ch.id, agent_id=None, user_id=u.id, role="pi"
                )
            )
            await db_session.flush()


# --------------------------------------------------------------------------
# 5. FK ondelete=CASCADE from simulation_runs (raw DELETE pins the DB rule,
#    not ORM relationship cascade)
# --------------------------------------------------------------------------

async def test_simulation_run_delete_cascades_children(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(db_session, run=run)
    await factories.make_agent_channel(db_session, run=run)
    await factories.make_llm_call_log(db_session, run=run)

    # Children must exist BEFORE the delete, else the post-delete 0-count is
    # vacuous (a factory regression that stopped persisting would pass silently).
    for model in (AgentMessage, AgentChannel, LlmCallLog):
        n = await db_session.scalar(
            select(func.count()).select_from(model).where(model.simulation_run_id == run.id)
        )
        assert n == 1, f"{model.__name__} not persisted pre-delete"

    await db_session.execute(
        text("DELETE FROM simulation_runs WHERE id = :id"), {"id": run.id}
    )

    for model in (AgentMessage, AgentChannel, LlmCallLog):
        n = await db_session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.simulation_run_id == run.id)
        )
        assert n == 0, f"{model.__name__} rows not cascaded"


# --------------------------------------------------------------------------
# 6. DAT-1: deleting a User who is a PI member of a private channel. The
#    membership row must go WITH the user (FK CASCADE, migration 0036).
# --------------------------------------------------------------------------

async def test_dat1_deleting_pi_member_user_cascades_the_membership(db_session):
    """DAT-1, inverted by migration 0036 — and the inversion is the fix.

    Until 0036 this test asserted the OPPOSITE, and did so deliberately: it
    pinned a real defect so nothing could change it by accident.
    ``private_channel_members.user_id`` was ``ON DELETE SET NULL`` under
    ``CHECK ((agent_id IS NULL) <> (user_id IS NULL))``, so on a PI membership row
    (``agent_id`` already NULL) the cascade's own UPDATE drove BOTH owner columns
    to NULL and violated that CHECK. The DELETE therefore raised
    ``pcm_exactly_one_of_agent_or_user`` — meaning ANY user delete for a
    private-channel member 500'd, both ``POST /profile/delete-account`` and the
    admin delete.

    SET NULL was never a coherent rule for this column: the row's whole identity
    is the member it names, so a row with neither owner is not a degraded record,
    it is an unrepresentable one. 0036 recreates the FK as ON DELETE CASCADE, so
    the membership goes with the user and the CHECK is never asked to hold an
    impossible row. ``added_by_user_id`` stays SET NULL and is unaffected — see
    ``test_dat1_deleting_added_by_user_is_safe`` below, which is the contrast that
    makes the distinction visible: nulling the ADDER violates no CHECK, so that
    row must survive.

    The table had 0 production rows when 0036 landed, which is why the swap was
    cheap then and would only have got dearer.
    """
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    u = await factories.make_user(db_session)
    m = await factories.make_private_channel_member(
        db_session, channel=ch, agent_id=None, user_id=u.id, role="pi"
    )
    # Assert the row is really there first, so the post-delete 0-count below cannot
    # pass vacuously against a factory that stopped persisting.
    before = await db_session.scalar(
        select(func.count())
        .select_from(PrivateChannelMember)
        .where(PrivateChannelMember.id == m.id)
    )
    assert before == 1

    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": u.id})

    after = await db_session.scalar(
        select(func.count())
        .select_from(PrivateChannelMember)
        .where(PrivateChannelMember.id == m.id)
    )
    assert after == 0, "membership row must cascade away with its user, not be nulled"


async def test_user_delete_cascades_researcher_profile(db_session):
    # researcher_profiles.user_id FK is ondelete=CASCADE (0001) — deleting the user
    # removes the profile at the DB level (raw DELETE pins the rule, not ORM cascade).
    u = await factories.make_user(db_session)
    p = await factories.make_profile(db_session, user=u)
    before = await db_session.scalar(
        select(func.count()).select_from(ResearcherProfile).where(ResearcherProfile.id == p.id)
    )
    assert before == 1
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": u.id})
    after = await db_session.scalar(
        select(func.count()).select_from(ResearcherProfile).where(ResearcherProfile.id == p.id)
    )
    assert after == 0


async def test_proposal_review_unique_per_thread_and_agent(db_session):
    # uq_proposal_reviews_decision_agent (0004): one review per (thread_decision, agent).
    run = await factories.make_simulation_run(db_session)
    td = await factories.make_thread_decision(db_session, run=run)
    u = await factories.make_user(db_session)
    db_session.add(
        ProposalReview(thread_decision_id=td.id, agent_id="su", user_id=u.id, rating=4)
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                ProposalReview(thread_decision_id=td.id, agent_id="su", user_id=u.id, rating=5)
            )
            await db_session.flush()


async def test_dat1_deleting_added_by_user_is_safe(db_session):
    # Contrast: added_by_user_id is also SET NULL, but nulling it violates no
    # CHECK, so deleting an added_by user succeeds and the row survives.
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    adder = await factories.make_user(db_session)
    m = await factories.make_private_channel_member(
        db_session, channel=ch, agent_id="bot-x", added_by_user_id=adder.id
    )
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": adder.id})
    # Row must SURVIVE (SET NULL, not CASCADE) AND have the column nulled. A bare
    # `scalar(select(added_by_user_id))` can't tell these apart — it returns None both
    # when the row survives with a nulled column and when the row was deleted.
    survived = await db_session.scalar(
        select(func.count()).select_from(PrivateChannelMember).where(PrivateChannelMember.id == m.id)
    )
    assert survived == 1
    nulled = await db_session.scalar(
        select(PrivateChannelMember.added_by_user_id).where(PrivateChannelMember.id == m.id)
    )
    assert nulled is None
