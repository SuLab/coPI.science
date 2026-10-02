"""A-02b (spec 2026-10-01 §6.5): the live Slack poller mirrors only messages from our
own agents' bot identities and attributes them by identity, never by the `username` a
message claims. Everything else the poller does is unchanged."""
import logging

import pytest

from src.agent.agent import Agent
from src.agent.run_marker import RUN_START_MARKER_PREFIX
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import AgentSlackClient
from src.agent.transport import NullTransport
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio

CH_NAME = "general"
CH_ID = "C_general"


class _Disconnected(FakeSlackClient):
    @property
    def is_connected(self) -> bool:
        return False


def _engine(monkeypatch, tmp_path, messages, *, wang_client=None):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang")
    hub_client = FakeSlackClient(agent_id="blackbird")
    lab_client = wang_client or FakeSlackClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[hub, lab], slack_clients={"blackbird": hub_client, "wang": lab_client},
    )
    eng._channel_id_map[CH_NAME] = CH_ID
    for client in (hub_client, lab_client):
        client.channel_history[CH_ID] = list(messages)
    eng._last_channel_poll = 0.0
    return eng


def _msg(ts, **fields):
    return {"ts": ts, "text": f"text of {ts}", **fields}


async def test_an_own_agents_message_is_accepted(monkeypatch, tmp_path):
    ts = "1700000100.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, bot_id="B_wang", user="U_wang", username="WangBot"),
    ])
    await eng._poll_slack_for_bot_messages()
    entry = eng.message_log.get_entry(ts)
    assert entry is not None
    assert (entry.sender_agent_id, entry.sender_name, entry.is_bot) == ("wang", "WangBot", True)
    assert eng._poll_cursors[CH_ID] == ts


async def test_a_foreign_bot_is_dropped_with_the_cursor_advanced(monkeypatch, tmp_path, caplog):
    ts = "1700000101.000000"
    eng = _engine(monkeypatch, tmp_path, [
        {"ts": ts, "bot_id": "B_EVIL", "user": "U_EVIL", "username": "EvilBot",
         "text": "SECRET-CONTENT"},
    ])
    caplog.set_level(logging.INFO, logger="src.agent.simulation")
    await eng._poll_slack_for_bot_messages()
    assert eng.message_log.get_entry(ts) is None
    assert eng._poll_cursors[CH_ID] == ts, "the dropped message would be re-fetched every tick"
    lines = [r.getMessage() for r in caplog.records if "unknown Slack identity" in r.getMessage()]
    assert len(lines) == 1, lines
    assert f"#{CH_NAME}" in lines[0] and ts in lines[0]
    assert caplog.records and not any("SECRET-CONTENT" in r.getMessage() for r in caplog.records)
    assert next(r for r in caplog.records if "unknown Slack identity" in r.getMessage()).levelno \
        == logging.INFO


async def test_a_spoofed_username_of_a_real_agent_is_dropped(monkeypatch, tmp_path):
    ts = "1700000102.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, bot_id="B_EVIL", user="U_EVIL", username="BlackbirdBot"),
    ])
    await eng._poll_slack_for_bot_messages()
    assert eng.message_log.get_entry(ts) is None
    assert eng._poll_cursors[CH_ID] == ts


async def test_attribution_comes_from_identity_not_username(monkeypatch, tmp_path):
    ts = "1700000103.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, bot_id="B_wang", user="U_wang", username="BlackbirdBot"),
    ])
    await eng._poll_slack_for_bot_messages()
    entry = eng.message_log.get_entry(ts)
    assert (entry.sender_agent_id, entry.sender_name) == ("wang", "WangBot")


async def test_identity_by_user_id_alone_is_accepted(monkeypatch, tmp_path):
    ts = "1700000104.000000"
    eng = _engine(monkeypatch, tmp_path, [
        _msg(ts, user="U_blackbird", subtype="bot_message", username="Whatever"),
    ])
    await eng._poll_slack_for_bot_messages()
    entry = eng.message_log.get_entry(ts)
    assert (entry.sender_agent_id, entry.sender_name) == ("blackbird", "BlackbirdBot")


async def test_a_disconnected_clients_identity_is_not_trusted(monkeypatch, tmp_path):
    ts = "1700000105.000000"
    eng = _engine(
        monkeypatch, tmp_path, [_msg(ts, bot_id="B_wang", user="U_wang", username="WangBot")],
        wang_client=_Disconnected(agent_id="wang"),
    )
    await eng._poll_slack_for_bot_messages()
    assert eng.message_log.get_entry(ts) is None
    assert eng._poll_cursors[CH_ID] == ts


async def test_the_other_paths_are_unchanged(monkeypatch, tmp_path):
    """Human, foreign, marker and own messages in one poll: only ours is mirrored, the
    cursor ends on the newest, and a second poll re-fetches nothing."""
    human, foreign, own, marker = (f"17000002{i:02d}.000000" for i in range(4))
    eng = _engine(monkeypatch, tmp_path, [
        _msg(human, user="UHUMAN"),
        _msg(foreign, bot_id="B_EVIL", user="U_EVIL", username="WangBot"),
        _msg(own, bot_id="B_blackbird", user="U_blackbird", username="BlackbirdBot"),
        {"ts": marker, "text": f"{RUN_START_MARKER_PREFIX}\nRun: x", "bot_id": "B_blackbird",
         "user": "U_blackbird"},
    ])
    await eng._poll_slack_for_bot_messages()
    assert [e.ts for e in eng.message_log._entries] == [own]
    assert eng._poll_cursors[CH_ID] == marker
    eng._last_channel_poll = 0.0
    await eng._poll_slack_for_bot_messages()
    assert [e.ts for e in eng.message_log._entries] == [own]


def test_transports_expose_their_identity():
    client = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    assert (client.bot_id, client.bot_user_id) == (None, None)
    client._bot_id, client._bot_user_id = "B1", "U1"
    assert (client.bot_id, client.bot_user_id) == ("B1", "U1")
    assert (NullTransport("su").bot_id, NullTransport("su").bot_user_id) == (None, None)
    fake = FakeSlackClient(agent_id="su")
    assert (fake.bot_id, fake.bot_user_id) == ("B_su", "U_su")
