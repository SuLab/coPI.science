"""Harness C — MessageLog linear-scan cost on the event loop, at scale.

Every MessageLog read is an O(n) pass over `_entries` (message_log.py), and
they are SYNCHRONOUS — they run on the event-loop thread. Per main-loop tick,
`_dispatch_reply_lane` runs `_phase3_activate_threads` for EVERY agent (3
gated reads each) plus `_pending_reply_pairs` (1 `has_new_reply_from_other`
scan per active thread). `_entries` is append-only and never pruned
in-process, so a long-lived run scans an ever-growing list every tick.

Real code under test: MessageLog + SimulationEngine._pending_reply_pairs +
SimulationEngine._phase3_activate_threads. 12 agents (the deployed star),
3 active threads each.
"""
import os
import sys
import time
import tracemalloc

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-audit")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from src.agent.agent import Agent  # noqa: E402
from src.agent.message_log import LogEntry  # noqa: E402
from src.agent.simulation import SimulationEngine  # noqa: E402
from src.agent.state import ThreadState  # noqa: E402
from tests.fakes import FakeSlackClient  # noqa: E402

N_AGENTS = 12
THREADS_PER_AGENT = 3


def build(n_entries: int):
    agents = [Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")]
    for i in range(1, N_AGENTS):
        agents.append(Agent(f"pi{i}", f"Pi{i}Bot", f"PI {i}", role="pi_lab"))
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in agents}
    eng = SimulationEngine(agents=agents, slack_clients=clients)

    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for i in range(n_entries):
        sender = agents[i % N_AGENTS].agent_id
        eng.message_log.load_entry(LogEntry(
            ts=f"171{i:010d}.000100", channel="general",
            sender_agent_id=sender, sender_name=f"{sender}Bot",
            content=("A realistic thread reply about target validation and "
                     "translational feasibility, roughly the median length of "
                     "a lab-agent message in production. " * 3),
            thread_ts=f"root-{i % 400}" if i % 5 else None,
            posted_at=float(i), is_bot=True,
        ))
    after = tracemalloc.take_snapshot()
    stats = after.compare_to(before, "filename")
    log_bytes = sum(s.size_diff for s in stats)
    tracemalloc.stop()

    for a in agents:
        a.state.last_seen_cursor = float(n_entries)  # cursor caught up: worst case scan finds nothing new
        for t in range(THREADS_PER_AGENT):
            tid = f"root-{(hash(a.agent_id) + t) % 400}"
            a.state.active_threads[tid] = ThreadState(
                thread_id=tid, channel="general",
                other_agent_id="blackbird" if a.agent_id != "blackbird" else "pi1",
            )
    return eng, log_bytes


def bench(fn, reps=5):
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


for n in (10_000, 50_000, 100_000):
    eng, log_bytes = build(n)

    t_pairs = bench(lambda: eng._pending_reply_pairs())

    def phase3_sweep():
        for a in eng.agents.values():
            eng._phase3_activate_threads(a)
    t_p3 = bench(phase3_sweep)

    def memory_ctx():
        # the filter _update_agent_memory runs over the whole log per close
        a = next(iter(eng.agents.values()))
        return [e for e in eng.message_log._entries
                if e.sender_agent_id == a.agent_id and e.visibility == "public"]
    t_mem = bench(memory_ctx)

    per_tick_ms = (t_pairs + t_p3) * 1000
    print(f"n={n:>7,} entries | log RAM ~{log_bytes/1e6:6.1f} MB | "
          f"_pending_reply_pairs {t_pairs*1000:7.1f} ms | "
          f"phase3 sweep (12 agents) {t_p3*1000:7.1f} ms | "
          f"per-tick total {per_tick_ms:7.1f} ms (sync, on the event loop) | "
          f"memory-update filter {t_mem*1000:6.1f} ms/close")
