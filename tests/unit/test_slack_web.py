"""Contract for the web-layer Slack boundary.

Everything outside src/agent/slack_client.py goes through src/services/slack_web.py.
Each function left there is one unpaginated call that posts nothing, so these tests
pin what the module still owns: retry on 429 (with a capped Retry-After), terminal
errors that do not retry, and async wrappers that keep the blocking call off the
event loop.
"""
from unittest.mock import MagicMock

from slack_sdk.errors import SlackApiError

from src.services import slack_web


def _resp(data):
    r = MagicMock()
    r.data = data
    r.get = data.get
    r.__getitem__ = lambda _s, k: data[k]
    return r


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


# ---------------------------------------------------------------------------
# The async wrappers. Their callers are FastAPI route handlers or code those call
# (four of the five call sites), and _call sleeps
# synchronously between retries, so calling the sync functions from
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
    """A future call site must not have to choose the blocking variant by accident.

    ``get_user_info`` is the one exception: its only caller, ``_resolve_delegate_names``,
    is sync and is itself run through ``asyncio.to_thread`` by the dashboard route.
    """
    for name in ("lookup_user_by_email", "revoke_token"):
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
