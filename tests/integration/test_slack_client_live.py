"""Every AgentSlackClient method against the real workspace.

Rule S1 throughout: each write is verified by reading Slack back, not by checking that
no exception escaped. Several of these methods return `None` or `[]` on failure, so
"it didn't raise" is not evidence of anything.

One trap the offline contract tests surfaced first: `post_message` runs the text through
`markdown_to_mrkdwn`, so what lands in Slack is not byte-identical to what we passed.
Every live assertion here uses plain prose for that reason.
"""

import os
import time
import uuid

import pytest

from src.agent.slack_client import BotNotInvitedToPrivateChannel, ThreadNotFound

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
    uid = slack_client_su.bot_user_id
    assert uid and uid.startswith("U"), uid
    assert slack_client_su.is_bot_user(uid) is True
    # Control: a human is not a bot. Without it, an is_bot_user that returned True
    # unconditionally would pass.
    assert slack_client_su.is_bot_user(slack_pi_user_id) is False


def test_resolve_user_name_returns_a_name_not_the_raw_id(slack_client_su, slack_pi_user_id):
    """A fallback to the raw id is what you get when users:read is missing, and it is
    silent — the PI's messages would render as U0123ABC in every prompt."""
    name = slack_client_su.resolve_user_name(slack_pi_user_id)
    assert name and name != slack_pi_user_id, f"fell back to the raw id: {name!r}"


def test_an_unknown_user_id_does_not_raise(slack_client_su):
    """Degrade, don't crash: an unresolvable id must not take down a turn."""
    assert slack_client_su.resolve_user_name("U000NOTREAL") is not None


# --- channel lifecycle --------------------------------------------------------------


