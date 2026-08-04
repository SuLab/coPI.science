"""Contract for the web-layer Slack boundary.

Everything outside src/agent/slack_client.py goes through src/services/slack_web.py.
These tests pin the three properties the eight ex-call-sites were each missing:
full pagination, retry on 429, and splitting at 4000 characters.
"""
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from src.services import slack_web


def _resp(data):
    r = MagicMock()
    r.data = data
    r.get = data.get
    r.__getitem__ = lambda _s, k: data[k]
    return r


def test_list_channel_ids_follows_every_cursor(monkeypatch):
    pages = [
        {"channels": [{"name": "a", "id": "C1"}],
         "response_metadata": {"next_cursor": "p2"}},
        {"channels": [{"name": "b", "id": "C2"}],
         "response_metadata": {"next_cursor": ""}},
    ]
    calls = []

    client = MagicMock()
    client.conversations_list.side_effect = lambda **kw: (
        calls.append(kw), _resp(pages[len(calls) - 1]))[1]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.list_channel_ids("xoxb-test") == {"a": "C1", "b": "C2"}
    assert len(calls) == 2, "a single page is the defect this replaces"
    assert calls[1]["cursor"] == "p2"


def test_lookup_user_by_email_retries_a_rate_limit(monkeypatch):
    err = SlackApiError("ratelimited", _resp({"error": "ratelimited"}))
    err.response.headers = {"Retry-After": "0"}
    client = MagicMock()
    client.users_lookupByEmail.side_effect = [
        err, _resp({"user": {"id": "U9"}}),
    ]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.lookup_user_by_email("xoxb-test", "a@b.org") == "U9"
    assert client.users_lookupByEmail.call_count == 2


def test_lookup_user_by_email_returns_none_when_not_found(monkeypatch):
    err = SlackApiError("users_not_found", _resp({"error": "users_not_found"}))
    client = MagicMock()
    client.users_lookupByEmail.side_effect = err
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.lookup_user_by_email("xoxb-test", "nobody@b.org") is None


def test_get_user_info_returns_none_without_retrying(monkeypatch):
    # users.info says `user_not_found`, users.lookupByEmail says `users_not_found`.
    # Both are terminal: retrying costs the caller 3.5s of backoff in a synchronous
    # request path to re-learn that a user who does not exist still does not.
    err = SlackApiError("user_not_found", _resp({"error": "user_not_found"}))
    client = MagicMock()
    client.users_info.side_effect = err
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.get_user_info("xoxb-test", "U404") is None
    assert client.users_info.call_count == 1


def test_post_message_splits_over_the_limit(monkeypatch):
    client = MagicMock()
    client.chat_postMessage.side_effect = lambda **kw: _resp({"ts": "1.0", "ok": True})
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    posted = slack_web.post_message("xoxb-test", "#general", "x" * 9000)

    assert client.chat_postMessage.call_count >= 3
    for call in client.chat_postMessage.call_args_list:
        assert len(call.kwargs["text"]) <= 4000
    assert len(posted) == client.chat_postMessage.call_count


def test_post_message_leaves_a_short_body_in_one_call(monkeypatch):
    client = MagicMock()
    client.chat_postMessage.side_effect = lambda **kw: _resp({"ts": "1.0", "ok": True})
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert len(slack_web.post_message("xoxb-test", "#general", "short")) == 1
    assert client.chat_postMessage.call_count == 1


def test_list_channel_ids_raises_rather_than_returning_a_subset(monkeypatch):
    # An unrecognised error is retried, so page two has to fail on *every* attempt.
    # A single error entry would exhaust side_effect and surface as StopIteration
    # instead of the SlackListingIncomplete this test is about.
    fatal = SlackApiError("fatal", _resp({"error": "fatal"}))
    client = MagicMock()
    client.conversations_list.side_effect = [
        _resp({"channels": [{"name": "a", "id": "C1"}],
               "response_metadata": {"next_cursor": "p2"}}),
        *[fatal] * slack_web._MAX_ATTEMPTS,
    ]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)
    # Retry delays are real sleeps; zero the base so the test costs nothing.
    monkeypatch.setattr(slack_web, "_BACKOFF_BASE", 0)

    with pytest.raises(slack_web.SlackListingIncomplete) as exc:
        slack_web.list_channel_ids("xoxb-test")
    assert exc.value.partial == {"a": "C1"}


