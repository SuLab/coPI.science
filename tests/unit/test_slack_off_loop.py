"""The Slack client is synchronous and its 429 handler sleeps. Called directly
from a coroutine it pins the loop for the whole HTTP request, so one Slack
rate-limit stall freezes the hub and all 62 spokes."""
import asyncio
import threading
import time

import pytest

from tests.fakes import _SlackResponse


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


# ---------------------------------------------------------------------------
# Cache-lock races. The engine runs post_message/poll_channel_messages/etc
# through asyncio.to_thread, which is what makes the test below possible at
# all: before that, "concurrent" asyncio callers of a synchronous method never
# actually overlapped in execution, so AgentSlackClient._channel_name_to_id
# was a check-then-act dict that happened to be safe by accident. The reply
# lane's bounded concurrency (up to reply_lane_max_in_flight at once,
# simulation.py's _dispatch_reply_lane) can now run several of one agent's
# posts on different OS threads at once, so a cache miss on the same
# not-yet-cached channel from two threads is a real race.
#
# The test drives several asyncio.to_thread callers at once against a fake
# WebClient whose lookup sleeps briefly (widening the race window well past
# anything the GIL would close on its own) and asserts the underlying fetch
# happens exactly once. That is the invariant the lock actually buys here:
# _cache_lock is held across the whole check-then-refresh-then-return, so a
# second caller blocked on the lock finds the first caller's result already
# cached and never reaches Slack at all. It is also the invariant that
# actually discriminates — see the note at the end of this file on why a
# "does the dict ever look torn" test would not.
# ---------------------------------------------------------------------------


class _SlowListingClient:
    """Fake slack_sdk WebClient. conversations_list sleeps, widening the race
    window, and counts how many times it was really called."""

    def __init__(self, channels=None, delay: float = 0.05):
        self._channels = channels or [{"name": "general", "id": "C_GENERAL"}]
        self._delay = delay
        self.list_calls = 0
        self._lock = threading.Lock()

    def conversations_list(self, **kwargs):
        with self._lock:
            self.list_calls += 1
        time.sleep(self._delay)
        return _SlackResponse({"channels": self._channels, "response_metadata": {}})


def _fake_client(webclient):
    from src.agent.slack_client import AgentSlackClient

    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    c._client = webclient  # the seam connect() would normally fill
    return c


@pytest.mark.asyncio
async def test_concurrent_channel_lookups_on_a_cache_miss_fetch_only_once():
    """Five callers race get_channel_id on the same not-yet-cached name.

    Without _cache_lock held across the whole check-then-refresh, all five
    would see the miss before any of them finished refreshing and each would
    trigger its own conversations.list. With it, only one really does.
    """
    web = _SlowListingClient()
    client = _fake_client(web)

    results = await asyncio.gather(
        *(asyncio.to_thread(client.get_channel_id, "general") for _ in range(5))
    )

    assert results == ["C_GENERAL"] * 5
    assert web.list_calls == 1, (
        f"conversations_list was called {web.list_calls} times for 5 concurrent "
        "misses on the same channel name — the lock did not dedupe the refresh"
    )


# A "does the dict ever look torn" test was considered instead (racing several
# large concurrent dict.update()/dict[key]=value calls against a reader
# polling len()) and deliberately NOT written: an empirical check
# (200 rounds x 5000-key concurrent dict.update(), and 400000 concurrent
# dict[key]=value ops) found zero lost entries with NO lock at all — CPython's
# GIL already makes a single dict.update()/__setitem__ call with plain string
# keys atomic in practice, since the C loop backing it never hits a bytecode-
# level GIL-release checkpoint. A test asserting "no torn state" on the dict
# itself would pass identically whether or not _cache_lock exists, so it
# would prove nothing. The dedupe test above is the invariant _cache_lock actually changes.
