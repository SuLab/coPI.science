"""What AgentSlackClient actually sends to Slack, and how it handles what comes back.

`test_transport.py` checks protocol conformance and `test_thread_not_found.py` covers one
error path. Neither asserts on the *outbound call* — and that is the gap Rule S2 names:
the whole engine suite runs with NullTransport, so a client that quietly stopped sending
`thread_ts`, or stopped retrying a 429, would look identical from inside our own database.

Every test here asserts on `RecordingSlackClient.calls`, which is evidence the call
happened, not merely that no exception escaped.

The second half of the file covers the chokepoint itself — pagination, the >4000-char
split and inbound `thread_ts` normalisation. Those three were implemented with live-tier
coverage only, which means the 1091-test offline suite could not observe them at all: a
paginator reverted to a single page, or a split reverted to posting blind, passed the
whole offline suite. They are pinned here because that is where a mutation to them has to
die, not on a tier that needs a real workspace and four minutes.
"""

import ast
import time
from pathlib import Path

import pytest

from src.agent import slack_client as slack_client_module
from src.agent.slack_client import (
    MAX_PAGES,
    MAX_RETRIES,
    SLACK_MAX_TEXT_CHARS,
    SLACK_PAGE_LIMIT,
    AgentSlackClient,
    SlackListingIncomplete,
    ThreadNotFound,
    markdown_to_mrkdwn,
    normalize_inbound_message,
    split_for_slack,
)
from tests.fakes import RecordingSlackClient, _SlackResponse, slack_error


def _client(fake, *, visibility_lookup=None) -> AgentSlackClient:
    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test",
                         visibility_lookup=visibility_lookup)
    c._client = fake              # the seam connect() would fill
    c._bot_user_id = "U_SU"
    c._channel_name_to_id = {"general": "C_GENERAL"}
    return c


class SequencedWebClient:
    """A WebClient stand-in that answers a method with a *sequence* of responses.

    ``RecordingSlackClient`` returns the same dict for every call to a method, which
    cannot express pagination at all: a client that followed
    ``response_metadata.next_cursor`` and one that ignored it would both see the same
    single page and look identical. Here each call pops the next scripted item, so
    "page 1, then page 2, then stop" is expressible — and "page 2 fails" is too, by
    scripting an exception in the sequence.

    Methods with no scripted sequence fall back to ``responses`` exactly as
    ``RecordingSlackClient`` does, so the two are interchangeable for everything else.
    """

    def __init__(self, sequences=None, responses=None, errors=None):
        self.calls: list[tuple[str, dict]] = []
        self._seq = {k: list(v) for k, v in (sequences or {}).items()}
        self._responses = dict(responses or {})
        self._errors = {k: list(v) for k, v in (errors or {}).items()}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(**kwargs):
            self.calls.append((name, dict(kwargs)))
            queue = self._errors.get(name)
            if queue:
                raise queue.pop(0)
            seq = self._seq.get(name)
            if seq is not None:
                assert seq, f"{name} was called more times than the test scripted"
                item = seq.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return _SlackResponse(item)
            return _SlackResponse(self._responses.get(name, {"ok": True}))

        return _call

    def calls_to(self, method: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == method]

    def unconsumed(self, method: str) -> int:
        """Scripted responses the client never asked for — i.e. pages it skipped."""
        return len(self._seq.get(method, []))


def _page(key: str, items: list, next_cursor: str = "") -> dict:
    body = {"ok": True, key: items}
    if next_cursor:
        body["response_metadata"] = {"next_cursor": next_cursor}
    return body


def _msg(ts: str, **extra) -> dict:
    return {"ts": ts, "text": f"m{ts}", "user": "U_X", **extra}


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


# ===========================================================================
# The chokepoint, at the source level
# ===========================================================================


def _chokepoint_nodes():
    """(direct attribute accesses on self._client, dynamic getattr lookups)."""
    tree = ast.parse(Path(slack_client_module.__file__).read_text())

    def _is_self_client(node) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "_client"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for sub in ast.walk(fn):
                owner.setdefault(id(sub), fn.name)

    direct = [
        (n.lineno, n.attr) for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and _is_self_client(n.value)
    ]
    dynamic = [
        (n.lineno, owner.get(id(n), "<module>")) for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id == "getattr"
        and n.args and _is_self_client(n.args[0])
    ]
    return direct, dynamic


