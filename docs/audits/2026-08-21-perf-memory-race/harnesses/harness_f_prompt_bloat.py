"""Harness F — _prior_threads renders 1:1 into every Phase-5 prompt, uncapped.
Real code: SimulationEngine._get_prior_threads_for_agent + Agent.build_phase5_prompt.
"""
import os, sys
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-audit")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pi = Agent("wang", "WangBot", "Wang", role="pi_lab")
hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
eng = SimulationEngine(agents=[pi, hub], slack_clients={
    "wang": FakeSlackClient(agent_id="wang"),
    "blackbird": FakeSlackClient(agent_id="blackbird")})

pair = tuple(sorted(["wang", "blackbird"]))
for n in (10, 100, 500):
    eng._prior_threads[pair] = [
        {"channel": "general", "outcome": "no_proposal",
         "summary": "Screened: declined at gating for FTO; PI to return with in-vivo PK data " * 3}
        for _ in range(n)
    ]
    prior = eng._get_prior_threads_for_agent("wang")
    sysp, msgs = pi.build_phase5_prompt(
        recent_posts=[],
        prior_threads=prior,

    )
    total = len(sysp) + sum(len(m["content"]) for m in msgs)
    print(f"{n:4d} closed threads with this pair -> phase-5 prompt {total:8,d} chars (~{total//4:,} tokens)")
