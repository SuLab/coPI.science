"""Every AgentSlackClient method against the real workspace.

Rule S1 throughout: each write is verified by reading Slack back, not by checking that
no exception escaped. Several of these methods return `None` or `[]` on failure, so
"it didn't raise" is not evidence of anything.

One trap the offline contract tests surfaced first: `post_message` runs the text through
`markdown_to_mrkdwn`, so what lands in Slack is not byte-identical to what we passed.
Every live assertion here uses plain prose for that reason.
"""

import re
import time
import uuid

import pytest

from src.agent.slack_client import ThreadNotFound
from tests.slack_live_support import create_private_channel, invite, thread_replies

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]

# Slack allows roughly one message per second per channel. Every test here posts a
# handful; this keeps a full-file run comfortably inside that.
POST_GAP = 1.1


def _post(client, channel, text, thread_ts=None):
    out = client.post_message(channel, text, thread_ts=thread_ts)
    time.sleep(POST_GAP)
    return out


# --- identity ----------------------------------------------------------------------


def test_connect_and_identity(slack_client_su, slack_pi_user_id):
    assert slack_client_su.is_connected is True
    uid = slack_client_su._bot_user_id
    assert uid and uid.startswith("U"), uid
    assert slack_client_su.is_bot_user(uid) is True
    # Control: a human is not a bot. Without it, an is_bot_user that returned True
    # unconditionally would pass.
    assert slack_client_su.is_bot_user(slack_pi_user_id) is False


# --- channel lifecycle --------------------------------------------------------------


def test_channel_create_list_join_and_id_resolution(
    slack_client_su, slack_probe_channel, slack_list_all_channels
):
    """Creation, resolution and join. The channel's *existence* is asserted against the
    fully paginated fixture rather than against `list_channels()` — deliberately a
    different code path, so this test cannot pass because the client's own listing and
    the client's own resolution share a bug. `list_channels()`'s completeness is the
    subject of test_list_channels_returns_every_public_channel below, and is not
    re-litigated here.
    """
    name, cid = slack_probe_channel
    assert slack_list_all_channels(slack_client_su).get(name) == cid, (
        f"#{name} was created but Slack does not list it as a public channel"
    )
    # list_channels itself must at least answer with a well-formed page.
    listed = slack_client_su.list_channels()
    assert listed and all(v.startswith("C") for v in listed.values()), listed

    # create_channel populates the name->id cache, which is what makes resolution work
    # without a listing round trip. That is the contract `cache_channel_ids` and
    # `_ensure_seeded_channels` rely on.
    assert slack_client_su._channel_name_to_id.get(name) == cid, (
        "create_channel did not cache the new channel's id"
    )
    assert slack_client_su.get_channel_id(name) == cid
    assert slack_client_su._resolve_channel_id(name) == cid
    assert slack_client_su._resolve_channel_id(cid) == cid, "an id must pass through"
    # join is idempotent — the engine calls it on every post via autojoin.
    slack_client_su.join_channel(cid)
    slack_client_su.join_channel(cid)
    # Control: an unknown name resolves to None rather than to something plausible.
    assert slack_client_su.get_channel_id("t-does-not-exist-zzzz") is None


def test_list_channels_returns_every_public_channel(
    slack_client_su, slack_list_all_channels
):
    """Pagination, against the workspace that broke without it.

    This was the root cause of the whole tier's rotating failures: `list_channels`
    asked conversations.list for a single 200-item page and ignored
    `response_metadata.next_cursor`, so every test that addressed a channel by name
    went through a listing that could silently omit it. Slack orders conversations.list
    by channel id, and ids are not monotonic in creation time, so which channels a
    single page showed was effectively random.

    The control matters as much as the claim: the workspace must be *bigger* than one
    page, or a client that still ignored the cursor would pass this.

    The ground truth is read TWICE, bracketing the call under test, and the comparison is
    made against both. Neither walk is a snapshot — conversations.list is cursor-paginated
    over a workspace this very suite mutates (the previous test archives its probe channel
    on the way out) and Slack's listing is eventually consistent, so a channel can be
    absent from one complete walk and present in the next one seconds later. Measured:
    with a single ground read, this test failed on 1 of 3 consecutive tier runs because a
    `t-probe-` channel archived moments earlier was missing from the ground walk and
    present in `list_channels()` — an artifact of Slack's index latency, asserted as if it
    were a src defect. Bracketing keeps both real claims intact: a channel Slack listed in
    both walks was there throughout and a paginating client must have seen it, and a name
    the client invented is in neither.
    """
    before = slack_list_all_channels(slack_client_su)
    listed = slack_client_su.list_channels()
    after = slack_list_all_channels(slack_client_su)

    stable = set(before) & set(after)
    assert len(stable) > 200, (
        f"only {len(stable)} stably-listed public channels — this workspace no longer "
        "exceeds one 200-item page, so this test can no longer detect a missing paginator"
    )
    missing = sorted(stable - set(listed))
    assert not missing, (
        f"list_channels() returned {len(listed)} of {len(stable)} public channels that "
        f"Slack listed in two independent walks; {len(missing)} are invisible to it, "
        f"e.g. {missing[:5]}"
    )
    invented = sorted(set(listed) - set(before) - set(after))
    assert not invented, (
        "list_channels() returned channels Slack does not list in either walk: "
        f"{invented[:5]}"
    )
    # The ids agree too, not just the names — a listing that paired the right names with
    # the wrong ids resolves every post to the wrong channel.
    assert {n: listed[n] for n in stable} == {n: before[n] for n in stable}


