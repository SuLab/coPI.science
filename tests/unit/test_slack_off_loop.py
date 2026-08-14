"""The Slack client is synchronous and its 429 handler sleeps. Called directly
from a coroutine it pins the loop for the whole HTTP request, so one Slack
rate-limit stall freezes the hub and all 62 spokes."""
import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_apost_message_does_not_pin_the_event_loop(monkeypatch):
    from src.agent.slack_client import AgentSlackClient

    client = AgentSlackClient.__new__(AgentSlackClient)

    def _blocking_post(*a, **kw):
        time.sleep(0.30)
        return {"ok": True, "ts": "1.0"}

    monkeypatch.setattr(client, "post_message", _blocking_post, raising=False)

    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    result = await client.apost_message("C1", "hello")
    stop = True
    await t

    assert result["ok"] is True
    assert ticks > 5, f"event loop pinned during the Slack post (only {ticks} tick(s))"


def test_message_log_append_is_documented_loop_only():
    """append()'s dedupe is a check-then-act and _record mutates two structures.
    Loop-safe (no await inside), NOT thread-safe. Pin the docstring so a future
    change to move it into a thread has to confront this."""
    from src.agent.message_log import MessageLog

    doc = (MessageLog.append.__doc__ or "").lower()
    assert "not thread-safe" in doc or "loop-only" in doc
