"""Contract for the web-layer Slack boundary.

Everything outside src/agent/slack_client.py goes through src/services/slack_web.py.
Each function left there is one unpaginated call that posts nothing, so these tests
pin what the module still owns: retry on 429 (with a capped Retry-After) and the
async wrapper that keeps the blocking call off the event loop.
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


# ---------------------------------------------------------------------------
# The async wrapper. Its one call site is the account-deletion teardown, which
# runs on the event loop, and _call sleeps synchronously between retries, so
# calling the sync function from an `async def` would stall every request the
# process is serving.
# ---------------------------------------------------------------------------


async def test_the_async_wrapper_runs_the_blocking_call_off_the_event_loop(monkeypatch):
    """The sync body must execute on a worker thread, not the loop's thread."""
    import threading

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _record(**kw):
        seen["thread"] = threading.get_ident()
        return _resp({"revoked": True})

    client = MagicMock()
    client.auth_revoke.side_effect = _record
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert await slack_web.revoke_token_async("xoxb-test") is True
    assert seen["thread"] != loop_thread, (
        "the blocking Slack call ran on the event loop's own thread — one 429 "
        "would freeze every other request in the process"
    )


async def test_every_sync_entry_point_has_an_async_wrapper():
    """A future call site must not have to choose the blocking variant by accident."""
    for name in ("revoke_token",):
        assert hasattr(slack_web, f"{name}_async"), f"missing {name}_async"
        assert f"{name}_async" in slack_web.__all__


def test_the_email_lookups_are_gone():
    """Connect Slack (their only callers) was removed (D16)."""
    for name in ("lookup_user_by_email", "lookup_user_by_email_async", "get_user_info"):
        assert not hasattr(slack_web, name), name


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
    client.auth_revoke.side_effect = [err, _resp({"revoked": True})]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    assert slack_web.revoke_token("xoxb-test") is True
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
    client.auth_revoke.side_effect = [err, _resp({"revoked": True})]
    monkeypatch.setattr(slack_web, "_client", lambda _t: client)

    slack_web.revoke_token("xoxb-test")
    assert slept == [7.0]