def test_exclude_archived_is_opt_in_because_an_archived_channel_owns_its_name(
    slack_clients, slack_list_all_channels
):
    """Both halves of the `exclude_archived` decision, live.

    The default is False — archived channels ARE listed — and that is deliberate, not
    an oversight: both callers ask this question to learn whether a *name* is in use,
    and Slack keeps the name of an archived channel reserved. A listing that hid
    archived channels would send `_ensure_seeded_channels` to conversations.create for
    a name Slack refuses with `name_taken`, which is the same production failure the
    pagination fix just closed, reached by a different route.

    Control: passing True really does drop it, so the parameter is not inert.
    """
    su = slack_clients["su"]
    name = f"t-arch-{uuid.uuid4().hex[:8]}"
    made = su.create_channel(name)
    assert made and made.get("id"), made
    su._call_with_retry(su._client.conversations_archive, channel=made["id"])

    with_archived = su.list_channels()
    assert with_archived.get(name) == made["id"], (
        f"#{name} is archived and vanished from the default listing — "
        "_ensure_seeded_channels would try to create it and get name_taken"
    )
    without = su.list_channels(exclude_archived=True)
    assert name not in without, (
        "exclude_archived=True still returned an archived channel, so the flag does "
        "nothing"
    )
    assert without and set(without) < set(with_archived), (
        f"exclude_archived=True is not a subset of the default listing: "
        f"{len(without)} vs {len(with_archived)}"
    )
    # And the archived channel is still addressable by name, which is the point.
    assert su.get_channel_id(name) == made["id"]


def test_cache_channel_ids_is_used_by_resolution(slack_client_su):
    """The engine seeds this cache from the DB so it does not re-list on every post."""
    slack_client_su.cache_channel_ids({"t-cached-name": "C_CACHED_FAKE"})
    assert slack_client_su._resolve_channel_id("t-cached-name") == "C_CACHED_FAKE"


# --- posting, threading, history ----------------------------------------------------


def test_post_thread_and_history_round_trip(slack_client_su, slack_probe_channel):
    name, cid = slack_probe_channel
    root = _post(slack_client_su, cid, "root from the probe")
    assert root and root.get("ts"), root
    reply = _post(slack_client_su, cid, "reply from the probe", thread_ts=root["ts"])
    assert reply and reply["ts"] != root["ts"]

    hist = slack_client_su.poll_channel_messages(cid, oldest="0")
    texts = [m.get("text") for m in hist]
    assert "root from the probe" in texts
    # A threaded reply must NOT surface as a top-level history entry, or every reply
    # would be re-ingested as a fresh root by the poller.
    assert "reply from the probe" not in texts, (
        f"the reply appeared at top level — it was not threaded. history={texts}"
    )

    replies = thread_replies(slack_client_su, cid, root["ts"])
    assert "reply from the probe" in [m.get("text") for m in replies]
    assert len(slack_client_su.get_full_channel_history(cid)) >= 1


def test_poll_cursor_excludes_already_seen_messages(slack_client_su, slack_probe_channel):
    """The engine's _poll_cursors depends on this. A poll that ignored `oldest` would
    re-ingest the whole channel every tick and duplicate every message."""
    name, cid = slack_probe_channel
    first = _post(slack_client_su, cid, "before the cursor")
    seen = slack_client_su.poll_channel_messages(cid, oldest="0")
    assert "before the cursor" in [m.get("text") for m in seen]

    after = slack_client_su.poll_channel_messages(cid, oldest=first["ts"])
    assert "before the cursor" not in [m.get("text") for m in after], (
        "oldest= did not exclude the message at that ts"
    )
    # Control: a NEW message past the cursor IS returned, so the filter is a cursor
    # rather than a poll that returns nothing.
    _post(slack_client_su, cid, "after the cursor")
    after2 = slack_client_su.poll_channel_messages(cid, oldest=first["ts"])
    assert "after the cursor" in [m.get("text") for m in after2]


