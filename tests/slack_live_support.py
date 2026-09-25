"""Private-channel setup for the live Slack tier.

`AgentSlackClient` no longer creates or invites into private channels: the feature
that needed it is gone, so production has no caller. The live tier still needs a
private channel to test what production DOES do with one (polling through a member
bot, the not-invited error, the listing exclusion), so these helpers make it through
the client's own `_api`, which keeps its rate-limit retry.

Both need `groups:write` on the calling bot. That scope is no longer in `BOT_SCOPES`;
`su` was installed with it for this purpose, and
`test_slack_provision_live.py::test_the_granted_scopes_are_the_scopes_we_asked_for`
checks that it still holds it. Call them as `su`.

Unlike the removed client methods, both raise `SlackApiError` on failure rather than
returning a falsy value, so a missing scope stops the test at the setup line.
"""


def create_private_channel(client, name: str) -> dict:
    """Create private channel `name` exactly as given and return Slack's channel dict.

    No timestamp suffix, no retry on `name_taken`, and no entry in the client's
    name->id cache: callers pass a fresh uuid-suffixed name, and address the channel
    by its id or seed `cache_channel_ids` themselves.
    """
    return client._api("conversations_create", name=name, is_private=True)["channel"]


def invite(client, cid: str, uids) -> None:
    """Invite each user id in `uids` to channel `cid`, one conversations.invite per id.

    One call per id because Slack aborts a comma-separated invite at the first
    per-user error. Nothing is tolerated: `already_in_channel` and `cant_invite_self`
    raise like any other error.
    """
    for uid in uids:
        client._api("conversations_invite", channel=cid, users=uid)