def test_no_slack_endpoint_is_reached_outside_the_chokepoint():
    """The module docstring's central claim, asserted instead of merely written down.

    Four separate defects — `list_channels` not paginating, `create_channel` skipping
    the retry, the >4000-char split, and `thread_ts == ts` normalised in one ingest path
    but not another — were four instances of one structural absence: each call site
    reached `self._client.<endpoint>(...)` itself and therefore had to remember the
    cross-cutting rules on its own. `_api` takes the endpoint's *name* so there is
    exactly one place that touches the WebClient, and a fifth instance of that class of
    bug requires editing this test first.

    Parsed from the file the loaded module was imported from, not from a path guess, so
    it also fails if the module under test is not the one in the working tree.
    """
    assert Path(slack_client_module.__file__).is_file(), slack_client_module.__file__
    direct, dynamic = _chokepoint_nodes()
    assert direct == [], (
        "these lines call a Slack endpoint directly and so inherit neither the "
        f"rate-limit retry nor pagination: {direct}"
    )
    assert len(dynamic) == 1, (
        f"expected exactly one dynamic endpoint lookup (in _api); found {dynamic}"
    )
    assert dynamic[0][1] == "_api", (
        f"the WebClient is reached from {dynamic[0][1]}, not from _api"
    )


def test_every_cursor_paginated_read_goes_through_paginate():
    """Control for the test above: routing through `_api` is necessary but not
    sufficient. A `_api("conversations_list", ...)` call that skipped `_paginate` would
    satisfy the chokepoint test and still return one page — which is defect 1 exactly.
    """
    tree = ast.parse(Path(slack_client_module.__file__).read_text())
    paginated = {"conversations_list", "conversations_history", "conversations_replies"}
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name in ("_paginate", "_api"):
            continue
        for call in ast.walk(fn):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_api"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value in paginated
            ):
                offenders.append((call.lineno, fn.name, call.args[0].value))
    assert offenders == [], (
        "a cursor-paginated endpoint is called outside _paginate, so it returns one "
        f"page and silently drops the rest: {offenders}"
    )


# ===========================================================================
# Pagination — defect 1
# ===========================================================================


def test_list_channels_follows_next_cursor_to_the_end():
    """The production defect: one 200-item page of a 323-channel workspace, and Slack
    orders conversations.list by channel id, which is not monotonic in creation time —
    so which channels a single page showed was effectively random.
    """
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "a", "id": "C_A"}], next_cursor="cur1"),
        _page("channels", [{"name": "b", "id": "C_B"}], next_cursor="cur2"),
        _page("channels", [{"name": "c", "id": "C_C"}]),
    ]})
    c = _client(fake)
    assert c.list_channels() == {"a": "C_A", "b": "C_B", "c": "C_C"}
    assert fake.unconsumed("conversations_list") == 0, "pages were left unread"
    assert [kw.get("cursor") for kw in fake.calls_to("conversations_list")] == [
        None, "cur1", "cur2",
    ], "the cursor was not threaded through the walk"
    # And the whole listing is cached, which is what makes name resolution work.
    assert c._channel_name_to_id["c"] == "C_C"


def test_a_single_page_listing_makes_exactly_one_call():
    """Control for the test above: a client that always asked for a second page would
    satisfy it, and would double every listing's cost."""
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "a", "id": "C_A"}]),
    ]})
    assert _client(fake).list_channels() == {"a": "C_A"}
    assert len(fake.calls_to("conversations_list")) == 1


def test_an_empty_page_carrying_a_cursor_is_followed_not_read_as_the_end():
    """Slack does return empty pages mid-walk. Stopping on one loses the tail."""
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [], next_cursor="cur1"),
        _page("channels", [{"name": "b", "id": "C_B"}]),
    ]})
    assert _client(fake).list_channels() == {"b": "C_B"}


def test_every_page_asks_for_slacks_maximum_page_size():
    """A smaller page multiplies the round trips, and the rate limit is per method."""
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "a", "id": "C_A"}], next_cursor="cur1"),
        _page("channels", [{"name": "b", "id": "C_B"}]),
    ]})
    _client(fake).list_channels()
    assert [kw["limit"] for kw in fake.calls_to("conversations_list")] == [
        SLACK_PAGE_LIMIT, SLACK_PAGE_LIMIT,
    ]


