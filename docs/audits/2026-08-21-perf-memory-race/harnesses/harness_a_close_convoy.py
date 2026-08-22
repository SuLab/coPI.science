"""Harness A — reply-lane convoy: _close_thread holds thread+agent locks and a
semaphore slot across TWO sequential LLM memory calls.

Uses the REAL SimulationEngine, real LockRegistry, real _dispatch_reply_lane,
real _reply_to_thread/_close_thread/_update_agent_memory. Only the LLM calls
and Slack client are faked (with genuine `await asyncio.sleep`, mirroring
tests/integration/test_concurrent_thread_safety.py's methodology).

Scenario: star topology. The hub has N_CLOSE threads at max_thread_messages
(so `_reply_to_thread` system-enforce-closes each: no reply LLM call, but
_close_thread fires TWO memory-synthesis LLM calls under the hub's agent
lock). Two unrelated pi_lab pairs owe ordinary replies (one generate_with_tools
call each, no locks beyond their own thread lock).

Measured questions:
  1. Do the hub's closes serialize on the hub agent lock?  (predict: yes)
  2. Do ordinary, lock-independent replies stall behind the closes because the
     blocked closes hold all reply_lane_max_in_flight semaphore slots?
Control: same run with memory synthesis outside the measurement (no-op) to
show the machinery itself is fast.
"""
import asyncio
import os
import sys
import time

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

MEM_LATENCY = 1.0    # stand-in for one memory-synthesis LLM call (prod: ~5-30s)
TOOL_LATENCY = 0.2   # stand-in for one ordinary thread-reply LLM call

N_CLOSE = 4          # concluding hub interviews pending this tick

EVENTS: list[tuple[float, str]] = []
T0 = [0.0]


def mark(tag: str) -> None:
    EVENTS.append((time.monotonic() - T0[0], tag))


def seed_history(engine, thread_id, channel, count, a, b, a_name, b_name):
    for i in range(count):
        engine.message_log.append(LogEntry(
            ts=f"{thread_id}-msg{i}", channel=channel,
            sender_agent_id=b if i % 2 else a,
            sender_name=b_name if i % 2 else a_name,
            content=f"message {i}", thread_ts=thread_id,
            posted_at=float(i), is_bot=True,
        ))


async def fake_generate_agent_response(system_prompt, messages, model=None,
                                       max_tokens=1000, log_meta=None,
                                       on_retry=None):
    meta = log_meta or {}
    mark(f"MEM-start {meta.get('agent_id')}")
    await asyncio.sleep(MEM_LATENCY)
    mark(f"MEM-end   {meta.get('agent_id')}")
    return "updated working memory"


async def fake_generate_with_tools(system_prompt, messages, tools,
                                   tool_executor, model=None, max_tokens=1000,
                                   max_tool_rounds=5, log_meta=None,
                                   on_retry=None, should_continue=None):
    meta = log_meta or {}
    mark(f"REPLY-start {meta.get('agent_id')}")
    await asyncio.sleep(TOOL_LATENCY)
    mark(f"REPLY-end   {meta.get('agent_id')}")
    return "<slack_message>Interesting — tell me more.</slack_message>"


def build_engine(mem_noop: bool):
    agents = []
    clients = {}
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    agents.append(hub)
    clients["blackbird"] = FakeSlackClient(agent_id="blackbird")

    for i in range(N_CLOSE):
        aid = f"wang{i}"
        a = Agent(aid, f"Wang{i}Bot", f"Wang {i}", role="pi_lab")
        agents.append(a)
        clients[aid] = FakeSlackClient(agent_id=aid)

    for aid in ("oa1", "oa2", "ob1", "ob2"):
        a = Agent(aid, f"{aid.capitalize()}Bot", aid.upper(), role="pi_lab")
        agents.append(a)
        clients[aid] = FakeSlackClient(agent_id=aid)

    eng = SimulationEngine(agents=agents, slack_clients=clients)

    settings = get_settings()
    maxm = settings.max_thread_messages

    # Hub interviews at the system-enforced close boundary.
    for i in range(N_CLOSE):
        tid = f"close-t{i}"
        th = ThreadState(thread_id=tid, channel="general",
                         other_agent_id=f"wang{i}", has_pending_reply=True)
        hub.state.active_threads[tid] = th
        # Give the partner the same active thread so _close_thread's
        # other-agent branch runs (as in production).
        pth = ThreadState(thread_id=tid, channel="general",
                          other_agent_id="blackbird")
        eng.agents[f"wang{i}"].state.active_threads[tid] = pth
        seed_history(eng, tid, "general", maxm, "blackbird", f"wang{i}",
                     "BlackbirdBot", f"Wang{i}Bot")

    # Two ordinary, lock-independent reply pairs.
    for a, b in (("oa1", "oa2"), ("ob1", "ob2")):
        tid = f"ord-{a}"
        th = ThreadState(thread_id=tid, channel="general", other_agent_id=b,
                         has_pending_reply=True)
        eng.agents[a].state.active_threads[tid] = th
        seed_history(eng, tid, "general", 4, a, b,
                     f"{a.capitalize()}Bot", f"{b.capitalize()}Bot")

    if mem_noop:
        async def noop_mem(agent, event, visibility="public", channel_id=None):
            return None
        eng._update_agent_memory = noop_mem
    return eng


async def run(mem_noop: bool):
    EVENTS.clear()
    sim.generate_agent_response = fake_generate_agent_response
    sim.generate_with_tools = fake_generate_with_tools
    eng = build_engine(mem_noop)
    eng._running = True
    T0[0] = time.monotonic()
    await eng._dispatch_reply_lane()
    total = time.monotonic() - T0[0]
    return total, list(EVENTS), eng


def analyze(label, total, events):
    print(f"\n=== {label} ===")
    for t, tag in events:
        print(f"  {t:7.3f}s  {tag}")
    mem_starts = [(t, g) for t, g in events if g.startswith("MEM-start")]
    reply_starts = [(t, g) for t, g in events if g.startswith("REPLY-start")]
    print(f"  total wall: {total:.3f}s")
    if mem_starts:
        print(f"  first memory call: {mem_starts[0][0]:.3f}s, last: {mem_starts[-1][0]:.3f}s")
    if reply_starts:
        print(f"  first ORDINARY reply start: {reply_starts[0][0]:.3f}s "
              f"(its own LLM call takes {TOOL_LATENCY}s)")


async def main():
    settings = get_settings()
    print(f"reply_lane_max_in_flight={settings.reply_lane_max_in_flight}, "
          f"max_thread_messages={settings.max_thread_messages}, "
          f"N_CLOSE={N_CLOSE}, MEM_LATENCY={MEM_LATENCY}s, TOOL_LATENCY={TOOL_LATENCY}s")

    total, events, eng = await run(mem_noop=False)
    analyze("DEFECT RUN (real _close_thread -> 2 memory LLM calls under locks)", total, events)
    ideal = 2 * MEM_LATENCY + TOOL_LATENCY
    print(f"\n  If closes ran concurrently (no shared hub lock) wall time would be ~{ideal:.1f}s; "
          f"measured {total:.3f}s => serialization factor {total/ideal:.1f}x")

    total2, events2, _ = await run(mem_noop=True)
    analyze("CONTROL RUN (memory synthesis removed from the lock span)", total2, events2)


asyncio.run(main())
