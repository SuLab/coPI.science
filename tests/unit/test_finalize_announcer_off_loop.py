"""The Finalize announcer's client lookups block (auth.test, and
conversations.list, which sleeps on a 429), so they run off the supervisor's
event loop."""
import threading
from types import SimpleNamespace

import pytest

from src.agent.engine.headlines import HeadlineAnnouncer, HeadlineInputs


@pytest.mark.asyncio
async def test_client_and_channel_lookups_run_in_a_worker_thread():
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    client = SimpleNamespace(is_connected=False)

    def client_for(agent_id):
        seen["client_for"] = threading.get_ident()
        return client

    def summary_channel_id(c):
        seen["summary_channel_id"] = threading.get_ident()
        return "C-SUMMARY"

    announcer = HeadlineAnnouncer(
        session_factory=None, run_id=None, client_for=client_for,
        summary_channel_id=summary_channel_id, source_channel_id=lambda name: None,
        pi_label_for=lambda subject: "Lab",
    )
    outcome = await announcer.announce(
        HeadlineInputs(
            agent_id="blackbird", thread_id="t1", channel_name="c", subject_agent_id=None,
            slack_ts=None, project=None, recommendation=None, scores={}, elevator_pitch=None,
        ),
        trigger="finalize",
    )
    assert outcome == "lost"  # not connected: nothing claimed or posted
    assert set(seen) == {"client_for", "summary_channel_id"}
    assert loop_thread not in seen.values()
