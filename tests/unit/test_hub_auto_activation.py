"""Hub auto-activation (Approach C).

The scout hub previously needed either a `@BlackbirdBot` tag or a reply into
one of its own threads to open an interview — every lab that posted a plain
top-level update without tagging it went unseen by Phase 3 until some other
thread-adjacent event happened to surface it. This adds a third
`_phase3_activate_threads` loop, gated on the plain `agent.role == "scout_hub"`
attribute (see INV-E structural note 4 — this must NOT become a third
consumer of `self._roles_by_agent()`, the separately-recomputed role map): the
hub auto-activates on every new top-level post from an allowed sender (cohort
gate), no mention required, mirroring the tag loop's `_closed_thread_ids` /
already-active / `get_thread_allowed_agents` guards exactly.
"""
from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine


def _post(ts, channel, agent_id, name, content, thread_ts=None):
    return LogEntry(
        ts=ts,
        channel=channel,
        sender_agent_id=agent_id,
        sender_name=name,
        content=content,
        thread_ts=thread_ts,
        posted_at=float(ts),
        is_bot=True,
    )


def _hub():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    hub.allowed_sender_ids = None
    hub.state.subscribed_channels = {"general"}
    hub.state.last_seen_cursor = 0.0
    return hub


def _lab(agent_id="gill", bot_name="GillBot", pi_name="Gill"):
    lab = Agent(agent_id, bot_name, pi_name, role="pi_lab")
    lab.allowed_sender_ids = None
    lab.state.subscribed_channels = {"general"}
    lab.state.last_seen_cursor = 0.0
    return lab


def _engine(*agents):
    return SimulationEngine(agents=list(agents), slack_clients={})


def test_untagged_lab_post_activates_a_hub_thread():
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "gill", "GillBot", "We just published something new.")
    )

    eng._phase3_activate_threads(hub)

    assert "1" in hub.state.active_threads, (
        "an untagged top-level post from an allowed sender must open a hub thread"
    )
    thread = hub.state.active_threads["1"]
    assert thread.other_agent_id == "gill"
    assert thread.channel == "general"
    assert thread.has_pending_reply is True


def test_tagged_post_activates_exactly_one_thread_no_dupe_with_tag_loop():
    """A post that both tags the hub AND is a new top-level post must not be
    double-activated by the tag loop and the new hub loop."""
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "gill", "GillBot", "@BlackbirdBot take a look at this")
    )

    eng._phase3_activate_threads(hub)

    assert list(hub.state.active_threads.keys()) == ["1"]


def test_pi_lab_agent_does_not_auto_activate_on_anothers_post():
    """The loop is gated on agent.role == 'scout_hub'. A pi_lab agent must not
    pick up another agent's untagged, non-reply top-level post."""
    hub = _hub()
    lab = _lab()
    other_lab = _lab(agent_id="wu", bot_name="WuBot", pi_name="Wu")
    eng = _engine(hub, lab, other_lab)
    eng.message_log.append(
        _post("1", "general", "gill", "GillBot", "no mention, no reply, just an update")
    )

    eng._phase3_activate_threads(other_lab)

    assert other_lab.state.active_threads == {}


def test_hubs_own_assessment_post_does_not_self_activate():
    """The hub's own terminal artifact is a top-level post it authored itself
    — exclude_agent_id must keep it from opening a thread against itself."""
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "blackbird", "BlackbirdBot", ":mag: Opportunity Assessment")
    )

    eng._phase3_activate_threads(hub)

    assert hub.state.active_threads == {}


def test_closed_thread_id_is_not_reactivated():
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "gill", "GillBot", "already handled elsewhere")
    )
    eng._closed_thread_ids.add("1")

    eng._phase3_activate_threads(hub)

    assert hub.state.active_threads == {}
