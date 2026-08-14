"""`anthropic.Anthropic` is the SYNCHRONOUS client, so every
`client.messages.create` inside an `async def` blocks the whole event loop.

Consequences measured on the hub, which sits on all 62 spoke edges:

  * `_phase4_reply_threads` builds its coroutines and `asyncio.gather`s them
    under a "Run replies in parallel" comment, but they cannot overlap — the
    first blocking call pins the loop thread until it returns. A production run
    logged `Phase 4: Replying to 37 threads` in a single turn with only ~15
    spokes live.
  * For that whole stretch nothing else in the process runs: no Slack poll, no
    DB persist flush, no roster sync, and — the one that costs data — no
    asyncio SIGTERM handler. `docker stop -t 30` therefore escalates to SIGKILL
    mid-turn and loses the shutdown flush that the runbook treats as the
    guarantee that the DB, not Slack, is the durable store.

The fix is `asyncio.to_thread`, so the call is awaited off the loop thread.
These tests pin the property (loop stays responsive), not the mechanism.
"""

import asyncio
import time

import pytest

from tests.fakes import FakeAnthropic, text_response

# The fake sleeps this long inside messages.create, imitating a real API call.
_BLOCK_S = 0.30
# The ticker runs this often. Plenty of ticks fit inside _BLOCK_S if the loop
# is free; none do if it is pinned.
_TICK_S = 0.01


class _BlockingAnthropic(FakeAnthropic):
    """A fake whose messages.create blocks the calling thread, like the real one."""

    def _next(self, kwargs: dict):
        time.sleep(_BLOCK_S)
        return super()._next(kwargs)


async def _count_ticks_during(coro) -> tuple[int, object]:
    """Run `coro`, counting how many times a co-scheduled ticker gets to run."""
    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(_TICK_S)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # let the ticker reach its first await
    result = await coro
    stop = True
    await t
    return ticks, result


@pytest.mark.asyncio
async def test_generate_with_tools_does_not_pin_the_event_loop(monkeypatch):
    from src.services.llm import generate_with_tools

    fake = _BlockingAnthropic([text_response("done")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    ticks, out = await _count_ticks_during(
        generate_with_tools(
            system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
            tools=[], tool_executor=None, model="m", max_tokens=100,
        )
    )

    assert "done" in out
    # A free loop fits ~30 ticks into 0.30s. A pinned one manages the single
    # tick that happened before the call started.
    assert ticks > 5, (
        f"event loop was blocked for the whole API call (only {ticks} tick(s) ran)"
    )


@pytest.mark.asyncio
async def test_generate_agent_response_does_not_pin_the_event_loop(monkeypatch):
    from src.services.llm import generate_agent_response

    fake = _BlockingAnthropic([text_response("done")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    ticks, out = await _count_ticks_during(
        generate_agent_response(
            system_prompt="sys", messages=[{"role": "user", "content": "hi"}],
            model="m", max_tokens=100,
        )
    )

    assert "done" in out
    assert ticks > 5, (
        f"event loop was blocked for the whole API call (only {ticks} tick(s) ran)"
    )