def test_a_listing_that_failed_part_way_raises_instead_of_returning_a_subset():
    """The distinction the whole exception exists for: "I got 1 of an unknown number of
    channels" must never be indistinguishable from "there is 1 channel". The subset is
    what made `_ensure_seeded_channels` re-create a channel Slack already had.
    """
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "a", "id": "C_A"}], next_cursor="cur1"),
        slack_error("internal_error"),
    ]})
    c = _client(fake)
    with pytest.raises(SlackListingIncomplete) as exc:
        c.list_channels()
    assert [ch["name"] for ch in exc.value.partial] == ["a"]
    # Caching what it *did* see is still additive and correct — only the return lies.
    assert c._channel_name_to_id["a"] == "C_A"


def test_a_first_page_failure_raises_the_error_callers_already_handle():
    """"The request did not work at all" must keep its existing shape, or every caller's
    `except SlackApiError` stops firing."""
    from slack_sdk.errors import SlackApiError

    fake = SequencedWebClient(sequences={"conversations_list": [slack_error("invalid_auth")]})
    c = _client(fake)
    with pytest.raises(SlackApiError):
        c._paginate("conversations_list", "channels")
    # list_channels keeps degrading to {} for that case, as it always did.
    fake2 = SequencedWebClient(sequences={"conversations_list": [slack_error("invalid_auth")]})
    assert _client(fake2).list_channels() == {}


def test_a_repeated_cursor_stops_the_walk():
    """Observed live: Slack hands back a cursor it has already issued. Following it is
    an infinite loop that never returns to the caller."""
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "a", "id": "C_A"}], next_cursor="same"),
        _page("channels", [{"name": "b", "id": "C_B"}], next_cursor="same"),
    ]})
    with pytest.raises(SlackListingIncomplete) as exc:
        _client(fake).list_channels()
    assert "repeated cursor" in exc.value.reason
    assert len(fake.calls_to("conversations_list")) == 2


def test_the_page_walk_is_bounded_even_if_slack_never_repeats_a_cursor():
    """The repeat check catches one cycle; only a bound guarantees termination when a
    backend cycles through several distinct cursors."""

    class _Endless:
        def __init__(self):
            self.n = 0

        def conversations_list(self, **kwargs):
            self.n += 1
            return _SlackResponse({
                "ok": True,
                "channels": [{"name": f"c{self.n}", "id": f"C{self.n}"}],
                "response_metadata": {"next_cursor": f"cur{self.n}"},
            })

    fake = _Endless()
    with pytest.raises(SlackListingIncomplete) as exc:
        _client(fake).list_channels()
    assert fake.n == MAX_PAGES
    assert len(exc.value.partial) == MAX_PAGES


def test_exclude_archived_defaults_to_false_because_an_archived_channel_owns_its_name():
    """Both callers ask this question to learn whether a *name* is in use, and Slack
    keeps an archived channel's name reserved. Hiding archived channels would send
    `_ensure_seeded_channels` to conversations.create for a name Slack answers
    `name_taken` — the same production failure, reached by a different route.
    """
    fake = SequencedWebClient(sequences={"conversations_list": [_page("channels", [])]})
    _client(fake).list_channels()
    kw = fake.calls_to("conversations_list")[0]
    assert kw["exclude_archived"] is False
    assert kw["types"] == "public_channel"

    # Control: the parameter is not inert, and include_private widens the types.
    fake2 = SequencedWebClient(sequences={"conversations_list": [_page("channels", [])]})
    _client(fake2).list_channels(include_private=True, exclude_archived=True)
    kw2 = fake2.calls_to("conversations_list")[0]
    assert kw2["exclude_archived"] is True
    assert kw2["types"] == "public_channel,private_channel"


def test_resolving_a_channel_name_survives_an_incomplete_listing():
    """`_resolve_channel_id` is on the post path, so it must degrade to "resolve from
    what we know" rather than propagate — otherwise a partial listing turns every post
    into an exception."""
    fake = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "funding", "id": "C_FUND"}], next_cursor="cur1"),
        slack_error("internal_error"),
    ]})
    c = _client(fake)
    assert c._resolve_channel_id("funding") == "C_FUND"

    # A name that is in neither the partial listing nor the cache still falls back to
    # the name itself, as it always did — no exception reaches the post path.
    fake2 = SequencedWebClient(sequences={"conversations_list": [
        _page("channels", [{"name": "funding", "id": "C_FUND"}], next_cursor="cur1"),
        slack_error("internal_error"),
    ]})
    assert _client(fake2)._resolve_channel_id("nope") == "nope"


