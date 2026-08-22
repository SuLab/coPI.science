"""Harness E — per-interview state that is never released in-process.

Drives REAL engine machinery (dispatch -> _reply_to_thread -> system-enforced
_close_thread) through many interview lifecycles and measures what stays
behind after each thread is fully closed and forgotten by the agents:

  - LockRegistry._locks (thread + agent registries): locks are created per key
    and never evicted (src/agent/locks.py has no removal path).
  - SimulationEngine._closed_thread_ids: insert-only by design.
  - SimulationEngine._prior_threads: one dict appended per close, no dedup cap.
  - MessageLog._entries / _by_ts: append-only, closed threads never pruned.

Memory numbers via tracemalloc; counts via len().
"""
import asyncio
import os
import sys
import tracemalloc

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-audit")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from src.agent.agent import Agent  # noqa: E402
from src.agent.message_log import LogEntry  # noqa: E402
from src.agent.simulation import SimulationEngine  # noqa: E402
from src.agent.state import ThreadState  # noqa: E402
from src.config import get_settings  # noqa: E402
from tests.fakes import FakeSlackClient  # noqa: E402
import src.agent.simulation as sim  # noqa: E402


async def fake_gar(*a, **kw):
    return "updated memory"

async def fake_gwt(*a, **kw):
    return "<slack_message>ok</slack_message>"

sim.generate_agent_response = fake_gar
sim.generate_with_tools = fake_gwt


async def main():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    pi = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, pi],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird"),
                       "wang": FakeSlackClient(agent_id="wang")},
    )
    async def noop_mem(agent, event, visibility="public", channel_id=None):
        return None
    eng._update_agent_memory = noop_mem  # keep the harness fast; memory files irrelevant here
    eng._running = True
    maxm = get_settings().max_thread_messages

    tracemalloc.start()
    base = tracemalloc.take_snapshot()

    N = 2000
    for i in range(N):
        tid = f"interview-{i}"
        hub.state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id="wang",
            has_pending_reply=True,
        )
        for m in range(maxm):
            eng.message_log.append(LogEntry(
                ts=f"{tid}-m{m}", channel="general",
                sender_agent_id="wang" if m % 2 else "blackbird",
                sender_name="WangBot" if m % 2 else "BlackbirdBot",
                content=f"interview {i} message {m} " + "x" * 200,
                thread_ts=tid, posted_at=float(i * 100 + m), is_bot=True,
            ))
        await eng._dispatch_reply_lane()  # system-enforced close of interview i
        eng._pending_persist.clear()      # what a DB flush would have drained

    snap = tracemalloc.take_snapshot()
    growth = sum(s.size_diff for s in snap.compare_to(base, "filename"))
    tracemalloc.stop()

    assert not hub.state.active_threads, "threads should all be closed"
    print(f"After {N} interview lifecycles (every thread CLOSED and gone from agent state):")
    print(f"  _thread_locks registry:  {len(eng._thread_locks._locks):>7,} locks (never evicted)")
    print(f"  _agent_locks registry:   {len(eng._agent_locks._locks):>7,} locks")
    print(f"  _closed_thread_ids:      {len(eng._closed_thread_ids):>7,} ids (insert-only)")
    pair = tuple(sorted(["blackbird", "wang"]))
    print(f"  _prior_threads[hub,pi]:  {len(eng._prior_threads.get(pair, [])):>7,} dicts (one per close, no cap)")
    print(f"  MessageLog entries:      {len(eng.message_log._entries):>7,} (closed threads never pruned)")
    print(f"  total heap growth:       {growth/1e6:8.1f} MB "
          f"({growth/N/1e3:.1f} KB per closed interview, retained forever)")


asyncio.run(main())
