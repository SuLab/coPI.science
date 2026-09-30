from src.agent.slack_client import AgentSlackClient
from tests.fakes import RecordingSlackClient, slack_error
from tests.unit.test_slack_client_contract import SequencedWebClient


def _client(fake):
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    c._client = fake
    c._bot_user_id = "U_SU"
    c._channel_name_to_id = {"general": "C_GENERAL"}
    return c


def test_ten_polls_cause_one_join():
    fake = RecordingSlackClient(responses={"conversations_history": {"ok": True, "messages": []}})
    c = _client(fake)
    for _ in range(10):
        c.poll_channel_messages("C_GENERAL", oldest="0")
    assert len(fake.calls_to("conversations_join")) == 1


def test_a_post_after_the_bot_was_removed_rejoins_and_lands():
    fake = SequencedWebClient(
        sequences={"chat_postMessage": [slack_error("not_in_channel"),
                                        {"ok": True, "ts": "1.1", "channel": "C_GENERAL", "message": {}}]},
    )
    c = _client(fake)
    c._joined_channels.add("C_GENERAL")
    out = c.post_message("general", "hello")
    assert out is not None and out["ts"] == "1.1"
    assert len(fake.calls_to("conversations_join")) == 1
    assert len(fake.calls_to("chat_postMessage")) == 2


def test_a_poll_rejected_not_in_channel_rejoins_on_the_next_poll():
    fake = SequencedWebClient(
        sequences={"conversations_history": [slack_error("not_in_channel"),
                                             {"ok": True, "messages": []}]},
    )
    c = _client(fake)
    c.poll_channel_messages("C_GENERAL", oldest="0")
    c.poll_channel_messages("C_GENERAL", oldest="0")
    assert len(fake.calls_to("conversations_join")) == 2