def test_polling_a_channel_pages_and_still_returns_oldest_first():
    """`limit` is Slack's *page* size, which is what Slack's `limit` means. A tick that
    found more than `limit` new messages used to get the newest `limit` of them, and the
    caller then advanced its poll cursor past the ones it never saw — a silent,
    permanent loss.

    The page shape here is Slack's real one, measured live: with `oldest` set,
    conversations.history anchors at `oldest` and pages FORWARD in time, so page 1 is the
    OLDEST block (newest-first *within* the page). Reversing the concatenated walk — what
    a single page needed — therefore assembles the blocks backwards, and
    `_poll_slack_for_bot_messages` advances `_poll_cursors` to the last message it
    iterates, so the cursor lands mid-window and the same messages are re-polled and
    re-handled on every later tick.
    """
    fake = SequencedWebClient(sequences={"conversations_history": [
        _page("messages", [_msg("3.0"), _msg("2.0")], next_cursor="cur1"),
        _page("messages", [_msg("5.0"), _msg("4.0")]),
    ]})
    out = _client(fake).poll_channel_messages("C_GENERAL", oldest="1.0", limit=2)
    assert [m["ts"] for m in out] == ["2.0", "3.0", "4.0", "5.0"], (
        "a page was dropped, or the pages were assembled in the wrong order — the "
        "caller's poll cursor is set from the LAST element, so it must be the newest"
    )
    calls = fake.calls_to("conversations_history")
    assert [kw["limit"] for kw in calls] == [2, 2]
    assert {kw["oldest"] for kw in calls} == {"1.0"}, "the window moved between pages"


def test_polling_is_oldest_first_for_backward_paging_too():
    """Control, and the other half of the measurement: with no `oldest`, the same
    endpoint pages BACKWARDS in time. Ordering by ts is correct for both, which is why it
    does not depend on Slack's page order at all."""
    fake = SequencedWebClient(sequences={"conversations_history": [
        _page("messages", [_msg("5.0"), _msg("4.0")], next_cursor="cur1"),
        _page("messages", [_msg("3.0"), _msg("2.0")]),
    ]})
    out = _client(fake).poll_channel_messages("C_GENERAL")
    assert [m["ts"] for m in out] == ["2.0", "3.0", "4.0", "5.0"]


def test_a_message_with_an_unparseable_ts_does_not_break_the_ordering():
    """Degrade, don't crash: a malformed ts sorts first rather than raising inside the
    poll loop, which would take down the tick."""
    fake = SequencedWebClient(sequences={"conversations_history": [
        _page("messages", [_msg("2.0"), {"ts": "", "text": "odd"}]),
    ]})
    assert [m["ts"] for m in _client(fake).poll_channel_messages("C_GENERAL")] == ["", "2.0"]


def test_an_incomplete_poll_returns_nothing_rather_than_a_partial_window():
    """The caller advances `_poll_cursors` to the last ts it is handed. Handing it a
    partial window makes it step over the gap permanently, so the only safe answer is
    "nothing new"."""
    fake = SequencedWebClient(sequences={"conversations_history": [
        _page("messages", [_msg("5.0")], next_cursor="cur1"),
        slack_error("internal_error"),
    ]})
    assert _client(fake).poll_channel_messages("C_GENERAL") == []


def test_full_channel_history_uses_the_partial_because_the_db_is_primary():
    """The deliberate asymmetry with the poll above. History pages newest-first, so a
    partial history is missing its OLDEST messages — which the DB rebuild already has —
    and the cursor derived from it still ends at the newest message.
    """
    fake = SequencedWebClient(sequences={"conversations_history": [
        _page("messages", [_msg("9.0"), _msg("8.0")], next_cursor="cur1"),
        slack_error("internal_error"),
    ]})
    out = _client(fake).get_full_channel_history("C_GENERAL")
    assert [m["ts"] for m in out] == ["8.0", "9.0"]


# ===========================================================================
# Inbound thread_ts normalisation — defect 4
# ===========================================================================

# How Slack marks a thread parent once it has replies, and one real reply as a control.
_ROOT = {"ts": "1.0", "thread_ts": "1.0", "text": "root", "user": "U_X", "reply_count": 1}
_REPLY = {"ts": "2.0", "thread_ts": "1.0", "text": "reply", "user": "U_Y"}


def test_normalize_inbound_message_nulls_only_a_self_referential_thread_ts():
    assert normalize_inbound_message(dict(_ROOT))["thread_ts"] is None
    assert normalize_inbound_message(dict(_REPLY))["thread_ts"] == "1.0"
    # A root that has no replies yet carries no thread_ts at all — left alone.
    assert normalize_inbound_message({"ts": "3.0"}).get("thread_ts") is None


