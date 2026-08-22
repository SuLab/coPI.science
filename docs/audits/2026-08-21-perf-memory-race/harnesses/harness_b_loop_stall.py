"""Harness B — event-loop freeze from SYNC Slack calls made in async context.

`_poll_slack_for_bot_messages` awaits `apoll_channel_messages` (correctly
off-loop via to_thread) but then calls `client.is_bot_user(user_id)` DIRECTLY
(simulation.py:4196) for every message whose author isn't identifiable as a
bot — a synchronous `users.info` HTTP round trip on the event-loop thread,
routed through `_call_with_retry`, which on a Slack 429 does a synchronous
`time.sleep(Retry-After)` ON THE LOOP.

Uses the REAL SimulationEngine poller and the REAL AgentSlackClient retry
path; only the innermost `WebClient` is replaced by a stub with a controlled
latency. A heartbeat task measures how long the event loop goes unresponsive.

Case 1: 5 human channel messages, users.info RTT = 0.3s each.
Case 2: one users.info that gets rate-limited (Retry-After: 2, MAX_RETRIES=3).
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key-for-audit")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))

from slack_sdk.errors import SlackApiError  # noqa: E402

from src.agent.agent import Agent  # noqa: E402
from src.agent.simulation import SimulationEngine  # noqa: E402
from src.agent.slack_client import AgentSlackClient  # noqa: E402


class FakeResponse:
    """Duck-typed slack_sdk response for the ratelimited branch."""
    def __init__(self, error, headers=None):
        self._error = error
        self.headers = headers or {}
    def get(self, key, default=None):
        return {"error": self._error}.get(key, default)


class StubWebClient:
    """Innermost slack_sdk.WebClient stand-in with controlled latency."""
    def __init__(self, human_msgs, users_info_latency=0.0, ratelimit_users_info=False):
        self._msgs = human_msgs
        self._latency = users_info_latency
        self._ratelimit = ratelimit_users_info
        self.users_info_calls = 0

    def conversations_history(self, **kw):
        return {"messages": self._msgs, "has_more": False,
                "response_metadata": {}}

    def users_info(self, **kw):
        self.users_info_calls += 1
        if self._ratelimit:
            raise SlackApiError(
                "ratelimited",
                response=FakeResponse("ratelimited", {"Retry-After": "2"}),
            )
        time.sleep(self._latency)  # what a real sync HTTP call does to the loop
        return {"user": {"is_bot": False}}


def build(engine_msgs, **stub_kw):
    a = Agent("su", "SuBot", "Su", role="pi_lab")
    client = AgentSlackClient(agent_id="su", bot_token="xoxb-real-looking")
    stub = StubWebClient(engine_msgs, **stub_kw)
    client._client = stub          # bypass connect(); real _api/_call_with_retry stay
    client._bot_user_id = "UBOT"
    eng = SimulationEngine(agents=[a], slack_clients={"su": client})
    eng._channel_id_map = {"general": "C1"}
    eng._last_channel_poll = 0.0
    return eng, stub


async def measure(engine):
    gaps = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gap = now - last
            if gap > 0.20:
                gaps.append(gap)
            last = now

    hb = asyncio.create_task(heartbeat())
    t0 = time.monotonic()
    await engine._poll_slack_for_bot_messages()
    total = time.monotonic() - t0
    stop.set()
    await hb
    return total, gaps


async def main():
    human = [
        {"ts": f"1000.00000{i}", "user": f"UHUMAN{i}", "text": f"hi {i}"}
        for i in range(5)
    ]

    eng, stub = build(human, users_info_latency=0.3)
    total, gaps = await measure(eng)
    print("=== Case 1: 5 human messages, users.info RTT 0.3s ===")
    print(f"  poll wall time: {total:.2f}s, users_info calls: {stub.users_info_calls}")
    print(f"  event-loop stalls >200ms: {[f'{g:.2f}s' for g in gaps]}")
    print(f"  max single stall: {max(gaps) if gaps else 0:.2f}s "
          f"(loop frozen for the sum of all sync users.info calls in the batch)")

    eng2, stub2 = build(human[:1], ratelimit_users_info=True)
    total2, gaps2 = await measure(eng2)
    print("\n=== Case 2: ONE rate-limited users.info (Retry-After: 2s, 3 attempts) ===")
    print(f"  poll wall time: {total2:.2f}s, users_info calls: {stub2.users_info_calls}")
    print(f"  event-loop stalls >200ms: {[f'{g:.2f}s' for g in gaps2]}")
    print("  -> time.sleep(Retry-After) inside _call_with_retry runs ON the loop:")
    print("     every in-flight reply, poller, flush and the SIGTERM handler are frozen.")


asyncio.run(main())
