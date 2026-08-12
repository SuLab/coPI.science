"""Task 12 — PI-tag repurpose: pitch-shaping context.

The `reply` action is gone (Task 6/7): a PI tagging a bot's channel post can no
longer make that bot join or reply to the thread. Instead the tag is folded
into the bot's next Phase 5 pitch as authoritative context — surfaced via a
new `## Your PI flagged this` section in the phase-5 prompt (built from the
agent's current `pi_priority` `interesting_posts` entries) and consumed once
the turn's action resolves (`new_post` or `skip`), mirroring
`has_pi_directive`'s turn-scoped semantics. `handle_channel_tag`'s DM copy is
updated to say so instead of promising a reply/new-thread it can no longer
deliver.

Four tests:
  (a) a seeded pi_priority entry renders the exact heading into the phase-5
      prompt (and it is absent when there is nothing flagged).
  (b) the flagged entries are consumed after the turn resolves — both on a
      successful `new_post` and on an explicit `skip`.
  (c) `handle_channel_tag`'s DM text matches the new no-reply/pitch-shaping
      copy, for both the joinable and the already-two-agents branches.
  (d) `has_pi_priority` still bypasses `phase5_skip_probability` (the skip
      bypass is unchanged by this task).
"""
import types

from src.agent.agent import Agent
from src.agent.message_log import LogEntry, MessageLog
from src.agent.pi_handler import PIHandler
from src.agent.simulation import SimulationEngine
from src.agent.state import PostRef
from tests.fakes import FakeSlackClient


def _lab(agent_id="gill", bot_name="GillBot", pi_name="Gill"):
    return Agent(agent_id, bot_name, pi_name, role="pi_lab")


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _settings(**over):
    base = dict(
        daily_post_cap=5,
        active_thread_threshold=12,
        unreviewed_proposal_block_count=2,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


_PITCH = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": "pitch", "tagged_agent": null}\n'
    '```\n\n'
    '<slack_message>:bulb: Pitch — a thing worth screening.</slack_message>'
)
_SKIP = '```json\n{"action": "skip"}\n```'


def _pi_post(post_id="p1", pi_context="PI said: focus on X"):
    return PostRef(
        post_id=post_id, channel="general", sender_agent_id="blackbird",
        content_snippet="a post the PI tagged", posted_at=0.0,
        pi_priority=True, pi_context=pi_context,
    )


async def _drive(monkeypatch, response, *, skip_probability=0.0, seed_pi_post=True):
    lab = _lab()
    lab.allowed_sender_ids = None
    hub = _hub()
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: _settings(phase5_skip_probability=skip_probability),
    )

    captured = {"calls": 0, "messages": None}

    async def _fake_generate(**kwargs):
        captured["calls"] += 1
        captured["messages"] = kwargs["messages"]
        return response

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    if seed_pi_post:
        lab.state.interesting_posts = [_pi_post()]

    await eng._phase5_new_post(lab)
    return eng, lab, client, captured


# ---------------------------------------------------------------------------
# (a) heading injection
# ---------------------------------------------------------------------------

def test_pi_flagged_renders_the_exact_heading():
    a = _lab()
    note = "- PI said: focus on X (post p1 in #general: 'a post the PI tagged')"

    _, messages = a.build_phase5_prompt(pi_flagged=note)
    content = messages[0]["content"]
    # The exact injected block, verbatim — not just a loose substring check,
    # since the Instructions section already mentions the heading NAME in
    # backticks as static prose ("if a section titled `## Your PI flagged
    # this` appears above...").
    injected = (
        "\n\n## Your PI flagged this\n\n" + note +
        "\n\nYour PI's direction is authoritative — see the Instructions section."
    )
    assert injected in content


def test_no_pi_flagged_omits_the_heading():
    """The Instructions section already mentions the heading NAME in backticks
    ("if a section titled `## Your PI flagged this` appears above...") — that
    static reference is not the injected section. Assert the injected marker
    (the trailing authoritative-direction sentence, unique to the appended
    block) is absent instead of a bare substring check on the heading text."""
    a = _lab()
    _, messages = a.build_phase5_prompt()
    assert "Your PI's direction is authoritative — see the Instructions section." not in messages[0]["content"]

    _, messages_empty = a.build_phase5_prompt(pi_flagged="")
    assert "Your PI's direction is authoritative — see the Instructions section." not in messages_empty[0]["content"]


# ---------------------------------------------------------------------------
# (b) consume-once semantics
# ---------------------------------------------------------------------------

