"""Tests for the ThreadNotFound eviction path.

Covers the three failure modes we saw during the grantbot-duplicate incident:
1. conversations.replies returning thread_not_found — surfaces as ThreadNotFound.
2. chat.postMessage silently dropping thread_ts when the parent is deleted —
   surfaces as ThreadNotFound (and the orphan top-level post is cleaned up).
3. _evict_dead_thread purges the dead ts from every agent's state so the
   scheduler doesn't keep re-polling or re-replying to the grave.
"""

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import AgentSlackClient, ThreadNotFound
from src.agent.state import PostRef, ProposalRef, ThreadState


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


class TestGetThreadRepliesRaisesThreadNotFound:
    def test_thread_not_found_raises(self, client):
        client._client.conversations_replies.side_effect = _slack_error("thread_not_found")
        with pytest.raises(ThreadNotFound) as exc_info:
            client.get_thread_replies("C123", "1777000000.000100")
        assert exc_info.value.thread_ts == "1777000000.000100"
        assert exc_info.value.channel_id == "C123"

    def test_other_errors_return_empty_not_raise(self, client):
        # Non-thread-related errors shouldn't raise ThreadNotFound — they
        # should fall through to the original "log and return []" behavior.
        client._client.conversations_replies.side_effect = _slack_error("rate_limited")
        assert client.get_thread_replies("C123", "1.0") == []


class TestGetAllThreadRepliesRaisesThreadNotFound:
    def test_thread_not_found_raises(self, client):
        client._client.conversations_replies.side_effect = _slack_error("thread_not_found")
        with pytest.raises(ThreadNotFound):
            client.get_all_thread_replies("C123", "1.0")


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
            ag.state.interesting_posts.append(PostRef(
                post_id=dead_ts, channel="single-cell-omics",
                sender_agent_id="grantbot", content_snippet="dead", posted_at=0.0,
            ))
            ag.state.pending_proposals.append(ProposalRef(
                thread_id=dead_ts, channel="single-cell-omics",
                other_agent_id="other", summary_text="x", proposed_at=0.0,
            ))

        engine = SimulationEngine(agents=[a, b], slack_clients={})
        engine._poll_cursors[f"proposal_thread:{dead_ts}"] = "1.0"
        engine._closed_thread_ids.add(dead_ts)
        return engine, dead_ts, a, b

    def test_evicts_from_all_agents(self, engine_with_agents):
        engine, dead_ts, a, b = engine_with_agents
        engine._evict_dead_thread(dead_ts)

        for ag in (a, b):
            assert dead_ts not in ag.state.active_threads
            assert not any(p.post_id == dead_ts for p in ag.state.interesting_posts)
            assert not any(p.thread_id == dead_ts for p in ag.state.pending_proposals)

        assert f"proposal_thread:{dead_ts}" not in engine._poll_cursors
        assert dead_ts not in engine._closed_thread_ids

    def test_unknown_thread_id_is_noop(self, engine_with_agents):
        engine, _, a, b = engine_with_agents
        # Unknown ts — must not raise, must not touch the dead_ts data
        engine._evict_dead_thread("9999999999.999999")
        for ag in (a, b):
            assert len(ag.state.active_threads) == 1
            assert len(ag.state.interesting_posts) == 1
            assert len(ag.state.pending_proposals) == 1