@pytest.mark.parametrize("read", [
    "poll_channel_messages", "get_full_channel_history",
])
def test_a_channel_read_never_reports_a_root_as_a_reply_to_itself(read):
    """Slack sets `thread_ts == ts` on a parent once it has replies, so a history page
    hands back thread roots that look like replies to themselves. Anything treating a
    non-null `thread_ts` as "this is a reply" then loses the root:
    `MessageLog.get_new_top_level_posts` skips it, so it never reaches Phase 2, and the
    next `_rebuild_state_from_db` makes that permanent.
    """
    fake = SequencedWebClient(sequences={"conversations_history": [
        _page("messages", [dict(_REPLY), dict(_ROOT)]),
    ]})
    by_ts = {m["ts"]: m for m in getattr(_client(fake), read)("C_GENERAL")}
    assert by_ts["1.0"]["thread_ts"] is None, (
        "the thread root was ingested as a reply to itself, so Phase 2 will never see it"
    )
    assert by_ts["2.0"]["thread_ts"] == "1.0", "a real reply lost its parent"


@pytest.mark.parametrize("read,args", [
    ("get_thread_replies", ("C_GENERAL", "1.0")),
    ("get_all_thread_replies", ("C_GENERAL", "1.0")),
])
def test_a_thread_read_never_reports_the_parent_as_a_reply_to_itself(read, args):
    """conversations.replies returns the parent first, and it carries the same
    self-referential `thread_ts`. The rule has to hold for all four inbound reads or it
    is back to being a property of the call site."""
    fake = SequencedWebClient(sequences={"conversations_replies": [
        _page("messages", [dict(_ROOT), dict(_REPLY)]),
    ]})
    out = getattr(_client(fake), read)(*args)
    assert out[0]["ts"] == "1.0" and out[0]["thread_ts"] is None
    assert out[1]["thread_ts"] == "1.0"


def test_workspace_bookkeeping_is_dropped_from_every_inbound_read():
    """`channel_join` and friends are not conversation. Filtering them in two of the
    four reads (which is what the code did) leaks them into the thread paths."""
    fake = SequencedWebClient(sequences={"conversations_replies": [
        _page("messages", [dict(_ROOT), {"ts": "2.5", "subtype": "channel_join"}, dict(_REPLY)]),
    ]})
    assert [m["ts"] for m in _client(fake).get_all_thread_replies("C_GENERAL", "1.0")] == [
        "1.0", "2.0",
    ]


# ===========================================================================
# The >4000-char split — defect 2
# ===========================================================================

_LONG = "kinetics " * 600            # 5400 chars: two chunks
_VERY_LONG = "kinetics " * 1000      # 9000 chars: three chunks


def test_text_at_the_limit_is_left_as_one_message():
    """Measured live: 4000 characters arrive as a single Slack message. Splitting at the
    boundary would turn one post into two for nothing."""
    body = "x" * SLACK_MAX_TEXT_CHARS
    assert split_for_slack(body) == [body]
    assert split_for_slack("short") == ["short"]


def test_no_chunk_exceeds_the_limit_before_or_after_mrkdwn_conversion():
    """`markdown_to_mrkdwn` runs *after* the split, so the guarantee has to survive it.
    It never lengthens a string — `**x**`->`*x*` shortens, `- `->`• ` is the same
    character count — and this is what says so.
    """
    bodies = [
        _LONG,
        _VERY_LONG,
        "**bold** and - bullets\n" * 500,
        "x" * 12000,                                  # one unbreakable run
        ("word " * 200 + "\n\n") * 12,                # paragraph boundaries
        "a\n" * 5000,                                 # line boundaries only
        "Sentence one. Sentence two. " * 400,         # sentence boundaries
        "```\n" + "row = measure()\n" * 500 + "```",  # a fenced block
    ]
    for body in bodies:
        chunks = split_for_slack(body)
        assert chunks, body[:40]
        over = [len(c) for c in chunks if len(c) > SLACK_MAX_TEXT_CHARS]
        assert not over, f"chunk(s) over the limit: {over} for {body[:40]!r}"
        after = [len(markdown_to_mrkdwn(c)) for c in chunks]
        assert max(after) <= SLACK_MAX_TEXT_CHARS, (
            f"mrkdwn conversion pushed a chunk over the limit: {max(after)}"
        )


