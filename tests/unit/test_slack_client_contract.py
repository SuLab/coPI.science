"""What AgentSlackClient actually sends to Slack, and how it handles what comes back.

`test_transport.py` checks protocol conformance and `test_thread_not_found.py` covers one
error path. Neither asserts on the *outbound call* — and that is the gap Rule S2 names:
the whole engine suite runs with NullTransport, so a client that quietly stopped sending
`thread_ts`, or stopped retrying a 429, would look identical from inside our own database.

Every test here asserts on `RecordingSlackClient.calls`, which is evidence the call
happened, not merely that no exception escaped.
"""

import time

import pytest

from src.agent.slack_client import MAX_RETRIES, AgentSlackClient, ThreadNotFound
from tests.fakes import RecordingSlackClient, slack_error


def _client(fake, *, visibility_lookup=None) -> AgentSlackClient:
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test",
                         visibility_lookup=visibility_lookup)
    c._client = fake              # the seam connect() would fill
    c._bot_user_id = "U_SU"
    c._channel_name_to_id = {"general": "C_GENERAL"}
    return c


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The retry path sleeps for Retry-After seconds. Without this the rate-limit
    tests would really wait."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


# --- the retry path ---------------------------------------------------------------


def test_a_rate_limited_call_is_retried_and_then_succeeds():
    fake = RecordingSlackClient(
        responses={"chat_postMessage": {"ok": True, "ts": "1.1", "channel": "C_GENERAL"}},
        errors={"chat_postMessage": [slack_error("ratelimited", retry_after=1)]},
    )
    out = _client(fake).post_message("general", "hello")
    assert out and out["ts"] == "1.1"
    assert len(fake.calls_to("chat_postMessage")) == 2, "the 429 was not retried"


def test_an_unthrottled_call_is_made_exactly_once():
    """Control for the test above. A client that always sent the request twice would
    satisfy the retry assertion on its own."""
    fake = RecordingSlackClient(
        responses={"chat_postMessage": {"ok": True, "ts": "1.2", "channel": "C_GENERAL"}})
    _client(fake).post_message("general", "hello")
    assert len(fake.calls_to("chat_postMessage")) == 1


def test_a_non_rate_limit_error_is_not_retried():
    """Retrying a `channel_not_found` just burns quota — the answer will not change."""
    fake = RecordingSlackClient(
        errors={"chat_postMessage": [slack_error("channel_not_found")] * 5})
    assert _client(fake).post_message("general", "hello") is None
    assert len(fake.calls_to("chat_postMessage")) == 1


def test_retries_are_bounded_and_raise_a_SlackApiError():
    """A permanently rate-limited endpoint must give up, not spin forever — and it must
    give up with the exception type its callers catch.

    Regression: `_call_with_retry` referred to `exc` after the loop, but Python unbinds
    an `except ... as exc` name at the end of the except block. Exhausting the retries
    raised UnboundLocalError instead of SlackApiError, which `post_message`'s
    `except SlackApiError` does not catch — so a sustained 429 crashed the turn rather
    than degrading to "not posted". That is the failure mode you get exactly when Slack
    is throttling you.
    """
    fake = RecordingSlackClient(
        errors={"chat_postMessage": [slack_error("ratelimited", retry_after=1)] * 50})
    # post_message must degrade to None, not propagate anything.
    assert _client(fake).post_message("general", "hello") is None
    assert len(fake.calls_to("chat_postMessage")) == MAX_RETRIES

    # And the raw helper must raise the type callers handle.
    from slack_sdk.errors import SlackApiError
    fake2 = RecordingSlackClient(
        errors={"conversations_history": [slack_error("ratelimited", retry_after=1)] * 50})
    c = _client(fake2)
    with pytest.raises(SlackApiError):
        c._call_with_retry(fake2.conversations_history, channel="C_GENERAL")


def test_retry_after_header_is_honoured(monkeypatch):
    """The sleep must use Slack's Retry-After, not a hardcoded constant — ignoring it
    is how a client gets itself rate-limited for longer."""
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    fake = RecordingSlackClient(
        responses={"chat_postMessage": {"ok": True, "ts": "1.3"}},
        errors={"chat_postMessage": [slack_error("ratelimited", retry_after=17)]},
    )
    _client(fake).post_message("general", "hi")
    assert slept == [17], f"slept {slept}, expected Slack's Retry-After of 17"


# --- what actually goes on the wire ------------------------------------------------


def test_thread_ts_is_omitted_for_a_root_and_sent_for_a_reply():
    """Both halves. Sending `thread_ts=None` explicitly would make every root post a
    malformed reply; omitting it on a real reply silently un-threads the conversation.
    """
    fake = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    _client(fake).post_message("general", "root")
    kw = fake.calls_to("chat_postMessage")[0]
    assert "thread_ts" not in kw, f"a root post carried thread_ts: {kw}"

    fake2 = RecordingSlackClient(responses={"chat_postMessage": {
        "ok": True, "ts": "1.2", "message": {"thread_ts": "1700000000.000100"}}})
    _client(fake2).post_message("general", "reply", thread_ts="1700000000.000100")
    assert fake2.calls_to("chat_postMessage")[0]["thread_ts"] == "1700000000.000100"


