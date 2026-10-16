"""A PI tagging their own bot gets a reply, even while the bot is blocked.

End to end over the real PIHandler.handle_channel_tag -> cohort-gate reconcile
-> _run_turn path. Two defects hid the reply: the 30 s gate reconcile deleted
the queued PI-priority post (its sender_agent_id is the PI's display name, which
is never in the cohort), and Phase 5 never let a blocked agent reply to it.
"""

import time
from unittest.mock import AsyncMock

import pytest

import src.agent.simulation as sim_mod
from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.pi_handler import PIHandler
from src.agent.simulation import SimulationEngine
from src.agent.state import PostRef, ProposalRef


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    monkeypatch.setattr(sim_mod.random, "random", lambda: 1.0)
    su = Agent("su", "SuBot", "Andrew Su")
    wu = Agent("wu", "WuBot", "Chunlei Wu")
    engine = SimulationEngine(agents=[su, wu], slack_clients={})
    for a in (su, wu):
        a.state.subscribed_channels = {"general"}
        # Gate on, both bots in one cohort — as in production.
        a.allowed_sender_ids = {"su", "wu"}
    engine._pi_handler = PIHandler(
        agents=engine.agents, slack_clients={},
        pi_slack_id_to_agent_ids={"UPISU": ["su"]},
        message_log=engine.message_log,
    )
    engine._phase1_channel_discovery = AsyncMock()
    engine._update_agent_memory = AsyncMock()
    engine._post_message = AsyncMock(return_value=True)
    llm = {"calls": [], "target": None}

    async def fake_agent_response(**kw):
        llm["calls"].append(kw)
        if kw.get("log_meta", {}).get("phase") == "new_post":
            return (
                f'{{"action": "reply", "target_post_id": "{llm["target"]}", '
                '"channel": "general"}\n'
                "<slack_message>Happy to look at this with you.</slack_message>"
            )
        return '{"selected_post_ids": []}'

    monkeypatch.setattr(sim_mod, "generate_agent_response", fake_agent_response)
    monkeypatch.setattr(sim_mod, "generate_with_tools", AsyncMock(return_value=""))
    return engine, su, llm


def _block(agent):
    agent.state.pending_proposals = [
        ProposalRef(thread_id=f"p{i}", channel="general", other_agent_id="wu",
                    summary_text="s", proposed_at=0.0)
        for i in range(2)
    ]


async def test_blocked_bot_replies_to_its_pis_tag_after_a_gate_reconcile(world):
    engine, su, llm = world
    now = time.time()
    su.state.last_seen_cursor = now - 600
    _block(su)
    assert engine._unreviewed_proposal_block_active(su)
    root = f"{now - 300:.6f}"
    engine.message_log.append(LogEntry(
        ts=root, channel="general", sender_agent_id="wu", sender_name="WuBot",
        content="New CRISPR screen results", posted_at=now - 300, is_bot=True,
    ))
    # A real Slack tag arrives as <@Uxxx>, which Phase 3's literal-name match
    # does not see, so this reply can only come through the PI-priority path.
    pi_tag = LogEntry(
        ts=f"{now - 30:.6f}", channel="general", sender_agent_id=None,
        sender_name="Andrew Su", content="<@U0SUBOT> please engage here",
        thread_ts=root, posted_at=now - 30, is_bot=False,
    )
    engine.message_log.append(pi_tag)
    su.state.has_pi_directive = True
    await engine._pi_handler.handle_channel_tag("su", pi_tag)
    llm["target"] = root

    engine._apply_cohort_gate_to_state()  # what every 30 s roster sync runs
    assert [p.post_id for p in su.state.interesting_posts if p.pi_priority] == [root]

    await engine._run_turn(su)

    engine._post_message.assert_awaited_once()
    args, kwargs = engine._post_message.await_args
    assert args[:2] == ("su", "general")
    assert kwargs["thread_ts"] == root
    assert su.state.has_pi_directive is False


async def test_a_repeated_tag_replaces_the_queued_post(world):
    engine, su, _ = world
    now = time.time()
    root = f"{now - 300:.6f}"
    engine.message_log.append(LogEntry(
        ts=root, channel="general", sender_agent_id="wu", sender_name="WuBot",
        content="root", posted_at=now - 300, is_bot=True,
    ))
    for i, text in enumerate(["<@U0SUBOT> first", "<@U0SUBOT> second"]):
        await engine._pi_handler.handle_channel_tag("su", LogEntry(
            ts=f"{now - 30 + i:.6f}", channel="general", sender_agent_id=None,
            sender_name="Andrew Su", content=text, thread_ts=root,
            posted_at=now - 30 + i, is_bot=False,
        ))

    assert [(p.post_id, p.content_snippet) for p in su.state.interesting_posts] == [
        (root, "<@U0SUBOT> second"),
    ]


async def test_phase2_prune_keeps_a_pi_priority_post_the_llm_dropped(world, monkeypatch):
    engine, su, _ = world
    su.state.interesting_posts = [
        PostRef(post_id=str(i), channel="general", sender_agent_id="wu",
                content_snippet="x", posted_at=float(i))
        for i in range(3)
    ] + [PostRef(post_id="pi", channel="general", sender_agent_id="Andrew Su",
                 content_snippet="@SuBot look", posted_at=9.0, pi_priority=True)]

    async def prune(**kw):
        return '{"keep_post_ids": ["0"]}'

    monkeypatch.setattr(sim_mod, "generate_agent_response", prune)

    await engine._phase2_prune(su)

    assert [p.post_id for p in su.state.interesting_posts] == ["0", "pi"]
