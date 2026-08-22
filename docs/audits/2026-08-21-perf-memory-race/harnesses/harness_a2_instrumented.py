import asyncio, os, sys, time
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-audit")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

import harness_a_close_convoy as H
import src.agent.simulation as sim

class InstrumentedSem(asyncio.Semaphore):
    def __init__(self, value, events, t0):
        super().__init__(value)
        self.events, self.t0 = events, t0
    async def __aenter__(self):
        task = asyncio.current_task().get_name()
        await self.acquire()
        self.events.append((time.monotonic()-self.t0[0], f"SEM-acquired by {task}"))
    async def __aexit__(self, *a):
        task = asyncio.current_task().get_name()
        self.release()
        self.events.append((time.monotonic()-self.t0[0], f"SEM-released by {task}"))

async def main():
    sim.generate_agent_response = H.fake_generate_agent_response
    sim.generate_with_tools = H.fake_generate_with_tools
    eng = H.build_engine(mem_noop=False)
    eng._reply_sem = InstrumentedSem(4, H.EVENTS, H.T0)
    eng._running = True
    H.T0[0] = time.monotonic()
    await eng._dispatch_reply_lane()
    for t, tag in H.EVENTS:
        print(f"{t:7.3f}s  {tag}")
asyncio.run(main())