def test_markdown_is_rendered_as_slack_mrkdwn(slack_client_su, slack_probe_channel):
    """Confirms live what the offline contract test asserts about the outbound call."""
    name, cid = slack_probe_channel
    _post(slack_client_su, cid, "a **bold** claim")
    texts = [m.get("text") for m in slack_client_su.poll_channel_messages(cid, oldest="0")]
    assert "a *bold* claim" in texts, texts


def test_replying_to_a_nonexistent_thread_raises_thread_not_found(
    slack_client_su, slack_probe_channel
):
    name, cid = slack_probe_channel
    with pytest.raises(ThreadNotFound):
        slack_client_su.post_message(cid, "reply into the void", thread_ts="1111111111.000100")


def test_posting_to_a_nonexistent_channel_returns_none(slack_client_su):
    """Degrade rather than crash: a stale channel id must not end a turn."""
    assert slack_client_su.post_message("C00000000000", "nowhere") is None


# --- the >4000-char split, at the client boundary --------------------------------------


def _prose(n: int) -> str:
    """Word-separated prose of exactly n characters."""
    unit = "kinetics "
    s = (unit * (n // len(unit) + 2))[:n]
    return s[:-1] + "." if s.endswith(" ") else s


def _texts_in(client, cid) -> list[str]:
    """Every message in the channel, top level and threaded, oldest first."""
    out = []
    for msg in client.get_full_channel_history(cid):
        out.append(msg.get("text") or "")
        if msg.get("reply_count"):
            for r in thread_replies(client, cid, msg["ts"]):
                if r.get("ts") != msg.get("ts"):
                    out.append(r.get("text") or "")
    return out


def test_a_message_at_the_limit_is_one_message(slack_client_su, slack_probe_channel):
    """Measured live: Slack accepts exactly 4000 characters as a single message, so the
    client must not split at the boundary and turn one post into two."""
    from src.agent.slack_client import SLACK_MAX_TEXT_CHARS

    name, cid = slack_probe_channel
    body = _prose(SLACK_MAX_TEXT_CHARS)
    assert len(body) == 4000
    out = _post(slack_client_su, cid, body)
    assert out and len(out["posted_messages"]) == 1, out["posted_messages"]
    assert len(_texts_in(slack_client_su, cid)) == 1


@pytest.mark.parametrize("size", [4001, 8500])
def test_an_over_limit_post_reports_every_message_it_created(
    slack_client_su, slack_probe_channel, size
):
    """Slack splits a >4000-char `text` itself and returns only the LAST chunk's ts, so
    a client that posts blind names the tail of its own message and leaves the head with
    no record. Chunking here instead means every Slack message is one we can account for.

    4001 is the first size past the boundary; 8500 forces three chunks. Both are asserted
    the same way, because the property — not the chunk count — is what matters:
    `posted_messages` must be exactly the set of messages the channel now holds, in order,
    and its FIRST ts (not its last) must be what `post_message` returns for threading.
    """
    name, cid = slack_probe_channel
    body = _prose(size)
    out = _post(slack_client_su, cid, body)
    assert out, "the oversized post did not land at all"
    posted = out["posted_messages"]
    assert len(posted) >= 2, f"{size} chars was not split: {len(posted)} message(s)"
    assert out["ts"] == posted[0]["ts"], (
        "post_message returned a ts other than the first message's — this is the value "
        "the engine records as the canonical id and threads replies onto"
    )

    live = _texts_in(slack_client_su, cid)
    assert len(live) == len(posted), (
        f"Slack holds {len(live)} message(s) for {len(posted)} reported: {live[:2]}"
    )
    # Every reported chunk is really there, and nothing else is.
    from src.agent.slack_client import markdown_to_mrkdwn
    assert [markdown_to_mrkdwn(p["text"]) for p in posted] == live
    # No content was lost or duplicated across the split.
    assert re.sub(r"\s+", "", "".join(live)) == re.sub(r"\s+", "", body)
    # A split root stays ONE top-level post: the continuations hang off the first
    # message, so no reader of top-level history sees several roots for one post.
    assert posted[0]["thread_ts"] is None
    assert all(p["thread_ts"] == posted[0]["ts"] for p in posted[1:]), (
        f"continuation chunks are not threaded on the first: {[p['thread_ts'] for p in posted]}"
    )
    assert len(slack_client_su.get_full_channel_history(cid)) == 1, (
        "the split produced more than one top-level message"
    )


def test_an_over_limit_reply_keeps_every_chunk_in_the_caller_s_thread(
    slack_client_su, slack_probe_channel
):
    """Control for the test above: for a *reply*, every chunk belongs to the thread the
    caller named — not to a sub-thread on the first chunk."""
    name, cid = slack_probe_channel
    root = _post(slack_client_su, cid, "root for a long reply")
    out = _post(slack_client_su, cid, _prose(9000), thread_ts=root["ts"])
    posted = out["posted_messages"]
    assert len(posted) >= 3, len(posted)
    assert all(p["thread_ts"] == root["ts"] for p in posted), (
        f"a reply chunk left the thread: {[p['thread_ts'] for p in posted]}"
    )
    replies = thread_replies(slack_client_su, cid, root["ts"])
    assert len([r for r in replies if r["ts"] != root["ts"]]) == len(posted)


def test_a_code_fence_spanning_a_split_is_closed_and_reopened(
    slack_client_su, slack_probe_channel
):
    """Slack renders `text` as mrkdwn, so a chunk that ends inside a ``` block renders
    its tail as code and the next chunk renders its head as prose — the split moves the
    block boundary. Balancing each chunk keeps every piece rendering as the whole would.
    """
    name, cid = slack_probe_channel
    body = "Here is the analysis script:\n\n```\n" + "\n".join(
        f"row_{i} = measure(sample_{i})  # covalent engagement at t={i}" for i in range(120)
    ) + "\n```\n\nThat is the whole pipeline."
    assert len(body) > 4000, len(body)
    out = _post(slack_client_su, cid, body)
    posted = out["posted_messages"]
    assert len(posted) >= 2, len(posted)
    for i, p in enumerate(posted):
        assert p["text"].count("```") % 2 == 0, (
            f"chunk {i} leaves a code fence open: ...{p['text'][-60:]!r}"
        )
    live = _texts_in(slack_client_su, cid)
    assert len(live) == len(posted)
    # The fence repair is the only text added; every original line survives.
    joined = "".join(live)
    for i in (0, 60, 119):
        assert f"row_{i} = measure(sample_{i})" in joined


# --- private channels -------------------------------------------------------------------


@pytest.fixture
def private_channel(slack_clients):
    """A private channel created by su, archived on teardown.

    Needs `groups:write` on su, which `BOT_SCOPES` no longer asks for; su was
    installed with it (see tests/slack_live_support.py).
    """
    su = slack_clients["su"]
    data = create_private_channel(su, f"t-priv-{uuid.uuid4().hex[:8]}")
    assert data and data.get("id"), data
    yield data["name"], data["id"]
    try:
        su._call_with_retry(su._client.conversations_archive, channel=data["id"])
    except Exception as exc:
        print(f"WARNING: could not archive #{data['name']}: {exc}")


def test_private_channel_invite_and_membership(slack_clients, private_channel):
    """A bot cannot self-join a private channel — it must be invited. That distinction
    is why private channels are invited into, never self-joined."""
    name, cid = private_channel
    su, cravatt = slack_clients["su"], slack_clients["cravatt"]

    # Before the invite, cravatt cannot read it.
    assert cravatt.poll_channel_messages(cid, oldest="0") == []
    invite(su, cid, [cravatt._bot_user_id])
    _post(su, cid, "after the invite")
    assert "after the invite" in [
        m.get("text") for m in cravatt.poll_channel_messages(cid, oldest="0")
    ], "the invited bot still cannot read the private channel"


def test_private_channels_are_excluded_from_the_public_listing(
    slack_clients, private_channel, slack_list_all_channels
):
    """The private listing asks conversations.list for `private_channel`, which is what
    `groups:read` covers. `BOT_SCOPES` no longer requests that scope; su holds it from
    its original install, and a reinstalled su would need it added back for this test.

    Both halves go through the fully paginated listing. Asking `list_channels()` (one
    200-item page of 323) made the positive half a coin flip AND the negative half
    vacuous — a private channel really leaking into the public listing would still be
    absent from page 1 about 38% of the time, so `not in` proved nothing.
    """
    name, cid = private_channel
    su = slack_clients["su"]
    assert slack_list_all_channels(su, include_private=True).get(name) == cid, (
        f"the private channel is missing from the include_private listing: {name}"
    )
    assert name not in slack_list_all_channels(su, include_private=False), (
        "a private channel leaked into the public listing"
    )