def test_the_split_loses_and_duplicates_no_content():
    """Compared with whitespace removed, because cuts land on whitespace and each chunk
    is stripped. A cut that ate a character, or repeated one, fails here."""
    for body in (_LONG, _VERY_LONG, "Sentence one. Sentence two. " * 400):
        joined = "".join(split_for_slack(body))
        assert joined.replace(" ", "").replace("\n", "") == (
            body.replace(" ", "").replace("\n", "")
        )


def test_a_cut_lands_on_a_boundary_rather_than_inside_a_word():
    chunks = split_for_slack(_LONG)
    assert len(chunks) == 2, len(chunks)
    assert chunks[0].endswith("kinetics"), repr(chunks[0][-20:])
    assert chunks[1].startswith("kinetics"), repr(chunks[1][:20])


def test_an_unbreakable_run_is_cut_anyway_rather_than_left_over_the_limit():
    """A 12000-character token has no non-corrupting split point. Refusing to split is
    not an option — Slack would split it for us and hide the tail."""
    chunks = split_for_slack("x" * 12000)
    assert [len(c) for c in chunks] == [4000, 4000, 4000]


@pytest.mark.parametrize("limit", [1, 2, 8, 9, 20])
def test_a_tiny_limit_terminates_instead_of_hanging_the_turn(limit):
    """`limit` is a parameter, and the fence-repair reserve is 8 characters — so a
    fenced body with `limit=8` left a budget of zero, `_cut_at` returned 0, `rest` never
    shrank and this looped forever inside the calling turn. Measured: it never returned.
    Unreachable at the module's own 4000-char limit, but a hang is not a failure mode
    worth leaving available.
    """
    for body in ("```\n" + "abc def\n" * 20 + "```", "abc def ghi " * 20):
        chunks = split_for_slack(body, limit=limit)
        assert chunks, (limit, body[:20])
        assert "".join(chunks).replace(" ", "").replace("\n", "").replace("`", "") == (
            body.replace(" ", "").replace("\n", "").replace("`", "")
        ), f"content lost at limit={limit}"


def test_a_code_fence_spanning_a_cut_is_closed_and_reopened():
    """Slack renders `text` as mrkdwn, so a chunk ending inside a ``` block renders its
    tail as code and the next chunk renders its head as prose — the block boundary
    moves. Every chunk has to be balanced on its own."""
    body = "```\n" + "row = measure(sample)\n" * 400 + "```"
    chunks = split_for_slack(body)
    assert len(chunks) > 1
    for i, chunk in enumerate(chunks):
        assert chunk.count("```") % 2 == 0, f"chunk {i} leaves a fence open"
    # Control: unfenced text of the same shape gets no added backticks.
    assert all("```" not in c for c in split_for_slack("row = measure(sample)\n" * 400))


def test_an_over_limit_post_becomes_several_messages_and_reports_every_one():
    """Slack splits a >4000-char `text` itself and returns only the LAST chunk's ts. A
    client that posts blind therefore records a ts naming the *tail* of its own message
    and leaves every earlier chunk in Slack with no database row — and on restart
    `_rebuild_state_from_slack` ingests those as brand-new inbound messages.
    """
    fake = SequencedWebClient(sequences={"chat_postMessage": [
        {"ok": True, "ts": "1.0", "channel": "C_GENERAL", "message": {}},
        {"ok": True, "ts": "2.0", "channel": "C_GENERAL", "message": {"thread_ts": "1.0"}},
    ]})
    out = _client(fake).post_message("general", _LONG)
    assert out and len(out["posted_messages"]) == 2, out
    assert out["ts"] == "1.0", (
        "post_message returned a ts other than the FIRST message's — this is the value "
        "the engine records as the canonical id and threads replies onto"
    )
    sent = [kw["text"] for kw in fake.calls_to("chat_postMessage")]
    assert len(sent) == 2 and all(len(t) <= SLACK_MAX_TEXT_CHARS for t in sent), (
        [len(t) for t in sent]
    )
    # A split root stays ONE top-level post: the continuation hangs off the first
    # message, so nobody else's Phase 2 scan sees two roots where one post was written.
    assert "thread_ts" not in fake.calls_to("chat_postMessage")[0]
    assert fake.calls_to("chat_postMessage")[1]["thread_ts"] == "1.0"
    posted = out["posted_messages"]
    assert posted[0]["thread_ts"] is None
    assert posted[1]["thread_ts"] == "1.0"
    # Each record carries the SOURCE text of its own message, which is what the DB
    # stores for it — that is what puts agent_messages in bijection with Slack.
    assert [p["text"] for p in posted] == split_for_slack(_LONG)


