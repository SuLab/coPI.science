"""PI interaction over real Slack, with the real classifier.

T7 of the plan. These are `live_slack` AND `real_llm` — `handle_dm` routes on an LLM
classification, and the whole point is that the routing decision and the Slack delivery
are both real.

What cannot be automated here: sending a message *as the human*. That needs the PI's own
user token, which we do not have. So the inbound half is driven by calling `handle_dm`
with the text directly — the same entry point the poller calls — and the outbound half
is asserted by reading the DM back out of Slack.
"""

import os
import time
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.message_log import MessageLog
from src.agent.pi_handler import PIHandler
from src.models import AgentRegistry, ResearcherProfile, SimulationRun, User

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_slack,
    pytest.mark.real_llm,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                       reason="handle_dm classifies with a real LLM call"),
]

POST_GAP = 1.1


@pytest.fixture
async def pi_setup(engine, slack_clients, slack_pi_user_id):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    async with factory() as db:
        db.add(SimulationRun(id=run_id, status="running"))
        u = User(id=uuid.uuid4(), orcid="9999-0000-0007-0001",
                 email="pi-live@scen.test", name="PI Su",
                 onboarding_complete=True, access_status="allowed")
        # The PI<->Slack mapping is not a User column; the engine builds it in
        # _load_pi_mappings and hands it to PIHandler explicitly, which is what the
        # pi_slack_id_to_agent_ids argument below does.
        db.add(u)
        await db.flush()
        db.add(AgentRegistry(agent_id="su", bot_name="SuProbeBot", pi_name="PI Su",
                             user_id=u.id, status="active"))
        db.add(ResearcherProfile(
            user_id=u.id, research_summary="CRISPR screens.",
            techniques=["crispr"], keywords=["degrader"],
            private_profile_md="# Private\nNo standing instructions yet.",
        ))
        await db.commit()
        user_id = u.id

    agent = Agent(agent_id="su", bot_name="SuProbeBot", pi_name="PI Su")
    agent._public_profile = "# Su Lab\n\nGenome-scale CRISPR screens.\n"
    agent._private_profile = "No standing instructions yet."
    log = MessageLog()
    handler = PIHandler(
        agents={"su": agent}, slack_clients={"su": slack_clients["su"]},
        pi_slack_id_to_agent_ids={slack_pi_user_id: ["su"]},
        message_log=log, session_factory=factory, simulation_run_id=run_id,
    )
    yield handler, factory, run_id, user_id, slack_clients["su"], slack_pi_user_id

    async with factory() as db:
        await db.execute(delete(ResearcherProfile).where(
            ResearcherProfile.user_id == user_id))
        await db.execute(delete(AgentRegistry).where(AgentRegistry.agent_id == "su"))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


def _dm_texts(client, pi_user_id, since="0"):
    dm = client.open_dm_channel(pi_user_id)
    return [m.get("text", "") for m in client.poll_channel_messages(dm, oldest=since)]


async def test_a_question_dm_gets_a_real_reply_in_slack(pi_setup):
    """The full round trip: a real classification, a real Opus answer, delivered to a
    real Slack DM. Rule S1 — the reply is read back from Slack, not from a return value.
    """
    handler, factory, run_id, user_id, client, pi = pi_setup
    before = set(_dm_texts(client, pi))

    await handler.handle_dm("su", pi, "What are you currently working on?")
    time.sleep(POST_GAP)

    after = [t for t in _dm_texts(client, pi) if t not in before]
    assert after, "the bot sent no DM at all in reply to a question"
    assert len(" ".join(after)) > 40, f"the reply is suspiciously short: {after}"


async def test_a_standing_instruction_is_persisted_and_acknowledged(pi_setup):
    """Both halves. The acknowledgement alone would be satisfied by a handler that
    replied politely and wrote nothing; the DB write alone would be satisfied by one
    that silently stored it and never told the PI.
    """
    handler, factory, run_id, user_id, client, pi = pi_setup
    async with factory() as db:
        before = (await db.execute(select(ResearcherProfile.private_profile_md)
                                   .where(ResearcherProfile.user_id == user_id))).scalar_one()

    marker = f"ferroptosis-{uuid.uuid4().hex[:6]}"
    dm_before = set(_dm_texts(client, pi))
    await handler.handle_dm(
        "su", pi,
        f"From now on, always mention our interest in {marker} when proposing "
        "collaborations.",
    )
    time.sleep(POST_GAP)

    async with factory() as db:
        after = (await db.execute(select(ResearcherProfile.private_profile_md)
                                  .where(ResearcherProfile.user_id == user_id))).scalar_one()
    assert after != before, "the standing instruction was not written to the profile"
    assert marker in after, (
        f"the instruction was written but lost its content: {after[-400:]!r}"
    )
    assert [t for t in _dm_texts(client, pi) if t not in dm_before], (
        "the PI was never told the instruction had been recorded"
    )


async def test_a_plain_question_does_not_become_a_standing_instruction(pi_setup):
    """Control for the test above. A classifier that routed everything to
    standing_instruction would pass it — and would quietly rewrite the PI's profile on
    every question they ask.
    """
    handler, factory, run_id, user_id, client, pi = pi_setup
    async with factory() as db:
        before = (await db.execute(select(ResearcherProfile.private_profile_md)
                                   .where(ResearcherProfile.user_id == user_id))).scalar_one()

    await handler.handle_dm("su", pi, "Which channels are you currently in?")
    time.sleep(POST_GAP)

    async with factory() as db:
        after = (await db.execute(select(ResearcherProfile.private_profile_md)
                                  .where(ResearcherProfile.user_id == user_id))).scalar_one()
    assert after == before, (
        "a plain question rewrote the private profile — every question the PI asks "
        "would silently become a standing instruction"
    )


async def test_notify_thread_conclusion_dms_the_pi(pi_setup):
    """The outbound-only path. Asserted from Slack."""
    handler, factory, run_id, user_id, client, pi = pi_setup
    before = set(_dm_texts(client, pi))
    marker = uuid.uuid4().hex[:6]

    from src.agent.state import ThreadState

    thread = ThreadState(thread_id="1.0", channel="t-probe", other_agent_id="cravatt",
                         message_count=4)
    await handler.notify_thread_conclusion(
        agent_id="su", thread=thread, outcome="proposal",
        summary_text=f"Joint degrader screen [{marker}].",
    )
    time.sleep(POST_GAP)

    new = [t for t in _dm_texts(client, pi) if t not in before]
    assert new, "no conclusion DM was sent"
    assert any(marker in t for t in new), (
        f"the conclusion DM does not carry the summary: {new}"
    )
