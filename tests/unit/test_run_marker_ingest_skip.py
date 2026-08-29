"""Neither Slack-ingest path may mirror a run-start marker into the log.

The marker has no agent_messages row by design, so without these skips the
resume reconcile re-ingests it (it is absent from _known_slack_ts, seeded from
stored rows only — simulation.py:6479) and the live poller fetches it on the
first tick of the fresh run that posted it (it posts AFTER the cursor seed).
Cursor bookkeeping is part of the contract: a skipped marker that is a
channel's newest message must still advance the cursor, or every later tick
re-fetches it forever.
"""
import pytest

from src.agent.agent import Agent
from src.agent.run_marker import RUN_START_MARKER_PREFIX
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


MARKER_TS = "1700000600.000000"
NORMAL_TS = "1700000500.000000"


def _engine(monkeypatch, tmp_path):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})
    eng._channel_id_map["general"] = "C-GENERAL"
    client.channel_history["C-GENERAL"] = [
        {"ts": NORMAL_TS, "text": "an ordinary bot post",
         "bot_id": "B1", "user": "U1", "username": "OtherBot"},
        {"ts": MARKER_TS,
         "text": f"{RUN_START_MARKER_PREFIX}\nRun: x", "bot_id": "B1",
         "user": "U_blackbird", "username": "BlackbirdBot"},
    ]
    return eng, client


async def test_reconcile_skips_the_marker_and_advances_cursor(monkeypatch, tmp_path):
    eng, client = _engine(monkeypatch, tmp_path)

    await eng._rebuild_state_from_slack()

    assert eng.message_log.get_entry(NORMAL_TS) is not None
    assert eng.message_log.get_entry(MARKER_TS) is None
    assert eng._poll_cursors["C-GENERAL"] == MARKER_TS
    assert MARKER_TS in eng._known_slack_ts


async def test_live_poller_skips_the_marker_and_advances_cursor(monkeypatch, tmp_path):
    eng, client = _engine(monkeypatch, tmp_path)
    eng._poll_cursors["C-GENERAL"] = NORMAL_TS  # marker is the only new message
    eng._last_channel_poll = 0.0

    await eng._poll_slack_for_bot_messages()

    assert eng.message_log.get_entry(MARKER_TS) is None
    assert eng._poll_cursors["C-GENERAL"] == MARKER_TS


async def test_reconcile_never_fetches_replies_under_a_marker(monkeypatch, tmp_path):
    eng, client = _engine(monkeypatch, tmp_path)
    client.channel_history["C-GENERAL"][1]["reply_count"] = 2
    calls = []

    async def _no_replies(*a, **k):
        calls.append(a)
        return []

    monkeypatch.setattr(client, "aget_all_thread_replies", _no_replies)

    await eng._rebuild_state_from_slack()

    assert calls == []  # the skipped root's reply fetch never fired


async def test_ordinary_bot_posts_still_ingest(monkeypatch, tmp_path):
    """Guard against an over-eager predicate: the skip must not eat normal
    traffic (the poller path)."""
    eng, client = _engine(monkeypatch, tmp_path)
    eng._last_channel_poll = 0.0

    await eng._poll_slack_for_bot_messages()

    assert eng.message_log.get_entry(NORMAL_TS) is not None
