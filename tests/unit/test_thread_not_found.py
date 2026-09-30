"""Tests for the ThreadNotFound eviction path.

Covers the failure modes we saw during the grantbot-duplicate incident:
1. chat.postMessage silently dropping thread_ts when the parent is deleted —
   surfaces as ThreadNotFound (and the orphan top-level post is cleaned up).
2. _evict_dead_thread purges the dead ts from every agent's state so the
   scheduler doesn't keep re-polling or re-replying to the grave.
"""

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import AgentSlackClient, ThreadNotFound
from src.agent.state import ThreadState


def _slack_error(error_code: str) -> SlackApiError:
    """Build a SlackApiError whose response looks like Slack's."""
    resp = MagicMock()
    resp.get = lambda key, default=None: {"error": error_code, "ok": False}.get(key, default)
    resp.headers = {}
    return SlackApiError(message=error_code, response=resp)


@pytest.fixture
def client():
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-real-token")
    c._client = MagicMock()
    return c


class TestPostMessageSilentOrphanDetection:
    def test_silent_thread_drop_raises_and_deletes_orphan(self, client):
        # Slack returns a post dict WITHOUT a nested thread_ts in message —
        # meaning the thread_ts we sent was silently dropped because the
        # parent was deleted. Client should delete the orphan and raise.
        client._client.chat_postMessage.return_value = MagicMock(
            data={"ok": True, "ts": "1777000999.123456", "message": {"ts": "1777000999.123456"}}
        )
        client._client.chat_delete.return_value = {"ok": True}

        with pytest.raises(ThreadNotFound) as exc_info:
            client.post_message("C123", "Reply to deleted parent", thread_ts="1777000000.000100")

        assert exc_info.value.thread_ts == "1777000000.000100"
        # Orphan cleanup happened
        client._client.chat_delete.assert_called_once()
        assert client._client.chat_delete.call_args.kwargs["ts"] == "1777000999.123456"

    def test_normal_thread_reply_passes_through(self, client):
        # Parent still exists; Slack echoes back the same thread_ts in message.
        client._client.chat_postMessage.return_value = MagicMock(
            data={
                "ok": True,
                "ts": "1777000999.123456",
                "message": {"ts": "1777000999.123456", "thread_ts": "1777000000.000100"},
            }
        )
        result = client.post_message("C123", "Normal reply", thread_ts="1777000000.000100")
        assert result["ts"] == "1777000999.123456"
        client._client.chat_delete.assert_not_called()

    def test_top_level_post_no_thread_ts_passes_through(self, client):
        # No thread_ts passed; the orphan detection shouldn't fire.
        client._client.chat_postMessage.return_value = MagicMock(
            data={"ok": True, "ts": "1777000999.123456", "message": {"ts": "1777000999.123456"}}
        )
        result = client.post_message("C123", "Top-level post")
        assert result["ts"] == "1777000999.123456"
        client._client.chat_delete.assert_not_called()

    def test_thread_not_found_from_api_also_raises(self, client):
        # If Slack *does* return thread_not_found directly (some API paths do),
        # we still surface it as ThreadNotFound.
        client._client.chat_postMessage.side_effect = _slack_error("thread_not_found")
        with pytest.raises(ThreadNotFound):
            client.post_message("C123", "Reply", thread_ts="1.0")


class TestEvictDeadThread:
    @pytest.fixture
    def engine_with_agents(self):
        # Two agents, both with the dead thread in various state containers.
        dead_ts = "1776900000.000100"
        a = Agent(agent_id="su", pi_name="Su", bot_name="SuBot")
        b = Agent(agent_id="wu", pi_name="Wu", bot_name="WuBot")

        # Populate per-agent state that should be cleaned
        for ag in (a, b):
            ag.state.active_threads[dead_ts] = ThreadState(
                thread_id=dead_ts, channel="single-cell-omics", other_agent_id="other",
            )

        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine._closed_thread_ids.add(dead_ts)
        return engine, dead_ts, a, b

    @pytest.mark.asyncio
    async def test_evicts_from_all_agents(self, engine_with_agents):
        engine, dead_ts, a, b = engine_with_agents
        await engine._evict_dead_thread(dead_ts)

        for ag in (a, b):
            assert dead_ts not in ag.state.active_threads

        # Eviction is additive: it removes per-agent state but never un-closes a thread
        assert dead_ts in engine._closed_thread_ids

    @pytest.mark.asyncio
    async def test_unknown_thread_id_is_noop(self, engine_with_agents):
        engine, _, a, b = engine_with_agents
        # Unknown ts — must not raise, must not touch the dead_ts data
        await engine._evict_dead_thread("9999999999.999999")
        for ag in (a, b):
            assert len(ag.state.active_threads) == 1
