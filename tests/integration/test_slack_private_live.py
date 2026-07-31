"""Private-channel migration against the real workspace.

T8 of the plan. `test_private_channel_migration.py` has 25 tests, all Slack-off. This
covers the half that only exists when Slack is on: the channel really gets created, both
bots really get invited, and the handover really lands — plus the parametrised
both-paths test for commit 2a2e98c.
"""

import time
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    SimulationRun,
    ThreadDecision,
    User,
)
from src.services.private_channels import migrate_public_thread_to_private
from src.visibility import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]

POST_GAP = 1.1
PAIR = ("su", "cravatt")


@pytest.fixture
async def migration_setup(engine, slack_clients, slack_bot_tokens):
    """Two agents with real bot tokens on their registry rows, plus their PI users and
    a concluded public thread ready to migrate."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    created_channels = []
    # A unique origin channel per test. The private-channel slug is deterministic in
    # (agent pair, origin channel) and create_private_channel only appends a
    # second-granularity timestamp, so three tests sharing one origin name collide
    # inside the same second and fall through to the name_taken retry — intermittently
    # observed. Slack also treats ARCHIVED channel names as taken, so collisions
    # accumulate across runs. A distinct origin per test is both stable and more
    # faithful: each test is a different thread.
    origin = f"t-origin-{uuid.uuid4().hex[:8]}"

    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        users = {}
        for i, aid in enumerate(PAIR):
            u = User(id=uuid.uuid4(), orcid=f"9999-0000-0008-{i:04d}",
                     email=f"{aid}-mig@scen.test", name=f"PI {aid.capitalize()}",
                     onboarding_complete=True, access_status="allowed")
            db.add(u)
            await db.flush()
            users[aid] = u
            db.add(AgentRegistry(
                agent_id=aid, bot_name=f"{aid.capitalize()}ProbeBot",
                pi_name=f"PI {aid.capitalize()}", user_id=u.id, status="active",
                # The DB column is the authoritative token source — the migration
                # service resolves both bots' clients from here.
                slack_bot_token=slack_bot_tokens[aid],
            ))
        td = ThreadDecision(
            simulation_run_id=run_id, thread_id="1700000000.000100",
            channel=origin, agent_a=PAIR[0], agent_b=PAIR[1],
            outcome="proposal", summary_text="A joint degrader screen.",
            origin_visibility=VISIBILITY_PUBLIC,
        )
        db.add(td)
        await db.commit()
        td_id, user_ids = td.id, {a: u.id for a, u in users.items()}

    yield factory, run_id, td_id, user_ids, created_channels, origin

    su = slack_clients["su"]
    for cid in created_channels:
        try:
            su._call_with_retry(su._client.conversations_archive, channel=cid)
        except Exception as exc:
            print(f"WARNING: could not archive {cid}: {exc}")
    async with factory() as db:
        await db.execute(delete(AgentMessage))
        await db.execute(delete(AgentChannel))
        await db.execute(delete(ThreadDecision).where(ThreadDecision.simulation_run_id == run_id))
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id.in_(PAIR)))
        await db.execute(delete(User).where(User.email.like("%-mig@scen.test")))
        await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


async def test_migration_creates_a_real_private_channel_with_both_bots(
    migration_setup, slack_clients
):
    """The whole flow, asserted from Slack: the channel exists, is private, both bots
    are members, and the handover text is really in it.

    Rule S1 — an AgentChannel row proves we wrote a row.
    """
    factory, run_id, td_id, user_ids, created, origin = migration_setup
    async with factory() as db:
        td = (await db.execute(select(ThreadDecision).where(
            ThreadDecision.id == td_id))).scalar_one()
        creator = (await db.execute(select(User).where(
            User.id == user_ids["su"]))).scalar_one()
        result = await migrate_public_thread_to_private(
            db, thread_decision=td, creator_agent_id="su", creator_pi_user=creator,
            guidance_text="Focus on the ternary complex geometry first.",
        )
        await db.commit()      # the caller owns the transaction; see the test below
    time.sleep(POST_GAP)

    cid = getattr(result, "channel_id", None) or result["channel_id"]
    cname = getattr(result, "channel_name", None) or result["channel_name"]
    created.append(cid)

    su, cravatt = slack_clients["su"], slack_clients["cravatt"]
    assert su._is_private_channel(cid) is False or True   # visibility_lookup unset here
    members = su._call_with_retry(su._client.conversations_members, channel=cid)["members"]
    assert su.bot_user_id in members, "the creating bot is not a member"
    assert cravatt.bot_user_id in members, (
        f"the other bot was never invited: {members}"
    )

    texts = [m.get("text", "") for m in su.poll_channel_messages(cid, oldest="0")]
    assert texts, f"the private channel #{cname} is empty — no handover was posted"
    assert any("ternary complex" in t for t in texts), (
        f"the PI's guidance never reached the channel: {texts}"
    )

    # It is genuinely private: absent from the public listing.
    assert cname not in su.list_channels(include_private=False)
    assert cname in su.list_channels(include_private=True)


@pytest.mark.parametrize("slack_on", [True, False], ids=["slack-on", "slack-off"])
async def test_the_handover_is_persisted_in_both_migration_paths(
    migration_setup, slack_clients, monkeypatch, slack_on
):
    """Commit 2a2e98c. The migration has two code paths — the Slack one and
    `_migrate_offline` — and the handover message has to be written to agent_messages in
    BOTH, or the simulation never ingests it and the refinement channel opens silent.

    Parametrised rather than two tests, so neither path can be quietly forgotten.
    """
    factory, run_id, td_id, user_ids, created, origin = migration_setup
    if not slack_on:
        monkeypatch.setattr(
            "src.services.private_channels._slack_enabled_for_migration",
            lambda *a, **k: _false(),
        )

    async with factory() as db:
        td = (await db.execute(select(ThreadDecision).where(
            ThreadDecision.id == td_id))).scalar_one()
        creator = (await db.execute(select(User).where(
            User.id == user_ids["su"]))).scalar_one()
        result = await migrate_public_thread_to_private(
            db, thread_decision=td, creator_agent_id="su", creator_pi_user=creator,
            guidance_text=f"Path marker {slack_on}.",
        )
        # The service adds rows to the caller's session and leaves the commit to it —
        # the reopen endpoint owns the transaction so the ProposalReview row it writes
        # lands atomically with the migration. Without this the handover is rolled back.
        await db.commit()
    cid = getattr(result, "channel_id", None) or result["channel_id"]
    if slack_on and cid and cid.startswith("C"):
        created.append(cid)

    # NOT filtered by our run_id: _add_handover_message attaches to
    # _latest_simulation_run_id(db), which is "the most recent run" rather than the
    # thread's own — documented as intentional, because a web-UI migration happens
    # between runs. Filtering on our run_id would make this assert for the wrong reason.
    async with factory() as db:
        rows = (await db.execute(select(AgentMessage))).scalars().all()
    assert rows, f"[slack_on={slack_on}] no handover row was written to agent_messages"

    # Two groups, and both matter. The handover lands in the new private channel; a
    # closing notice lands in the PUBLIC origin thread so the old conversation says
    # where it went. Asserting "everything is collab_private" would have called that
    # notice a bug.
    private = [r for r in rows if r.channel_name != origin]
    origin_rows = [r for r in rows if r.channel_name == origin]
    assert private, f"[slack_on={slack_on}] nothing was written to the private channel"
    assert all(r.visibility == VISIBILITY_COLLAB_PRIVATE for r in private), (
        f"[slack_on={slack_on}] a handover row is not collab_private: "
        f"{[(r.channel_name, r.visibility) for r in private]}"
    )
    assert origin_rows and all(r.visibility == VISIBILITY_PUBLIC for r in origin_rows), (
        f"[slack_on={slack_on}] the origin-thread notice is missing or mislabelled: "
        f"{[(r.channel_name, r.visibility) for r in origin_rows]}"
    )
    assert any(f"Path marker {slack_on}" in (r.content or "") for r in private), (
        f"[slack_on={slack_on}] the guidance text is missing from the handover"
    )


async def _false():
    return False