def test_a_post_within_the_limit_is_one_message_reported_as_one():
    """Control: a client that split everything, or that reported a phantom second
    message, would pass the test above."""
    fake = SequencedWebClient(sequences={"chat_postMessage": [
        {"ok": True, "ts": "1.0", "channel": "C_GENERAL", "message": {}},
    ]})
    out = _client(fake).post_message("general", "short enough")
    assert out["posted_messages"] == [
        {"ts": "1.0", "channel": "C_GENERAL", "text": "short enough", "thread_ts": None},
    ]
    assert len(fake.calls_to("chat_postMessage")) == 1


def test_an_over_limit_reply_keeps_every_chunk_in_the_callers_thread():
    """Control for the root case: for a *reply* every chunk belongs to the thread the
    caller named, not to a sub-thread on the first chunk."""
    fake = SequencedWebClient(sequences={"chat_postMessage": [
        {"ok": True, "ts": f"{i}.0", "channel": "C_GENERAL", "message": {"thread_ts": "0.5"}}
        for i in range(1, 4)
    ]})
    out = _client(fake).post_message("general", _VERY_LONG, thread_ts="0.5")
    posted = out["posted_messages"]
    assert len(posted) == 3
    assert all(p["thread_ts"] == "0.5" for p in posted), [p["thread_ts"] for p in posted]
    assert {kw["thread_ts"] for kw in fake.calls_to("chat_postMessage")} == {"0.5"}


def test_a_chunk_that_fails_stops_the_rest_and_reports_only_what_landed():
    """Never post the tail of a message whose head failed, and never claim a message
    that Slack refused. The caller records one row per reported message, so an
    over-report is a phantom row and an under-report is a lost one."""
    fake = SequencedWebClient(sequences={"chat_postMessage": [
        {"ok": True, "ts": "1.0", "channel": "C_GENERAL", "message": {}},
        slack_error("msg_too_long"),
    ]})
    out = _client(fake).post_message("general", _VERY_LONG)
    assert len(out["posted_messages"]) == 1 and out["ts"] == "1.0"
    assert len(fake.calls_to("chat_postMessage")) == 2, "it kept going after a failure"

    # And a failure on the FIRST chunk posts nothing at all.
    fake2 = SequencedWebClient(sequences={"chat_postMessage": [slack_error("msg_too_long")]})
    assert _client(fake2).post_message("general", _VERY_LONG) is None


def test_the_recorded_thread_parent_is_the_one_slack_reports():
    """Not the one we asked for. The row has to describe the message Slack actually
    made, or the mirror mapping is a guess."""
    fake = SequencedWebClient(sequences={"chat_postMessage": [
        {"ok": True, "ts": "9.9", "channel": "C_OTHER", "message": {"thread_ts": "0.5"}},
    ]})
    out = _client(fake).post_message("general", "reply", thread_ts="0.5")
    assert out["posted_messages"] == [
        {"ts": "9.9", "channel": "C_OTHER", "text": "reply", "thread_ts": "0.5"},
    ]


# ===========================================================================
# create_channel through the chokepoint — defect 3
# ===========================================================================


def test_a_rate_limited_channel_create_is_retried():
    """It bypassed `_call_with_retry` entirely, so a 429 collapsed into the same `None`
    that means "Slack refused" — and `_ensure_seeded_channels` left the channel with no
    id at all."""
    fake = SequencedWebClient(sequences={"conversations_create": [
        slack_error("ratelimited", retry_after=1),
        {"ok": True, "channel": {"id": "C_NEW", "name": "seeded"}},
    ]})
    c = _client(fake)
    assert c.create_channel("seeded") == {"id": "C_NEW", "name": "seeded"}
    assert len(fake.calls_to("conversations_create")) == 2, "the 429 was not retried"
    assert c._channel_name_to_id["seeded"] == "C_NEW"


def test_an_unthrottled_channel_create_is_made_exactly_once():
    """Control for the test above."""
    fake = SequencedWebClient(sequences={"conversations_create": [
        {"ok": True, "channel": {"id": "C_NEW", "name": "seeded"}},
    ]})
    _client(fake).create_channel("seeded")
    assert len(fake.calls_to("conversations_create")) == 1