def test_post_message_threads_the_reply_when_thread_ts_is_given(monkeypatch):
    """The two legacy PI-guidance callers post into a proposal thread.

    Without thread_ts they could not use this boundary at all: guidance posted
    without one lands in the channel root instead of the thread, which is worse
    than the raw client they used before.
    """
    client = MagicMock()
    client.chat_postMessage.side_effect = lambda **kw: _resp({"ts": "9.0", "ok": True})
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    posted = slack_web.post_message(
        "xoxb-test", "C123", "guidance", thread_ts="1700000000.000100")

    assert client.chat_postMessage.call_count == 1
    assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "1700000000.000100"
    assert posted[0]["thread_ts"] == "1700000000.000100"


def test_post_message_omits_thread_ts_entirely_when_not_threading(monkeypatch):
    """A top-level post must be byte-identical to before thread_ts existed.

    Sending thread_ts=None would be a different Slack payload, so the key is
    dropped rather than passed as None.
    """
    client = MagicMock()
    client.chat_postMessage.side_effect = lambda **kw: _resp({"ts": "9.0", "ok": True})
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    slack_web.post_message("xoxb-test", "#general", "top level")

    assert "thread_ts" not in client.chat_postMessage.call_args.kwargs


# ---------------------------------------------------------------------------
# The async wrappers. Six of the seven call sites are FastAPI route handlers, and
# _call sleeps synchronously between retries, so calling the sync functions from
# an `async def` stalls the event loop for every request the process is serving —
# strictly worse than the raw WebClient they replaced, which had no retry at all.
# ---------------------------------------------------------------------------


async def test_the_async_wrapper_runs_the_blocking_call_off_the_event_loop(monkeypatch):
    """The sync body must execute on a worker thread, not the loop's thread."""
    import threading

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _record(**kw):
        seen["thread"] = threading.get_ident()
        return _resp({"user": {"id": "U1"}})

    client = MagicMock()
    client.users_lookupByEmail.side_effect = _record
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert await slack_web.lookup_user_by_email_async("xoxb-test", "a@b.org") == "U1"
    assert seen["thread"] != loop_thread, (
        "the blocking Slack call ran on the event loop's own thread — one 429 "
        "would freeze every other request in the process"
    )


async def test_every_sync_entry_point_has_an_async_wrapper():
    """A future call site must not have to choose the blocking variant by accident."""
    for name in ("list_channel_ids", "lookup_user_by_email", "get_user_info",
                 "join_channel", "post_message"):
        assert hasattr(slack_web, f"{name}_async"), f"missing {name}_async"
        assert f"{name}_async" in slack_web.__all__


def test_an_outsized_retry_after_is_capped(monkeypatch):
    """Slack can ask for a minute. Three of those would hold a request for minutes.

    The cap bounds request latency; it is only safe to sleep at all because the
    async callers reach this through the _async wrappers.
    """
    slept: list[float] = []
    monkeypatch.setattr(slack_web.time, "sleep", lambda d: slept.append(d))

    err = SlackApiError("ratelimited", _resp({"error": "ratelimited"}))
    err.response.headers = {"Retry-After": "600"}
    client = MagicMock()
    client.users_lookupByEmail.side_effect = [err, _resp({"user": {"id": "U2"}})]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.lookup_user_by_email("xoxb-test", "a@b.org") == "U2"
    assert slept == [slack_web._MAX_RETRY_AFTER], (
        f"slept {slept} instead of capping at {slack_web._MAX_RETRY_AFTER}s"
    )


def test_a_modest_retry_after_is_honoured_exactly(monkeypatch):
    """Under the cap, obey Slack — guessing is how a throttled bot gets blocked."""
    slept: list[float] = []
    monkeypatch.setattr(slack_web.time, "sleep", lambda d: slept.append(d))

    err = SlackApiError("ratelimited", _resp({"error": "ratelimited"}))
    err.response.headers = {"Retry-After": "7"}
    client = MagicMock()
    client.users_lookupByEmail.side_effect = [err, _resp({"user": {"id": "U3"}})]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    slack_web.lookup_user_by_email("xoxb-test", "a@b.org")
    assert slept == [7.0]