def test_channel_create_list_join_and_id_resolution(
    slack_client_su, slack_probe_channel, slack_list_all_channels
):
    """Creation, resolution and join. The channel's *existence* is asserted against the
    fully paginated listing rather than against `list_channels()`, which shows one
    200-item page of a 323-channel workspace — see test_list_channels_returns_every_
    public_channel below for that defect, pinned separately so it cannot hide in here.
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


@pytest.mark.xfail(strict=True, reason=(
    "src defect (NOT fixed, reported): AgentSlackClient.list_channels calls "
    "conversations.list with limit=200, ignores response_metadata.next_cursor and never "
    "passes exclude_archived, so on a workspace with more than 200 conversations it "
    "returns an arbitrary subset — Slack orders the result by channel id, which is not "
    "monotonic in creation time. Consequences in production: "
    "_ensure_seeded_channels (simulation.py:3038) fails to find an existing seeded "
    "channel, re-creates it, gets name_taken, and leaves it with NO id; and "
    "post_message's _resolve_channel_id (slack_client.py:394) falls back to passing the "
    "channel NAME to chat.postMessage, which answers not_in_channel. "
    "strict=True on purpose: if pagination is added, or the workspace shrinks below one "
    "page, this XPASSes and fails the run, which is the signal to delete the marker."
))
def test_list_channels_returns_every_public_channel(
    slack_client_su, slack_list_all_channels
):
    """The single-page defect, pinned deterministically.

    This is the root cause of the whole tier's rotating failures: every test that
    addressed a channel by name went through a listing that can silently omit it.
    """
    ground = slack_list_all_channels(slack_client_su)
    listed = slack_client_su.list_channels()
    missing = sorted(set(ground) - set(listed))
    assert not missing, (
        f"list_channels() returned {len(listed)} of {len(ground)} public channels; "
        f"{len(missing)} are invisible to it, e.g. {missing[:5]}"
    )


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

    replies = slack_client_su.get_thread_replies(cid, root["ts"])
    assert "reply from the probe" in [m.get("text") for m in replies]
    assert len(slack_client_su.get_full_channel_history(cid)) >= 1
    assert len(slack_client_su.get_all_thread_replies(cid, root["ts"])) >= 1


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


# --- DMs -----------------------------------------------------------------------------


def test_dm_send_lands_in_slack_but_is_not_polled_back(slack_client_su, slack_pi_user_id):
    """`poll_dm_messages` filters to messages FROM the target user, excluding the bot's
    own. That filter is load-bearing: `handle_dm` replies to whatever the poll returns,
    so a bot that saw its own DM would answer itself forever.

    Both halves. The bot's message must really be in the DM channel (read back
    unfiltered, Rule S1) and must be absent from the filtered poll.
    """
    dm = slack_client_su.open_dm_channel(slack_pi_user_id)
    assert dm and dm.startswith("D"), dm
    marker = f"probe DM {uuid.uuid4().hex[:8]}"
    sent = slack_client_su.send_dm(slack_pi_user_id, marker)
    time.sleep(POST_GAP)
    assert sent and sent.get("ts")

    raw = slack_client_su.poll_channel_messages(dm, oldest="0")
    assert marker in [m.get("text") for m in raw], (
        "the DM never reached Slack at all"
    )
    filtered = slack_client_su.poll_dm_messages(slack_pi_user_id, oldest="0")
    assert marker not in [m.get("text") for m in filtered], (
        "poll_dm_messages returned the bot's own message — handle_dm would reply to "
        "itself in a loop"
    )
    assert all(m.get("user") == slack_pi_user_id for m in filtered), (
        f"poll_dm_messages returned a message from someone else: {filtered}"
    )


def test_open_dm_channel_is_cached(slack_client_su, slack_pi_user_id):
    a = slack_client_su.open_dm_channel(slack_pi_user_id)
    b = slack_client_su.open_dm_channel(slack_pi_user_id)
    assert a == b and slack_client_su._dm_channels[slack_pi_user_id] == a


# --- private channels, and the groups:write finding ------------------------------------


@pytest.fixture
def private_channel(slack_clients):
    """A private channel created by su, archived on teardown."""
    su = slack_clients["su"]
    requested = f"t-priv-{uuid.uuid4().hex[:8]}"
    data = su.create_private_channel(requested)
    assert data and data.get("id"), (
        f"could not create private #{requested}: {data} — if this is missing_scope, su "
        "was installed without groups:write"
    )
    # create_private_channel appends a UTC timestamp for collision avoidance, so the
    # assigned name is not the requested one. Hand back what Slack actually made.
    assert data["name"].startswith(requested), data["name"]
    yield data["name"], data["id"]
    try:
        su._call_with_retry(su._client.conversations_archive, channel=data["id"])
    except Exception as exc:
        print(f"WARNING: could not archive #{data['name']}: {exc}")


def test_private_channel_creation_needs_groups_write(slack_clients):
    """The live A/B behind the BOT_SCOPES finding.

    wiseman was installed with exactly the BOT_SCOPES list as it shipped before this
    work; su was installed with groups:write added. Same code, same call, different
    grant — so a failure here is the scope and nothing else.

    This is why the fix is a scope-coverage test rather than a one-line edit: a bot
    provisioned from the old manifest connects, posts and polls perfectly, and only
    fails the one call that PI pairing depends on.
    """
    su, wiseman = slack_clients["su"], slack_clients["wiseman"]

    ok = su.create_private_channel(f"t-priv-ab-{uuid.uuid4().hex[:6]}")
    assert ok and ok.get("id"), f"su (with groups:write) could not create: {ok}"
    try:
        bad = wiseman.create_private_channel(f"t-priv-ab-{uuid.uuid4().hex[:6]}")
        assert bad is None or not bad.get("id"), (
            "wiseman has no groups:write yet created a private channel — the A/B is "
            f"broken, re-check the install scopes. got {bad}"
        )
    finally:
        su._call_with_retry(su._client.conversations_archive, channel=ok["id"])


def test_private_channel_invite_and_membership(slack_clients, private_channel):
    """A bot cannot self-join a private channel — it must be invited. That distinction
    is exactly what _is_private_channel exists to protect."""
    name, cid = private_channel
    su, cravatt = slack_clients["su"], slack_clients["cravatt"]

    # Before the invite, cravatt cannot read it.
    assert cravatt.poll_channel_messages(cid, oldest="0") == []
    assert su.invite_to_channel(cid, [cravatt.bot_user_id]) is True
    _post(su, cid, "after the invite")
    assert "after the invite" in [
        m.get("text") for m in cravatt.poll_channel_messages(cid, oldest="0")
    ], "the invited bot still cannot read the private channel"


def test_private_channels_are_excluded_from_the_public_listing(
    slack_clients, private_channel, slack_list_all_channels
):
    """Note the name: create_private_channel appends a UTC timestamp to whatever it is
    given, because the reopen slug is deterministic per agent-pair + origin channel and
    Slack rejects a duplicate with name_taken. The fixture returns the name Slack
    actually assigned, not the one requested.

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


def test_a_non_member_bot_posting_to_a_private_channel_is_reported(
    slack_clients, private_channel
):
    """With a visibility_lookup that knows the channel is private, the client must
    raise BotNotInvitedToPrivateChannel rather than swallow the error — the point is
    that an invite-path bug stays visible.
    """
    name, cid = private_channel
    from src.agent.slack_client import AgentSlackClient

    tok = os.environ["SLACK_TEST_BOT_TOKEN_WISEMAN"]
    w = AgentSlackClient(agent_id="wiseman", bot_token=tok,
                         visibility_lookup=lambda c: "collab_private")
    assert w.connect() is True
    with pytest.raises(BotNotInvitedToPrivateChannel):
        w.post_message(cid, "I was never invited")


def test_a_non_member_bot_without_the_lookup_degrades_quietly(slack_clients, private_channel):
    """Control for the test above: the raise is conditional on the visibility lookup.
    Without it the client cannot tell a private channel from a deleted one, and
    returning None is the right degradation."""
    name, cid = private_channel
    from src.agent.slack_client import AgentSlackClient

    tok = os.environ["SLACK_TEST_BOT_TOKEN_WISEMAN"]
    w = AgentSlackClient(agent_id="wiseman", bot_token=tok)   # no visibility_lookup
    assert w.connect() is True
    assert w.post_message(cid, "still not invited") is None
