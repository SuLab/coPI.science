"""The reply lane services every pending pair without pacing, and the post lane
no longer has a reactive tier.

Task 11 splits the single turn-based pool into two lanes: a paced post lane
(``_run_post_turn`` — Phase 1 + Phase 5) and an unpaced reply lane (this file).
Nothing else calls ``_phase3_activate_threads`` once ``_run_post_turn`` stops
doing it, so ``_dispatch_reply_lane`` runs it for every agent before computing
the pending-pairs queue — otherwise a brand-new @-mention or reply would never
open a thread at all.
"""
import time

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient


async def _noop_async(*_a, **_kw):
    return None


def _engine_with_pending(n):
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    for i in range(n):
        hub.state.active_threads[f"t{i}"] = ThreadState(
            thread_id=f"t{i}", channel="general", other_agent_id=f"pi{i}",
            message_count=1, has_pending_reply=True,
        )
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    return eng, hub


def test_pending_pairs_lists_every_owed_reply():
    eng, hub = _engine_with_pending(5)
    pairs = eng._pending_reply_pairs()
    assert {t.thread_id for _a, t in pairs} == {f"t{i}" for i in range(5)}
    assert all(a.agent_id == "blackbird" for a, _t in pairs)


@pytest.mark.asyncio
async def test_dispatch_services_all_pending_pairs(monkeypatch):
    eng, hub = _engine_with_pending(5)
    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert n == 5
    assert sorted(served) == [f"t{i}" for i in range(5)]


def test_the_reactive_tier_is_gone():
    import inspect

    src = inspect.getsource(SimulationEngine._select_agent)
    assert "_owes_reply" not in src, "the post lane must not do reactive selection"
    assert "_reactive_streak" not in src


# ---------------------------------------------------------------------------
# Phase 3 (thread activation) has no other caller once _run_post_turn drops it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_activates_newly_tagged_threads(monkeypatch):
    """Nothing but _dispatch_reply_lane calls _phase3_activate_threads now that
    _run_post_turn is Phase 1 + 5 only — without this, a brand-new @-mention
    would never open a thread at all."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng.message_log.set_bot_name_map({"blackbirdbot": "blackbird"})
    eng.message_log.append(LogEntry(
        ts="1.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="hey @BlackbirdBot", thread_ts=None, posted_at=1.0, is_bot=True,
    ))

    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)

    n = await eng._dispatch_reply_lane()

    assert "1.0" in hub.state.active_threads, "Phase 3 never activated the new tag"
    assert served == ["1.0"]
    assert n == 1


@pytest.mark.asyncio
async def test_dispatch_advances_the_cursor_so_phase3_does_not_rescan_forever(
    monkeypatch,
):
    """Mirrors Task 6's snapshot-then-assign cursor invariant, applied to the
    reply lane: Phase 3 depends on last_seen_cursor to bound its "since cursor"
    scans (get_tags_for_agent / get_replies_to_agent_posts /
    get_new_top_level_posts are all O(len(log)) linear scans — see
    message_log.py). Without this, every agent the post lane does not happen to
    pick would rescan the entire message log from turn zero on every single
    main-loop tick, forever.
    """
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng.message_log.append(LogEntry(
        ts="5.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="unrelated post", thread_ts=None, posted_at=5.0, is_bot=True,
    ))
    monkeypatch.setattr(eng, "_service_reply", _noop_async)

    await eng._dispatch_reply_lane()

    assert hub.state.last_seen_cursor == 5.0


@pytest.mark.asyncio
async def test_dispatch_does_not_hide_a_reply_that_arrives_during_its_own_pass(
    monkeypatch,
):
    """The cursor must advance from a snapshot taken BEFORE Phase 3 runs, not
    after — advancing early would make _pending_reply_pairs' own
    has_new_reply_from_other check compare against a cursor that already
    covers the very reply it is trying to detect."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng.message_log.append(LogEntry(
        ts="1.0", channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="root", thread_ts=None,
        posted_at=1.0, is_bot=True,
    ))
    hub.state.active_threads["1.0"] = ThreadState(
        thread_id="1.0", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=False,
    )
    eng.message_log.append(LogEntry(
        ts="2.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="a reply", thread_ts="1.0", posted_at=2.0, is_bot=True,
    ))

    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)

    n = await eng._dispatch_reply_lane()

    assert served == ["1.0"], "a reply already in the log before this pass started was missed"
    assert n == 1


# ---------------------------------------------------------------------------
# Carry forward from Task 10: Phase 4 activity resets the skip-backoff streak
# but must never stamp last_phase5_action_time — that reset lived inside
# _run_turn's Phase-4 block, which no longer exists now that Phase 4 moved
# into _service_reply entirely.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_reply_resets_the_skip_streak(monkeypatch):
    hub, _thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    hub.state.consecutive_phase5_skips = 3
    monkeypatch.setattr(eng, "_reply_to_thread", _noop_async)

    await eng._service_reply(hub, _thread)

    assert hub.state.consecutive_phase5_skips == 0, (
        "reply-lane activity must still clear the skip-backoff streak"
    )


@pytest.mark.asyncio
async def test_service_reply_does_not_stamp_the_spontaneous_timer(monkeypatch):
    hub, _thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    stale_time = time.time() - 10**9
    hub.state.last_phase5_action_time = stale_time
    monkeypatch.setattr(eng, "_reply_to_thread", _noop_async)

    await eng._service_reply(hub, _thread)

    assert hub.state.last_phase5_action_time == stale_time, (
        "only a real Phase 5 action (inside _phase5_new_post) may stamp this — "
        "reply-lane activity conflating replying with posting is exactly the "
        "cross-lane coupling Task 10 removed"
    )


def _hub_with_one_pending_thread():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    return hub, thread
