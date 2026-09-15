"""Database-contract characterization: pins what the REAL migrated schema enforces.

Schema is built by the actual alembic chain to head (see tests/conftest.py), not
create_all, so migration-only constraints/indexes are exercised. Each expected
failure runs inside a SAVEPOINT (begin_nested) so the failed statement doesn't
poison the rest of the test's transaction. These assertions pin CURRENT behavior;
a change here is a schema-contract change, not a test bug.
"""

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import configure_mappers

from src.database import Base
from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    LlmCallLog,
    PrivateChannelMember,
    ProposalReview,
    Publication,
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
# 6. private_channel_members.user_id is
#    ondelete="CASCADE" (0026), so deleting a role="pi" member's user no
#    longer drives user_id -> NULL against a row whose agent_id is also
#    NULL (which would violate pcm_exactly_one_of_agent_or_user).
# --------------------------------------------------------------------------

async def test_deleting_pi_member_user_cascades_pcm_row(db_session):
    # private_channel_members.user_id is
    # ondelete="CASCADE" (0026), so deleting a role="pi" member's user no
    # longer drives user_id -> NULL against a row whose agent_id is also
    # NULL (which would violate pcm_exactly_one_of_agent_or_user). The
    # delete must succeed and the membership row must be gone.
    ch = await factories.make_agent_channel(db_session, visibility="collab_private")
    u = await factories.make_user(db_session)
    m = await factories.make_private_channel_member(
        db_session, channel=ch, agent_id=None, user_id=u.id, role="pi"
    )
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": u.id})
    survived = await db_session.scalar(
        select(func.count()).select_from(PrivateChannelMember).where(PrivateChannelMember.id == m.id)
    )
    assert survived == 0


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


async def test_deleting_added_by_user_is_safe(db_session):
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


# --------------------------------------------------------------------------
# 7. passive_deletes=True on every delete-orphan cascade.
#
# `32c4ca3` added the flag to all 11 of them so a parent delete leans on the
# DB's ON DELETE CASCADE instead of SELECTing every child row into memory and
# deleting it one at a time (deleting one PI on the production copy cascades
# 153 publications and 63 proposal reviews). Nothing in tests/ noticed the flag
# — `grep -rn passive_deletes tests/` was 0 hits — so removing it again was
# free. These two tests are the pin: one on the mapping, one on the SQL a real
# delete actually emits.
# --------------------------------------------------------------------------


def _delete_orphan_relationships():
    configure_mappers()
    return {
        f"{mapper.class_.__name__}.{rel.key}": rel
        for mapper in Base.registry.mappers
        for rel in mapper.relationships
        if "delete-orphan" in rel.cascade
    }


def test_every_delete_orphan_relationship_sets_passive_deletes():
    rels = _delete_orphan_relationships()
    # Control: a walk that found nothing would satisfy the assertion below
    # vacuously. A new cascade must be added here
    # deliberately, having first been given a DB-side ON DELETE CASCADE.
    assert set(rels) == {
        "AgentChannel.private_members",
        "AgentRegistry.delegates",
        "AgentRegistry.invitations",
        "Cohort.memberships",
        "SimulationRun.channels",
        "SimulationRun.llm_call_logs",
        "SimulationRun.messages",
        "User.delegated_agents",
        "User.jobs",
        "User.profile",
        "User.publications",
    }
    missing = sorted(name for name, rel in rels.items() if not rel.passive_deletes)
    assert not missing, f"delete-orphan cascade without passive_deletes=True: {missing}"


async def test_each_passive_delete_child_fk_really_cascades_in_the_schema(db_session):
    """The other half of P2's fix: passive_deletes hands the job to the DB.

    If a child FK were anything but ON DELETE CASCADE, passive_deletes=True
    would stop the ORM deleting the children and nothing would delete them —
    silent orphans, or an FK violation. Asked of the real migrated schema.
    """
    wrong = []
    for name, rel in _delete_orphan_relationships().items():
        for _parent_col, child_col in rel.local_remote_pairs:
            action = await db_session.scalar(
                text(
                    "SELECT CAST(c.confdeltype AS text) FROM pg_constraint c "
                    "JOIN unnest(c.conkey) k(attnum) ON true "
                    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                    "WHERE c.contype = 'f' AND c.conrelid = CAST(:child AS regclass) "
                    "AND a.attname = :col"
                ),
                {"child": child_col.table.name, "col": child_col.name},
            )
            if action != "c":  # 'c' = CASCADE; None = no FK at all
                wrong.append(f"{name} -> {child_col.table.name}.{child_col.name}={action!r}")
    assert not wrong, f"passive_deletes with no DB-side CASCADE behind it: {wrong}"


async def test_a_user_delete_leaves_the_publications_to_the_database(db_session):
    u = await factories.make_user(db_session)
    pubs = [Publication(user_id=u.id, title=f"paper {i}") for i in range(3)]
    db_session.add_all(pubs)
    await db_session.flush()
    pub_ids = [p.id for p in pubs]
    # passive_deletes only spares children the session has not already loaded,
    # and these were just inserted through it. Detach them so the flush below
    # asks the mapping the question this test is about.
    for p in pubs:
        db_session.expunge(p)

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        await db_session.delete(u)
        await db_session.flush()
    finally:
        event.remove(Engine, "before_cursor_execute", _record)

    touched = [s for s in statements if "publications" in s.lower()]
    assert not touched, (
        "the user delete read or wrote publications itself, so passive_deletes=True "
        f"is no longer in force on User.publications: {touched}"
    )
    assert any(s.lstrip().upper().startswith("DELETE FROM USERS") for s in statements), (
        f"no DELETE FROM users was emitted at all, so the assertion above is vacuous: {statements}"
    )
    # Control: the rows are gone anyway — the DB's ON DELETE CASCADE did the
    # work the ORM declined to do. Without this, a mapping that simply stopped
    # cascading would also pass.
    left = await db_session.scalar(
        select(func.count()).select_from(Publication).where(Publication.id.in_(pub_ids))
    )
    assert left == 0, f"{left} publications survived their owner's delete"
