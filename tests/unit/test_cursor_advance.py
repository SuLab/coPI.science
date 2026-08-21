"""The cursor must not mark as read anything Phase 3 did not actually see.

Retargeted for Task 11 fix round 1 (task review, Critical C1). Originally this
file pinned `_run_turn`'s (later `_run_post_turn`'s) snapshot-then-assign
cursor invariant from Task 6. Task 11's first pass gave `_run_post_turn` its
OWN second cursor snapshot/assign, taken *after* `_dispatch_reply_lane` had
already run for the tick — including, potentially, many slow LLM calls that
post new messages. That second write had no reader of its own (neither Phase 1
nor Phase 5 reads `last_seen_cursor`), but it still clobbered the shared
per-agent field that `_dispatch_reply_lane`'s Phase 3 / `_pending_reply_pairs`
depend on — advancing an agent's cursor past a message its own Phase 3 scan
never processed, permanently stalling that thread with no backstop (the
`has_pending_reply` flag was already cleared by whatever reply made the
cursor move). The fix deletes `_run_post_turn`'s cursor code entirely:
`_dispatch_reply_lane` is now the sole reader AND sole writer.

This file now:
1. Retargets the original snapshot-then-assign regression test at
   `_dispatch_reply_lane` (the invariant itself, and the Ruling-P4-style
   discriminator, are unchanged from Task 6 — just relocated).
2. Pins that `_run_post_turn` touches `last_seen_cursor` at all — a structural
   guard against the exact regression this fix round found.
3. Adds the COMPOSITIONAL regression test the review flagged: no prior test
   ever called `_dispatch_reply_lane()` and `_run_post_turn()` in the same
   tick, which is exactly the shape `_run_main_loop` uses and exactly the gap
   the Critical bug hid in.

The brief's own drafted test (a fixed far-future `_LATE_TS = 9_999_999_999.0`)
passes by accident: `time.time()` at turn-end is always far below that
constant regardless of whether the cursor is a start-of-turn snapshot or the
turn-end wall clock, so the assertion tells us nothing about the bug. See
Ruling P4 in the task-6 report for the reasoning behind the fixture below.
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


@pytest.mark.asyncio
async def test_dispatch_does_not_swallow_messages_that_arrive_mid_sweep(monkeypatch):
    """The discriminator that actually distinguishes the two implementations
    is *when* the late message's timestamp is captured relative to the
    dispatch-end write, not its absolute size. The fixture times the late
    entry with real `time.time()` from *inside* `_service_reply` — after
    Phase 3 has already read the log for every agent, but strictly before
    `_dispatch_reply_lane` assigns the cursor. Wall-clock time cannot run
    backwards over the microseconds a test takes to execute, so a
    reintroduced "assign wall-clock-at-the-end" bug would swallow this
    deterministically, not by luck.
    """
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    eng._running = True
    agent.state.active_threads["t1"] = ThreadState(
        thread_id="t1", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=True,
    )
    start_cursor = agent.state.last_seen_cursor
    late_ts_box: dict[str, float] = {}

    async def _serve(_a, _t):
        # A message lands from elsewhere while this dispatch pass is
        # mid-flight — after Phase 3 already read the log for every agent,
        # timestamped with real "now".
        late_ts = time.time()
        late_ts_box["ts"] = late_ts
        eng.message_log.append(_late_entry(late_ts))

    monkeypatch.setattr(eng, "_service_reply", _serve)

    await eng._dispatch_reply_lane()

    late_ts = late_ts_box["ts"]
    # The late entry must still be unread: the cursor may only advance to
    # what this pass observed when it started, not to "now" at pass-end.
    assert agent.state.last_seen_cursor < late_ts
    assert agent.state.last_seen_cursor >= start_cursor


def test_run_post_turn_never_touches_the_cursor():
    """Structural guard against the exact regression fix round 1 found:
    neither Phase 1 nor Phase 5 reads `last_seen_cursor`, and
    `_dispatch_reply_lane` is its sole owner — `_run_post_turn` must not
    reference it at all."""
    import inspect

    assert "agent.state.last_seen_cursor" not in inspect.getsource(
        SimulationEngine._run_post_turn
    )


@pytest.mark.asyncio
async def test_a_post_turn_in_the_same_tick_as_a_dispatch_does_not_swallow_a_tag(
    monkeypatch,
):
    """Regression for Critical C1. `_run_main_loop` calls
    `_dispatch_reply_lane()` and then `_run_post_turn(agent)` in the SAME
    tick — no prior test called both together, which is exactly the gap the
    Critical bug hid in.

    Scenario: agent `wang` has nothing pending this tick. During the SAME
    tick's dispatch pass, a DIFFERENT agent (`cravatt`) is serviced and (as
    real reply-lane activity does) posts a new message — one that happens to
    `@`-mention `wang`. If `_run_post_turn(wang)`, called immediately
    afterward in the same tick, still wrote its own cursor (the pre-fix bug),
    it would advance `wang`'s cursor past that tag before `wang`'s own Phase 3
    ever got a chance to see it — so the NEXT dispatch's Phase 3 scan would
    already read it as "before cursor" and silently never activate the
    thread.
    """
    wang = Agent("wang", "WangBot", "Wang", role="pi_lab")
    cravatt = Agent("cravatt", "CravattBot", "Cravatt", role="pi_lab")
    eng = SimulationEngine(
        agents=[wang, cravatt],
        slack_clients={
            "wang": FakeSlackClient(agent_id="wang"),
            "cravatt": FakeSlackClient(agent_id="cravatt"),
        },
    )
    eng._running = True
    cravatt.state.active_threads["ct"] = ThreadState(
        thread_id="ct", channel="general", other_agent_id="wang",
        message_count=1, has_pending_reply=True,
    )
    # Not due for a spontaneous post — isolates this test to the cursor
    # question, independent of whatever Phase 5 might otherwise do.
    wang.state.last_phase5_action_time = time.time()

    async def _serve(agent, thread):
        if agent.agent_id == "cravatt":
            # Real reply-lane activity: composing this reply posts a new
            # message, which happens to tag wang.
            eng.message_log.append(LogEntry(
                ts="100.0", channel="general", sender_agent_id="cravatt",
                sender_name="CravattBot", content="hey @WangBot, new idea",
                thread_ts=None, posted_at=100.0, is_bot=True,
            ))

    monkeypatch.setattr(eng, "_service_reply", _serve)
    monkeypatch.setattr(eng, "_phase1_channel_discovery", _noop_async)
    monkeypatch.setattr(eng, "_phase5_new_post", _noop_async)

    # Tick 1: dispatch (posts the tag mid-sweep, via cravatt's pair), then a
    # post-lane turn for wang, in that order — exactly _run_main_loop's shape.
    await eng._dispatch_reply_lane()
    await eng._run_post_turn(wang)

    # Tick 2: dispatch again. wang's Phase 3 must still see the tag — i.e.
    # tick 1's post-lane turn must not have advanced wang's cursor past it.
    monkeypatch.setattr(eng, "_service_reply", _noop_async)
    await eng._dispatch_reply_lane()

    assert "100.0" in wang.state.active_threads, (
        "the tag posted mid-sweep in tick 1 was swallowed — wang's cursor "
        "was advanced past it by something other than wang's own Phase 3"
    )


def _late_entry(ts: float) -> LogEntry:
    return LogEntry(
        ts=f"{ts:.6f}", channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="late arrival", thread_ts=None,
        posted_at=ts, is_bot=True,
    )