def test_name_taken_adopts_the_existing_channel_rather_than_reporting_failure():
    """`name_taken` means the channel exists — an archived one still owns its name — so
    reporting failure is what left `_channel_id_map[name] = None`, after which every
    post to it was addressed by name and Slack answered `not_in_channel`."""
    fake = SequencedWebClient(
        sequences={
            "conversations_create": [slack_error("name_taken")],
            "conversations_list": [_page("channels", [{"name": "seeded", "id": "C_OLD"}])],
        },
    )
    assert _client(fake).create_channel("seeded") == {"id": "C_OLD", "name": "seeded"}


def test_name_taken_on_an_invisible_channel_still_reports_failure():
    """Control: adoption is not unconditional. A private channel this bot cannot see is
    `name_taken` with no id to adopt, and inventing one would be worse than failing."""
    fake = SequencedWebClient(
        sequences={
            "conversations_create": [slack_error("name_taken")],
            "conversations_list": [_page("channels", [])],
        },
    )
    assert _client(fake).create_channel("seeded") is None


def test_a_private_channel_create_is_also_retried_on_a_429():
    fake = SequencedWebClient(sequences={"conversations_create": [
        slack_error("ratelimited", retry_after=1),
        {"ok": True, "channel": {"id": "G_NEW", "name": "priv-a-b-x"}},
    ]})
    out = _client(fake).create_private_channel("priv-a-b")
    assert out["id"] == "G_NEW"
    assert len(fake.calls_to("conversations_create")) == 2


def test_an_unconnected_client_raises_rather_than_calling_a_missing_endpoint():
    """`_api` with no WebClient behind it is a programming error, not a runtime
    condition: every public method guards on `self._client` first. Making it explicit
    keeps a new method that forgets the guard from failing as an AttributeError."""
    from src.agent.slack_client import SlackNotConnected

    c = AgentSlackClient(agent_id="su", bot_token="xoxb-test")
    with pytest.raises(SlackNotConnected):
        c._api("auth_test")


def test_is_bot_user_caches_successes_but_never_failures():
    from slack_sdk.errors import SlackApiError

    from src.agent.slack_client import AgentSlackClient

    class _Resp:
        headers: dict = {}
        def get(self, key, default=None):
            return {"error": "internal_error"}.get(key, default)

    class _Stub:
        def __init__(self):
            self.calls = 0
            self.fail_first = True
        def users_info(self, **kw):
            self.calls += 1
            if self.fail_first:
                self.fail_first = False
                raise SlackApiError("boom", response=_Resp())
            return {"user": {"is_bot": True}}

    client = AgentSlackClient(agent_id="su", bot_token="xoxb-x")
    stub = _Stub()
    client._client = stub
    # Failure path: returns False and must NOT be cached as an answer.
    assert client.is_bot_user("U1") is False
    # Retry reaches the API again and the success IS cached.
    assert client.is_bot_user("U1") is True
    assert client.is_bot_user("U1") is True
    assert stub.calls == 2


def test_get_permalink_returns_the_url_on_success():
    from src.agent.slack_client import AgentSlackClient

    class _Stub:
        def chat_getPermalink(self, **kw):
            return {"ok": True, "permalink": "https://example.slack.com/archives/C1/p123"}

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    client._client = _Stub()
    assert client.get_permalink("C1", "123.000") == "https://example.slack.com/archives/C1/p123"


def test_get_permalink_returns_none_on_any_failure():
    from slack_sdk.errors import SlackApiError

    from src.agent.slack_client import AgentSlackClient

    class _Resp:
        headers: dict = {}
        def get(self, key, default=None):
            return {"error": "channel_not_found"}.get(key, default)

    class _Stub:
        def chat_getPermalink(self, **kw):
            raise SlackApiError("boom", response=_Resp())

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    client._client = _Stub()
    assert client.get_permalink("C1", "123.000") is None


async def test_aget_permalink_wraps_the_sync_call():
    from src.agent.slack_client import AgentSlackClient

    class _Stub:
        def chat_getPermalink(self, **kw):
            return {"ok": True, "permalink": "https://example.slack.com/archives/C1/p999"}

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    client._client = _Stub()
    result = await client.aget_permalink("C1", "999.000")
    assert result == "https://example.slack.com/archives/C1/p999"


def test_get_permalink_returns_none_when_disconnected():
    from src.agent.slack_client import AgentSlackClient

    client = AgentSlackClient(agent_id="hub", bot_token="xoxb-x")
    # client._client is None by default (not connected)
    assert client.get_permalink("C1", "123.000") is None
