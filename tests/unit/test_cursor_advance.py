"""The cursor must not mark as read anything the turn did not actually see.

Retargeted for Task 11 (the post lane no longer touches Phase 3/4 at all —
those moved to `_dispatch_reply_lane` / `_service_reply`, see
tests/unit/test_reply_lane.py's own cursor-advancement tests). The invariant
this file pins is unchanged from Task 6: `_run_post_turn` snapshots
`message_log.latest_timestamp` at the very top, before Phase 1 runs, and only
assigns `max(old_cursor, snapshot)` at the very end — so a message that lands
mid-turn (after the snapshot, e.g. from another agent's concurrent activity)
is never swallowed.

The brief's own drafted test (a fixed far-future `_LATE_TS = 9_999_999_999.0`)
passes by accident: `time.time()` at turn-end is always far below that
constant regardless of whether the cursor is a start-of-turn snapshot or the
turn-end wall clock, so the assertion tells us nothing about the bug. See
Ruling P4 in the task-6 report for the reasoning behind the fixture below.

The discriminator that actually distinguishes the two implementations is
*when* the late message's timestamp is captured relative to the turn-end
`time.time()` call, not its absolute size. The fixture below timestamps the
late entry with real `time.time()` from *inside* Phase 5 — after Phase 1 has
already read the log, but strictly before the code at the end of
`_run_post_turn` executes. Wall-clock time cannot run backwards over the
microseconds a test takes to execute, so:

- under the OLD code (`last_seen_cursor = time.time()`, evaluated after every
  phase including the late append), the turn-end read is guaranteed to be >=
  the late entry's timestamp — the entry gets swallowed, deterministically,
  not by luck.
- under the FIXED code (snapshot `message_log.latest_timestamp` at the very
  top of `_run_post_turn`, before any phase runs, then `max(old_cursor,
  snapshot)` at the end), the snapshot was taken before the late entry
  existed, so the cursor cannot advance past it.
"""
import time

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


@pytest.mark.asyncio
async def test_cursor_does_not_swallow_messages_that_arrive_mid_turn(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    # Due for a spontaneous post, so Phase 5 actually runs this turn.
    agent.state.last_phase5_action_time = time.time() - 10**9
    start_cursor = agent.state.last_seen_cursor

    late_ts_box: dict[str, float] = {}

    async def _phase5(_a):
        # A message lands from elsewhere while this turn is mid-flight — after
        # Phase 1 already read the log, timestamped with real "now".
        late_ts = time.time()
        late_ts_box["ts"] = late_ts
        eng.message_log.append(_late_entry(late_ts))

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    await eng._run_post_turn(agent)

    late_ts = late_ts_box["ts"]
    # The late entry must still be unread: the cursor may only advance to what
    # the turn observed when it started, not to "now" at turn-end.
    assert agent.state.last_seen_cursor < late_ts
    assert agent.state.last_seen_cursor >= start_cursor


def _late_entry(ts: float) -> LogEntry:
    return LogEntry(
        ts=f"{ts:.6f}", channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="late arrival", thread_ts=None,
        posted_at=ts, is_bot=True,
    )
