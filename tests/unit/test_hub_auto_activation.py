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
from src.agent.state import ThreadState


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
    double-activated (i.e. re-processed/overwritten) by the hub loop after the
    tag loop has already activated it.

    Pins the hub loop's own `already active` guard specifically, not just the
    outcome: a sentinel ThreadState (a distinctive message_count=99, which
    `get_thread_message_count` could never produce for this 1-message thread)
    is pre-seeded under the thread id before `_phase3_activate_threads` runs.
    If the hub loop's guard fires, the sentinel is untouched. A same-shape
    ThreadState from a real activation (built via the tag loop, or a
    from-scratch hub-loop activation) would NOT carry message_count=99, so an
    unguarded second write is caught even though it would otherwise look like
    a harmless overwrite. Verified empirically: deleting the hub loop's
    `if thread_id in agent.state.active_threads: continue` line makes this
    test fail (the sentinel gets clobbered); restoring it passes again.
    """
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "gill", "GillBot", "@BlackbirdBot take a look at this")
    )
    sentinel = ThreadState(
        thread_id="1", channel="general", other_agent_id="gill", message_count=99,
    )
    hub.state.active_threads["1"] = sentinel

    eng._phase3_activate_threads(hub)

    assert list(hub.state.active_threads.keys()) == ["1"]
    assert hub.state.active_threads["1"] is sentinel
    assert hub.state.active_threads["1"].message_count == 99, (
        "the sentinel was overwritten — the hub loop's already-active guard "
        "did not fire"
    )


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


# ---------------------------------------------------------------------------
# Human-authored entries never activate a thread (2026-08-12 PI-interaction
# removal cycle). The trigger loop this closes: `post_agent_message`/
# `reopen_proposal` (via `src/services/pi_inbox.py::record_pi_message`) write
# an `is_bot=False` row into `agent_messages`; the engine's DB-inbound poller
# ingests it into the shared MessageLog; and — before this fix — Phase 3's
# three loops (fed by `get_tags_for_agent`/`get_replies_to_agent_posts`/
# `get_new_top_level_posts`, none of which check `is_bot` — those reads
# deliberately still return human rows for history/observability, decision 5)
# would activate a thread against it, with `SimulationEngine._infer_agent_id`'s
# substring match (`agent_id in name_lower or bot_name in name_lower`) able to
# misattribute `other_agent_id` from a human sender name that happens to
# contain a bot's agent_id (e.g. "Andrew Su (PI)" contains "su"). The guard is
# an explicit `if not entry.is_bot: continue` in each of
# `_phase3_activate_threads`'s three loops (`src/agent/simulation.py`) — at the
# point activation actually happens, not in the shared MessageLog reads.
# ---------------------------------------------------------------------------


def _human_post(ts, channel, name, content, thread_ts=None):
    return LogEntry(
        ts=ts, channel=channel, sender_agent_id=None, sender_name=name,
        content=content, thread_ts=thread_ts, posted_at=float(ts), is_bot=False,
    )


def test_human_tagged_post_does_not_activate_the_tag_loop():
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _human_post("1", "general", "Andrew Su (PI)", "Hey @GillBot, please check this")
    )

    eng._phase3_activate_threads(lab)

    assert lab.state.active_threads == {}


def test_human_reply_to_the_agents_own_post_does_not_activate_the_reply_loop():
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "gill", "GillBot", "Our new finding")
    )
    eng.message_log.append(
        _human_post("2", "general", "Dr PI", "Nice work", thread_ts="1")
    )

    eng._phase3_activate_threads(lab)

    assert lab.state.active_threads == {}


def test_human_untagged_post_does_not_auto_activate_a_hub_thread():
    """The hub loop's analogue of test_untagged_lab_post_activates_a_hub_thread:
    a human top-level post must not open a hub interview thread."""
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _human_post("1", "general", "Dr PI", "We just published something new.")
    )

    eng._phase3_activate_threads(hub)

    assert hub.state.active_threads == {}


def test_human_sender_name_substring_matching_a_bot_agent_id_does_not_activate():
    """The exact substring-match trap `_infer_agent_id` could otherwise walk
    into: "Andrew Su (PI)" contains "su" — a REAL agent_id in this roster
    (SuBot), which never posted anything. Even if the human filter were
    somehow bypassed, a thread fabricated and misattributed to "su" would be
    the observable damage; this pins that it never happens at all."""
    hub, lab = _hub(), _lab()
    su = Agent("su", "SuBot", "Su", role="pi_lab")
    eng = _engine(hub, lab, su)
    eng.message_log.append(
        _human_post("1", "general", "Andrew Su (PI)", "hey @GillBot take a look")
    )

    eng._phase3_activate_threads(lab)

    assert lab.state.active_threads == {}


def test_control_bot_tagged_post_still_activates_the_tag_loop():
    """Positive control for the three human-inertness tests above: the same
    shape of entry, bot-authored, still activates normally."""
    hub, lab = _hub(), _lab()
    eng = _engine(hub, lab)
    eng.message_log.append(
        _post("1", "general", "wu", "WuBot", "Hey @GillBot, please check this")
    )

    eng._phase3_activate_threads(lab)

    assert "1" in lab.state.active_threads
