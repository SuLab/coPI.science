"""Async model builders for tests.

Plain async helpers (not factory_boy's sync SQLAlchemy session API, which fights
AsyncSession). Each builder fills the columns the real models mark NOT NULL with no
server/Python default, uses a process-wide counter to keep unique columns
(orcid/email/agent_id) collision-free, applies **overrides last, adds+flushes on the
provided session (so PKs/server defaults populate), and returns the instance. All
writes live inside the test's rolled-back transaction — see tests/conftest.py.
"""

import itertools

from src.models import (
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    LlmCallLog,
    PrivateChannelMember,
    ResearcherProfile,
    SimulationRun,
    ThreadDecision,
    User,
)

_counter = itertools.count(1)

# The SES-stamped header every legitimately delivered inbound email carries.
# Shared by the inbound-email suites so a change to the validator's
# requirements (authserv-id, verdicts) is made in one place.
SES_PASS_HEADER = (
    "Authentication-Results: amazonses.com; spf=pass; dkim=pass; dmarc=pass\n"
)


async def make_user(session, **overrides) -> User:
    n = next(_counter)
    data = dict(
        name=f"Researcher {n}",
        orcid=f"0000-0000-0000-{n:04d}",
        email=f"user{n}@example.edu",
        institution="Test University",
        is_admin=False,
        onboarding_complete=True,
        access_status="allowed",
    )
    data.update(overrides)
    obj = User(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_profile(session, *, user=None, **overrides) -> ResearcherProfile:
    if user is None and "user_id" not in overrides:
        user = await make_user(session)
    data = dict(
        research_summary="Studies the thing.",
        techniques=["technique-a"],
        keywords=["keyword-a"],
        private_profile_md="# Private\nStuff.",
        profile_version=1,
    )
    if user is not None:
        data["user_id"] = user.id
    data.update(overrides)
    obj = ResearcherProfile(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_agent(session, *, user=None, **overrides) -> AgentRegistry:
    n = next(_counter)
    data = dict(
        agent_id=f"agent{n}",
        bot_name=f"Agent{n}Bot",
        pi_name=f"Researcher {n}",
        status="active",
    )
    if user is not None:
        data["user_id"] = user.id
    data.update(overrides)
    obj = AgentRegistry(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_simulation_run(session, **overrides) -> SimulationRun:
    data = dict(status="running", config={})
    data.update(overrides)
    obj = SimulationRun(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_agent_channel(session, *, run=None, **overrides) -> AgentChannel:
    if run is None and "simulation_run_id" not in overrides:
        run = await make_simulation_run(session)
    n = next(_counter)
    data = dict(
        channel_id=f"C{n:08d}",
        channel_name=f"channel-{n}",
        channel_type="thematic",
        created_by_agent="agent1",
    )
    if run is not None:
        data["simulation_run_id"] = run.id
    data.update(overrides)
    obj = AgentChannel(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_agent_message(session, *, run=None, **overrides) -> AgentMessage:
    if run is None and "simulation_run_id" not in overrides:
        run = await make_simulation_run(session)
    n = next(_counter)
    data = dict(
        agent_id="agent1",
        channel_id=f"C{n:08d}",
        channel_name=f"channel-{n}",
        phase="new_post",
        message_length=42,
    )
    if run is not None:
        data["simulation_run_id"] = run.id
    data.update(overrides)
    obj = AgentMessage(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_thread_decision(session, *, run=None, **overrides) -> ThreadDecision:
    if run is None and "simulation_run_id" not in overrides:
        run = await make_simulation_run(session)
    n = next(_counter)
    data = dict(
        thread_id=f"{n}.000100",
        channel=f"channel-{n}",
        agent_a="agent1",
        agent_b="agent2",
        outcome="proposal",
    )
    if run is not None:
        data["simulation_run_id"] = run.id
    data.update(overrides)
    obj = ThreadDecision(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_private_channel_member(
    session, *, channel=None, role="bot", **overrides
) -> PrivateChannelMember:
    if channel is None and "agent_channel_id" not in overrides:
        channel = await make_agent_channel(session, visibility="collab_private")
    # CHECK pcm_exactly_one_of_agent_or_user: exactly one of agent_id / user_id.
    data = dict(role=role, agent_id="agent1")
    if channel is not None:
        data["agent_channel_id"] = channel.id
    data.update(overrides)
    obj = PrivateChannelMember(**data)
    session.add(obj)
    await session.flush()
    return obj


async def make_llm_call_log(session, *, run=None, **overrides) -> LlmCallLog:
    if run is None and "simulation_run_id" not in overrides:
        run = await make_simulation_run(session)
    data = dict(
        agent_id="agent1",
        phase="decide",
        model="claude-test",
        system_prompt="sys",
        messages_json={"messages": []},
        response_text="resp",
    )
    if run is not None:
        data["simulation_run_id"] = run.id
    data.update(overrides)
    obj = LlmCallLog(**data)
    session.add(obj)
    await session.flush()
    return obj