def test_a_channel_name_is_resolved_to_an_id_before_posting():
    """Slack accepts names for some endpoints and ids for others; the client normalises
    to an id. A name reaching chat.postMessage works today and breaks on the endpoints
    that do not accept one, so the normalisation is what keeps them consistent."""
    fake = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    _client(fake).post_message("general", "hi")
    assert fake.calls_to("chat_postMessage")[0]["channel"] == "C_GENERAL"
    # Control: an id passes straight through rather than being mangled.
    fake2 = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    _client(fake2).post_message("C_OTHER", "hi")
    assert fake2.calls_to("chat_postMessage")[0]["channel"] == "C_OTHER"


def test_markdown_is_translated_to_slack_mrkdwn_before_sending():
    """The text Slack receives is not the text we composed. Any live assertion that
    compares a posted message to its source string has to know that."""
    fake = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    _client(fake).post_message("general", "a **bold** claim")
    sent = fake.calls_to("chat_postMessage")[0]["text"]
    assert "**bold**" not in sent, f"markdown was sent raw: {sent!r}"
    assert "*bold*" in sent, sent


# --- autojoin, and the private-channel exception to it ------------------------------


def test_autojoin_runs_for_a_public_channel():
    fake = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    _client(fake).post_message("general", "hi")
    assert fake.calls_to("conversations_join") == [{"channel": "C_GENERAL"}]


def test_autojoin_is_skipped_for_a_known_private_channel():
    """A bot cannot self-join a private channel; trying hides an invite-path bug behind
    a swallowed error.

    Control: the same client with the same lookup returning 'public' DOES join, so this
    is about the visibility branch and not about autojoin being dead.
    """
    fake = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    c = _client(fake, visibility_lookup=lambda cid: "collab_private")
    c.post_message("C_PRIV", "hi")
    assert fake.calls_to("conversations_join") == []

    fake2 = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})
    c2 = _client(fake2, visibility_lookup=lambda cid: "public")
    c2.post_message("C_PUB", "hi")
    assert fake2.calls_to("conversations_join") == [{"channel": "C_PUB"}]


def test_a_raising_visibility_lookup_fails_open_to_public():
    """Documented behaviour: a bad lookup must not break Slack calls."""
    fake = RecordingSlackClient(responses={"chat_postMessage": {"ok": True, "ts": "1.1"}})

    def _boom(_cid):
        raise RuntimeError("lookup exploded")

    c = _client(fake, visibility_lookup=_boom)
    assert c.post_message("C_X", "hi") is not None
    assert fake.calls_to("conversations_join") == [{"channel": "C_X"}]


def test_a_failing_autojoin_does_not_stop_the_post():
    """Autojoin is best-effort: an already-a-member bot gets an error here every time."""
    fake = RecordingSlackClient(
        responses={"chat_postMessage": {"ok": True, "ts": "1.1"}},
        errors={"conversations_join": [slack_error("already_in_channel")]},
    )
    assert _client(fake).post_message("general", "hi") is not None
    assert len(fake.calls_to("chat_postMessage")) == 1


# --- the silent orphan: Slack drops thread_ts when the parent is gone ----------------


def test_a_silently_dropped_thread_is_deleted_and_reported():
    """Slack accepts chat.postMessage against a deleted parent, drops the thread_ts,
    and creates a TOP-LEVEL message. Left alone every dead root spawns a cascade of
    pseudo-roots that other agents then treat as fresh posts.

    Three things must happen, and only asserting the exception would miss the worst of
    them — the orphan staying in the channel.
    """
    fake = RecordingSlackClient(responses={"chat_postMessage": {
        "ok": True, "ts": "9.9", "channel": "C_GENERAL",
        "message": {},                       # no thread_ts echoed back
    }})
    with pytest.raises(ThreadNotFound):
        _client(fake).post_message("general", "reply", thread_ts="1.0")
    assert fake.calls_to("chat_delete") == [{"channel": "C_GENERAL", "ts": "9.9"}], (
        "the orphaned top-level post was left in the channel"
    )


def test_a_correctly_threaded_reply_is_not_deleted():
    """Control for the test above: a client that deleted every reply would pass it."""
    fake = RecordingSlackClient(responses={"chat_postMessage": {
        "ok": True, "ts": "9.9", "channel": "C_GENERAL",
        "message": {"thread_ts": "1.0"},
    }})
    out = _client(fake).post_message("general", "reply", thread_ts="1.0")
    assert out and out["ts"] == "9.9"
    assert fake.calls_to("chat_delete") == []


def test_thread_not_found_from_slack_is_raised_not_swallowed():
    fake = RecordingSlackClient(
        errors={"chat_postMessage": [slack_error("thread_not_found")]})
    with pytest.raises(ThreadNotFound):
        _client(fake).post_message("general", "reply", thread_ts="1.0")


def test_thread_not_found_on_a_ROOT_post_is_not_raised():
    """Control: the ThreadNotFound branch is conditional on thread_ts. Without this a
    client that raised on every error would pass the test above."""
    fake = RecordingSlackClient(
        errors={"chat_postMessage": [slack_error("thread_not_found")]})
    assert _client(fake).post_message("general", "root") is None


# --- not connected ------------------------------------------------------------------


def test_an_unconnected_client_returns_none_rather_than_a_fake_ts():
    """The engine mints a unique canonical id when post_message returns None. A
    hardcoded ts here would collide across agents and, under idempotent append, silently
    drop real messages.
    """
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    assert c._client is None
    assert c.post_message("general", "hi") is None
    assert c.is_connected is False


def test_connect_refuses_a_placeholder_token():
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-placeholder-su")
    assert c.connect() is False
    assert c.is_connected is False
    # Control: an empty token is also refused, and neither leaves a half-built client.
    assert AgentSlackClient(agent_id="su", bot_token="").connect() is False
