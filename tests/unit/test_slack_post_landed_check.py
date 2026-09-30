"""AG-6: chat.postMessage goes through a WebClient with no retry handlers; a
reply or pitch chunk whose post raised a transport error is looked for in
Slack before it is retried once; the post payload itself is unchanged."""
import time
from urllib.error import URLError

import pytest

from src.agent import slack_client as sc
from src.agent.slack_client import AgentSlackClient, markdown_to_mrkdwn, split_for_slack
from tests.fakes import slack_error
from tests.unit.test_slack_client_contract import SequencedWebClient


def _client(fake):
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    c._client = fake
    c._post_client = fake
    c._bot_user_id = "U_SU"
    c._bot_id = "B_SU"
    c._channel_name_to_id = {"general": "C_GENERAL"}
    return c


def _ok(ts, thread_ts=None):
    return {"ok": True, "ts": ts, "channel": "C_GENERAL", "message": {"thread_ts": thread_ts}}


def _hist(*msgs):
    return {"ok": True, "messages": list(msgs), "response_metadata": {}}


def test_the_post_client_has_no_retry_handlers(monkeypatch):
    built = []

    class _WC:
        def __init__(self, **kw):
            built.append(kw)

        def auth_test(self):
            return {"user_id": "U_SU", "user": "su", "bot_id": "B_SU"}

    monkeypatch.setattr(sc, "WebClient", _WC)
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    assert c.connect()
    assert {"token": "xoxb-test"} in built
    assert {"token": "xoxb-test", "retry_handlers": []} in built
    assert c._bot_id == "B_SU"


def test_timeout_after_accept_is_not_reposted():
    fake = SequencedWebClient(
        sequences={"chat_postMessage": [URLError("timed out")],
                   "conversations_history": [_hist({"ts": str(time.time() + 1), "bot_id": "B_SU",
                                                    "text": "<https://x.org|x.org> landed"})]},
    )
    out = _client(fake).post_message("general", "see https://x.org", landed_check=True, known_ts="1.0")
    assert out is not None
    assert len(fake.calls_to("chat_postMessage")) == 1


def test_timeout_before_accept_is_retried_once():
    fake = SequencedWebClient(
        sequences={"chat_postMessage": [URLError("reset"), _ok("1700000002.000100")],
                   "conversations_history": [_hist()]},
    )
    out = _client(fake).post_message("general", "hello", landed_check=True, known_ts="1.0")
    assert out["ts"] == "1700000002.000100"
    assert len(fake.calls_to("chat_postMessage")) == 2


def test_without_landed_check_the_transport_error_propagates_as_today():
    fake = SequencedWebClient(sequences={"chat_postMessage": [URLError("reset")]})
    with pytest.raises(URLError):
        _client(fake).post_message("general", "hello")


def test_chunk_two_transport_failure_records_chunk_one_and_counts_posted():
    """Review Focus 4."""
    text = "a" * 3900 + "\n\n" + "b" * 3900
    assert len(split_for_slack(text)) == 2
    fake = SequencedWebClient(
        sequences={"chat_postMessage": [_ok("1700000003.000100"), URLError("x"), URLError("y")],
                   "conversations_replies": [_hist({"ts": "1700000003.000100", "bot_id": "B_SU"})]},
    )
    out = _client(fake).post_message("general", text, landed_check=True, known_ts="1.0")
    assert out is not None and len(out["posted_messages"]) == 1
    assert len(fake.calls_to("chat_postMessage")) == 3, "chunk 1 once, chunk 2 twice, never chunk 1 again"


def test_a_429_on_a_post_is_retried_as_today(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    fake = SequencedWebClient(
        sequences={"chat_postMessage": [slack_error("ratelimited", retry_after=1), _ok("1700000004.000100")]},
    )
    out = _client(fake).post_message("general", "hello", landed_check=True, known_ts="1.0")
    assert out["ts"] == "1700000004.000100"


def test_the_post_payload_is_byte_identical_with_and_without_the_landed_check():
    text = "## Title\n\n" + ("word " * 1000) + "\n\n**bold** https://x.org"
    payloads = []
    for flag in (False, True):
        fake = SequencedWebClient(responses={"chat_postMessage": _ok("1700000005.000100", "1700000005.000100")})
        _client(fake).post_message("general", text, landed_check=flag, known_ts="1.0")
        payloads.append(fake.calls_to("chat_postMessage"))
    assert payloads[0] == payloads[1]
    assert payloads[0][0]["text"] == markdown_to_mrkdwn(split_for_slack(text)[0])
    assert len(payloads[0]) == len(split_for_slack(text)) > 1


@pytest.mark.asyncio
async def test_reply_posts_run_under_the_thread_lock_and_pitches_in_the_post_lane():
    import inspect

    from src.agent.engine import post_lane, reply_lane
    from src.agent.simulation import SimulationEngine

    reply_src = inspect.getsource(reply_lane)
    assert reply_src.count("landed_check=True") == 1
    assert "_thread_locks.acquire_all(thread.thread_id)" in reply_src
    pitch_src = inspect.getsource(post_lane)
    assert pitch_src.count("landed_check=True") == 1
    loop_src = inspect.getsource(SimulationEngine._run_main_loop)
    assert "self._run_post_turn(agent)" in loop_src, (
        "pitches must stay one at a time: the post lane is awaited serially by the main loop"
    )