async def test_pi_flagged_entry_is_surfaced_and_consumed_on_new_post(monkeypatch):
    _eng, lab, client, captured = await _drive(monkeypatch, _PITCH)

    # It reached the LLM prompt this turn...
    content = captured["messages"][0]["content"]
    assert "## Your PI flagged this" in content
    assert "PI said: focus on X" in content
    assert "p1" in content

    # ...the post actually went out...
    assert len(client.posted) == 1

    # ...and the flagged entry is gone from interesting_posts afterward.
    assert all(p.post_id != "p1" for p in lab.state.interesting_posts)


async def test_pi_flagged_entry_is_consumed_on_skip(monkeypatch):
    _eng, lab, _client, captured = await _drive(monkeypatch, _SKIP)

    content = captured["messages"][0]["content"]
    assert "## Your PI flagged this" in content

    assert all(p.post_id != "p1" for p in lab.state.interesting_posts)


# ---------------------------------------------------------------------------
# (c) DM copy
# ---------------------------------------------------------------------------

async def test_dm_copy_for_joinable_thread_says_no_reply_but_will_pitch():
    lab = _lab()
    hub = _hub()
    client = FakeSlackClient(agent_id="gill")
    handler = PIHandler(
        agents={"gill": lab, "blackbird": hub},
        slack_clients={"gill": client},
        pi_slack_id_to_agent_ids={"U_PI": ["gill"]},
        message_log=MessageLog(),
    )

    entry = LogEntry(
        ts="100.0", channel="general", sender_agent_id=None, sender_name="PI",
        content="@GillBot please look into this", posted_at=100.0,
    )
    await handler.handle_channel_tag("gill", entry)

    dm_text = client.posted[-1]["text"]
    assert dm_text == (
        "Saw your tag in #general. I can't reply to posts in this workspace, "
        "but I'll fold it into my next pitch to the hub."
    )

    seeded = lab.state.interesting_posts[-1]
    assert seeded.pi_priority is True
    assert seeded.pi_context == "PI said: @GillBot please look into this"


async def test_dm_copy_for_two_agent_thread_says_cannot_join_but_will_pitch():
    lab = _lab()
    hub = _hub()
    wu = Agent("wu", "WuBot", "Wu", role="pi_lab")
    client = FakeSlackClient(agent_id="gill")
    message_log = MessageLog()
    message_log.append(LogEntry(
        ts="200.0", channel="general", sender_agent_id="wu", sender_name="WuBot",
        content="root post", posted_at=200.0,
    ))
    message_log.append(LogEntry(
        ts="201.0", channel="general", sender_agent_id="wu", sender_name="WuBot",
        content="reply", thread_ts="200.0", posted_at=201.0,
    ))
    message_log.append(LogEntry(
        ts="202.0", channel="general", sender_agent_id="blackbird", sender_name="BlackbirdBot",
        content="reply", thread_ts="200.0", posted_at=202.0,
    ))
    handler = PIHandler(
        agents={"gill": lab, "blackbird": hub, "wu": wu},
        slack_clients={"gill": client},
        pi_slack_id_to_agent_ids={"U_PI": ["gill"]},
        message_log=message_log,
    )

    tag_entry = LogEntry(
        ts="203.0", channel="general", sender_agent_id=None, sender_name="PI",
        content="@GillBot look at this", thread_ts="200.0", posted_at=203.0,
    )
    await handler.handle_channel_tag("gill", tag_entry)

    dm_text = client.posted[-1]["text"]
    assert dm_text.startswith("That thread already has two agents (")
    assert "I can't join or reply to it, but I'll fold your note into my next pitch to the hub." in dm_text

    seeded = lab.state.interesting_posts[-1]
    assert seeded.pi_priority is True
    assert "start a new thread with" not in seeded.pi_context.lower()


# ---------------------------------------------------------------------------
# (d) the has_pi_priority skip bypass stays
# ---------------------------------------------------------------------------

async def test_pi_priority_still_bypasses_random_skip(monkeypatch):
    _eng, _lab_obj, _client, captured = await _drive(
        monkeypatch, _SKIP, skip_probability=1.0, seed_pi_post=True,
    )
    assert captured["calls"] == 1, "pi_priority must bypass phase5_skip_probability"


async def test_without_pi_priority_random_skip_still_fires(monkeypatch):
    _eng, _lab_obj, _client, captured = await _drive(
        monkeypatch, _SKIP, skip_probability=1.0, seed_pi_post=False,
    )
    assert captured["calls"] == 0, "no pi_priority post -> random skip still applies"
