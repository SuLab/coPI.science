"""Execution verification of the remediation plan's two riskiest designs,
applied as a prototype to the scratchpad copy:

  Task 1 — memory-event queue replacing the in-lock LLM calls
  Task 5 — refcount-evicting LockRegistry

Checks (all against the REAL patched engine code):
  A. Convoy gone: harness-A scenario — no memory LLM call runs inside
     _dispatch_reply_lane; unrelated replies start immediately; drain then
     applies all events.
  B. Lost-update invariant preserved: two closes sharing the hub, then TWO
     CONCURRENT drains with an interleave-detecting fake — both events must
     survive in the hub's memory.
  C. stop() drains at most MEMORY_EVENTS_MAX_AT_SHUTDOWN and drops the rest.
  D. LockRegistry: mutual exclusion holds across eviction boundaries; idle
     keys are evicted; a waiter keeps its key alive.
  E. _prior_threads capped at PRIOR_THREADS_KEPT_PER_PAIR.
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-audit")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

import src.agent.simulation as sim  # noqa: E402
from src.agent.agent import Agent  # noqa: E402
from src.agent.locks import LockRegistry  # noqa: E402
from src.agent.message_log import LogEntry  # noqa: E402
from src.agent.simulation import SimulationEngine  # noqa: E402
from src.agent.state import ThreadState  # noqa: E402
from src.config import get_settings  # noqa: E402
from tests.fakes import FakeSlackClient  # noqa: E402

MEM_LATENCY = 1.0
TOOL_LATENCY = 0.2
N_CLOSE = 4
EVENTS: list[tuple[float, str]] = []
T0 = [0.0]


def mark(tag):
    EVENTS.append((time.monotonic() - T0[0], tag))


def seed(engine, tid, ch, count, a, b, an, bn):
    for i in range(count):
        engine.message_log.append(LogEntry(
            ts=f"{tid}-m{i}", channel=ch,
            sender_agent_id=b if i % 2 else a,
            sender_name=bn if i % 2 else an,
            content=f"m{i}", thread_ts=tid, posted_at=float(i), is_bot=True,
        ))


async def fake_gar(system_prompt=None, messages=None, model=None,
                   max_tokens=1000, log_meta=None, on_retry=None, **kw):
    meta = log_meta or {}
    mark(f"MEM-start {meta.get('agent_id')}")
    await asyncio.sleep(MEM_LATENCY)
    mark(f"MEM-end   {meta.get('agent_id')}")
    return "updated memory"


async def fake_gwt(system_prompt=None, messages=None, tools=None,
                   tool_executor=None, model=None, max_tokens=1000,
                   max_tool_rounds=5, log_meta=None, on_retry=None,
                   should_continue=None, **kw):
    meta = log_meta or {}
    mark(f"REPLY-start {meta.get('agent_id')}")
    await asyncio.sleep(TOOL_LATENCY)
    mark(f"REPLY-end   {meta.get('agent_id')}")
    return "<slack_message>ok</slack_message>"


def build_engine():
    agents, clients = [], {}
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    agents.append(hub); clients["blackbird"] = FakeSlackClient(agent_id="blackbird")
    for i in range(N_CLOSE):
        aid = f"wang{i}"
        agents.append(Agent(aid, f"Wang{i}Bot", f"W{i}", role="pi_lab"))
        clients[aid] = FakeSlackClient(agent_id=aid)
    for aid in ("oa1", "oa2", "ob1", "ob2"):
        agents.append(Agent(aid, f"{aid.capitalize()}Bot", aid, role="pi_lab"))
        clients[aid] = FakeSlackClient(agent_id=aid)
    eng = SimulationEngine(agents=agents, slack_clients=clients)
    maxm = get_settings().max_thread_messages
    for i in range(N_CLOSE):
        tid = f"close-t{i}"
        eng.agents["blackbird"].state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id=f"wang{i}",
            has_pending_reply=True)
        eng.agents[f"wang{i}"].state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id="blackbird")
        seed(eng, tid, "general", maxm, "blackbird", f"wang{i}",
             "BlackbirdBot", f"Wang{i}Bot")
    for a, b in (("oa1", "oa2"), ("ob1", "ob2")):
        tid = f"ord-{a}"
        eng.agents[a].state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id=b,
            has_pending_reply=True)
        seed(eng, tid, "general", 4, a, b, f"{a}Bot", f"{b}Bot")
    return eng


async def check_a_convoy_gone():
    EVENTS.clear()
    sim.generate_agent_response = fake_gar
    sim.generate_with_tools = fake_gwt
    eng = build_engine()
    eng._running = True
    T0[0] = time.monotonic()
    await eng._dispatch_reply_lane()
    dispatch_wall = time.monotonic() - T0[0]
    mem_in_dispatch = [t for t, g in EVENTS if g.startswith("MEM")]
    reply_starts = [t for t, g in EVENTS if g.startswith("REPLY-start")]
    assert not mem_in_dispatch, "memory LLM calls still run inside dispatch!"
    assert len(eng._pending_memory_events) == 2 * N_CLOSE
    drained = await eng._drain_memory_events()
    assert drained == 2 * N_CLOSE
    print(f"A. PASS convoy gone: dispatch wall {dispatch_wall:.3f}s "
          f"(was 8.0s), first ordinary reply at "
          f"{min(reply_starts):.3f}s (was 6.0s), 0 memory calls in dispatch, "
          f"{drained} drained after")


async def check_b_lost_update():
    hub = Agent("blackbird", "BlackbirdBot", "B", role="scout_hub")
    l1 = Agent("wang1", "Wang1Bot", "W1", role="pi_lab")
    l2 = Agent("wang2", "Wang2Bot", "W2", role="pi_lab")
    tab = ThreadState(thread_id="tAB", channel="general", other_agent_id="wang1")
    tcd = ThreadState(thread_id="tCD", channel="general", other_agent_id="wang2")
    hub.state.active_threads.update({"tAB": tab, "tCD": tcd})
    eng = SimulationEngine(
        agents=[hub, l1, l2],
        slack_clients={a.agent_id: FakeSlackClient(agent_id=a.agent_id)
                       for a in (hub, l1, l2)})

    async def fake(system_prompt=None, messages=None, **kw):
        content = messages[0]["content"]
        em = "The event that triggered this update:\n"
        mm = "Your current working memory:\n"
        es = content.index(em) + len(em)
        event = content[es:content.index("\n\n", es)]
        ms = content.index(mm) + len(mm)
        prior = content[ms:content.index("\n\nWrite the complete", ms)]
        await asyncio.sleep(0.05)
        return event if prior == "(empty)" else f"{prior} || {event}"

    sim.generate_agent_response = fake
    import tempfile
    import src.agent.agent as agent_mod
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        old = agent_mod.PROFILES_DIR
        agent_mod.PROFILES_DIR = Path(td)
        try:
            await asyncio.gather(
                eng._close_thread(hub, tab, "no_proposal"),
                eng._close_thread(hub, tcd, "no_proposal"))
            await asyncio.gather(
                eng._drain_memory_events(), eng._drain_memory_events())
            mem = hub.working_memory
            assert "wang1" in mem and "wang2" in mem, f"LOST UPDATE: {mem!r}"
        finally:
            agent_mod.PROFILES_DIR = old
    print("B. PASS lost-update invariant survives (2 closes, 2 concurrent "
          "drains, both events in memory)")


async def check_c_stop_bound():
    hub = Agent("blackbird", "BlackbirdBot", "B", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")})
    calls = []

    async def fake(system_prompt=None, messages=None, **kw):
        calls.append(1)
        return "m"
    sim.generate_agent_response = fake
    import tempfile
    from pathlib import Path
    import src.agent.agent as agent_mod
    with tempfile.TemporaryDirectory() as td:
        old = agent_mod.PROFILES_DIR
        agent_mod.PROFILES_DIR = Path(td)
        try:
            for i in range(12):
                eng._pending_memory_events.append(("blackbird", f"e{i}", "public", None))
            await eng.stop()
        finally:
            agent_mod.PROFILES_DIR = old
    assert len(calls) == sim.MEMORY_EVENTS_MAX_AT_SHUTDOWN, len(calls)
    assert not eng._pending_memory_events
    print(f"C. PASS stop() drained exactly {len(calls)}, dropped 2, buffer empty")


async def check_d_lock_registry():
    reg = LockRegistry()
    async with reg.acquire_all("t1"):
        assert len(reg) == 1
    assert len(reg) == 0
    inside = 0; max_inside = 0

    async def worker():
        nonlocal inside, max_inside
        async with reg.acquire_all("k"):
            inside += 1
            max_inside = max(max_inside, inside)
            await asyncio.sleep(0.02)
            inside -= 1
    await asyncio.gather(*(worker() for _ in range(3)))
    assert max_inside == 1, f"mutual exclusion broken: {max_inside}"
    assert len(reg) == 0
    entered = asyncio.Event(); release = asyncio.Event()

    async def holder():
        async with reg.acquire_all("k"):
            entered.set(); await release.wait()

    async def waiter():
        await entered.wait()
        async with reg.acquire_all("k"):
            pass
    h = asyncio.create_task(holder()); w = asyncio.create_task(waiter())
    await entered.wait(); await asyncio.sleep(0.01)
    assert len(reg) == 1, "waiter failed to keep the key alive"
    release.set(); await asyncio.gather(h, w)
    assert len(reg) == 0
    print("D. PASS LockRegistry: eviction at refcount zero, mutual exclusion "
          "holds across eviction, waiters keep keys alive")


async def check_e_prior_cap():
    hub = Agent("blackbird", "BlackbirdBot", "B", role="scout_hub")
    lab = Agent("wang", "WangBot", "W", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={a.agent_id: FakeSlackClient(agent_id=a.agent_id)
                       for a in (hub, lab)})

    async def fake(**kw):
        return "m"
    sim.generate_agent_response = fake
    for i in range(sim.PRIOR_THREADS_KEPT_PER_PAIR + 10):
        t = ThreadState(thread_id=f"t{i}", channel="general", other_agent_id="wang")
        hub.state.active_threads[t.thread_id] = t
        await eng._close_thread(hub, t, "no_proposal", summary_text=f"s{i}")
    pair = tuple(sorted(["blackbird", "wang"]))
    kept = eng._prior_threads[pair]
    assert len(kept) == sim.PRIOR_THREADS_KEPT_PER_PAIR, len(kept)
    assert kept[-1]["summary"] == f"s{sim.PRIOR_THREADS_KEPT_PER_PAIR + 9}"
    print(f"E. PASS _prior_threads capped at {len(kept)} per pair, newest kept")


async def main():
    await check_a_convoy_gone()
    await check_b_lost_update()
    await check_c_stop_bound()
    await check_d_lock_registry()
    await check_e_prior_cap()
    print("\nALL PROTOTYPE CHECKS PASSED")

asyncio.run(main())
