"""The reply lane services every pending pair without pacing, and the post lane
no longer has a reactive tier.

Task 11 splits the single turn-based pool into two lanes: a paced post lane
(``_run_post_turn`` — Phase 1 + Phase 5) and an unpaced reply lane (this file).
Nothing else calls ``_phase3_activate_threads`` once ``_run_post_turn`` stops
doing it, so ``_dispatch_reply_lane`` runs it for every agent before computing
the pending-pairs queue — otherwise a brand-new @-mention or reply would never
open a thread at all.

Fix round 1 (task review) added the C2/I3/I4/I5 sections below. `_dispatch_
reply_lane` now checks ``self._running`` between pairs (I5), which is False
by default on a freshly constructed engine (it only becomes True inside
``start()``) — so every test that drives the real dispatch sets it explicitly.
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
    eng._running = True  # I5: the servicing loop now checks this between pairs
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
    eng._running = True
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
    eng._running = True
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
    eng._running = True
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
#
# Fix round 1 (task review, Important I3): the reset moved AGAIN, out of
# _service_reply and into _dispatch_reply_lane, so it fires at most once per
# agent per tick instead of once per pending pair. See test_dispatch_resets_*
# below.
# ---------------------------------------------------------------------------


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


def test_service_reply_no_longer_owns_the_skip_reset():
    """Structural pin for I3: the reset moved to _dispatch_reply_lane so it can
    fire once per agent per tick rather than once per pending pair inside
    _service_reply."""
    import inspect

    assert "consecutive_phase5_skips =" not in inspect.getsource(
        SimulationEngine._service_reply
    )
    assert "consecutive_phase5_skips =" in inspect.getsource(
        SimulationEngine._dispatch_reply_lane
    )


@pytest.mark.asyncio
async def test_dispatch_resets_the_skip_streak_for_an_engaged_agent(monkeypatch):
    eng, hub = _engine_with_pending(1)
    hub.state.consecutive_phase5_skips = 3
    monkeypatch.setattr(eng, "_service_reply", _noop_async)

    await eng._dispatch_reply_lane()

    assert hub.state.consecutive_phase5_skips == 0, (
        "reply-lane activity must still clear the skip-backoff streak"
    )


@pytest.mark.asyncio
async def test_dispatch_resets_skip_streak_even_when_service_reply_is_stubbed(
    monkeypatch,
):
    """Proves the reset lives in the dispatcher, not in _service_reply: a stub
    that does nothing to consecutive_phase5_skips must not prevent the reset,
    which would not be true if the reset still lived inside _service_reply."""
    eng, hub = _engine_with_pending(1)
    hub.state.consecutive_phase5_skips = 5

    async def _stub(agent, thread):
        pass  # deliberately does not touch consecutive_phase5_skips

    monkeypatch.setattr(eng, "_service_reply", _stub)
    await eng._dispatch_reply_lane()

    assert hub.state.consecutive_phase5_skips == 0


@pytest.mark.asyncio
async def test_dispatch_only_resets_agents_that_had_a_pending_pair(monkeypatch):
    """A second, idle agent with no pending pairs must not have its skip
    streak touched at all this tick."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    idle = Agent("wang", "WangBot", "Wang", role="pi_lab")
    idle.state.consecutive_phase5_skips = 4
    hub.state.active_threads["t0"] = ThreadState(
        thread_id="t0", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=True,
    )
    hub.state.consecutive_phase5_skips = 3
    eng = SimulationEngine(
        agents=[hub, idle],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
    )
    eng._running = True
    monkeypatch.setattr(eng, "_service_reply", _noop_async)

    await eng._dispatch_reply_lane()

    assert hub.state.consecutive_phase5_skips == 0
    assert idle.state.consecutive_phase5_skips == 4, (
        "an agent with no pending pair this tick must not be reset"
    )


# ---------------------------------------------------------------------------
# C2 — one failing pair must not abort the sweep, and every pair's retry
# signal must be promoted for the WHOLE batch before any servicing runs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_pair_does_not_abort_the_sweep(monkeypatch):
    """Before this fix, the servicing loop was a bare ``for ... await`` with
    no try/except, and its call site in _run_main_loop was the only await in
    the main-loop body not wrapped in one either — one pair raising took down
    the rest of the sweep (and, uncaught, the whole tick)."""
    eng, hub = _engine_with_pending(3)
    served = []

    async def _serve(agent, thread):
        if thread.thread_id == "t1":
            raise RuntimeError("boom")
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert sorted(served) == ["t0", "t2"], (
        "a sibling pair's failure must not abort the rest of the sweep"
    )
    assert n == 3


# ---------------------------------------------------------------------------
# I4 — a thread closed (or evicted) by a sibling pair's call, mid-sweep, must
# not be serviced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_reply_skips_a_thread_closed_mid_sweep(monkeypatch):
    """`pairs` is snapshotted once by _dispatch_reply_lane before any of its
    (possibly many, possibly slow) LLM calls run, and _close_thread can pop
    this exact thread out from under a sibling pair's call in the same
    sweep. Without a guard, the agent spends a real Opus call replying into
    an already-closed thread."""
    hub, thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    thread.status = "closed"
    hub.state.active_threads.pop(thread.thread_id, None)

    called = []

    async def _fake_reply(_a, _t):
        called.append(True)

    monkeypatch.setattr(eng, "_reply_to_thread", _fake_reply)

    await eng._service_reply(hub, thread)

    assert called == [], (
        "must not call _reply_to_thread for a thread closed mid-sweep"
    )


@pytest.mark.asyncio
async def test_service_reply_skips_a_thread_evicted_from_active_threads(monkeypatch):
    """Other half of the guard: a ThreadState still reporting status=="active"
    but removed from agent.state.active_threads entirely (e.g. evicted) must
    also not be serviced."""
    hub, thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    hub.state.active_threads.pop(thread.thread_id, None)  # status left "active"

    called = []

    async def _fake_reply(_a, _t):
        called.append(True)

    monkeypatch.setattr(eng, "_reply_to_thread", _fake_reply)

    await eng._service_reply(hub, thread)

    assert called == []


# ---------------------------------------------------------------------------
# I5 — a shutdown request must be honoured within one reply's worth of
# latency, not after draining the whole sweep.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_stops_early_when_the_engine_stops_mid_sweep(monkeypatch):
    eng, hub = _engine_with_pending(5)
    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)
        if len(served) == 2:
            eng._running = False  # shutdown requested mid-sweep

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert served == ["t0", "t1"], (
        "must stop as soon as _running goes False, not drain the rest of the sweep"
    )
    assert n == 2
    # C2: the pairs never reached this tick must still carry a retry signal —
    # has_pending_reply was promoted for the WHOLE batch before servicing
    # started, independent of how far the loop actually got.
    for i in (2, 3, 4):
        assert hub.state.active_threads[f"t{i}"].has_pending_reply is True, (
            f"t{i} was never reached this tick but must still retry next dispatch"
        )


def _hub_with_one_pending_thread():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    return hub, thread
