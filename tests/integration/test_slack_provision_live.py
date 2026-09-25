"""Live provisioning: the probe bots really exist in the workspace.

This is the precondition every other live test rests on. If the tokens are dead, every
downstream failure would be about the tokens rather than about the code, so this runs
first and says so plainly.

Rule S1: assert on Slack's answer, not on our database. A token column being set proves
we wrote a column.
"""

import os

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live_slack]

SLACK_API = "https://slack.com/api"


def _auth_test(token: str) -> dict:
    return httpx.post(f"{SLACK_API}/auth.test",
                      headers={"Authorization": f"Bearer {token}"}, timeout=15).json()


def test_every_probe_bot_authenticates_into_the_same_workspace(slack_bot_tokens_all):
    # slack_bot_tokens_all, not slack_bot_tokens: this test compares the bots
    # against each other, so a partial set makes it vacuous. Skip, don't fail.
    slack_bot_tokens = slack_bot_tokens_all
    assert set(slack_bot_tokens) == {"su", "cravatt", "wiseman"}, sorted(slack_bot_tokens)
    teams, users = set(), {}
    for aid, tok in slack_bot_tokens.items():
        d = _auth_test(tok)
        assert d.get("ok"), f"{aid}: auth.test failed: {d.get('error')}"
        teams.add(d["team_id"])
        users[aid] = d["user_id"]
    assert len(teams) == 1, f"the bots are in different workspaces: {teams}"
    assert teams == {os.environ["SLACK_TEST_TEAM_ID"]}, (
        f"installed into the wrong workspace: {teams}"
    )
    assert len(set(users.values())) == 3, f"two agents share a bot user: {users}"


def test_lookup_team_id_agrees_with_auth_test(slack_bot_tokens):
    """`lookup_team_id` is what start_provisioning uses to pin the OAuth URL to the
    right workspace — the exact thing whose absence sent the first hand-built install
    links at the wrong team."""
    from src.services.slack_provisioning import lookup_team_id

    tok = slack_bot_tokens["su"]
    assert lookup_team_id(tok) == os.environ["SLACK_TEST_TEAM_ID"]
    # Control: it must return None for a non-bot token rather than guessing.
    assert lookup_team_id("xoxp-not-a-bot-token") is None
    assert lookup_team_id("") is None


def test_the_granted_scopes_are_the_scopes_we_asked_for(slack_bot_tokens_all):
    """Slack's x-oauth-scopes header reports what the install actually granted.

    Every probe bot must hold every scope in `BOT_SCOPES`. They were installed from
    an older, larger manifest, so they may hold more; the check is a subset, not an
    equality.

    su must also hold `groups:write` and `groups:read`, which `BOT_SCOPES` no longer
    requests: the live tier's private-channel helpers (tests/slack_live_support.py)
    create and invite as su, its listing test lists private channels as su, and su was
    installed with both scopes for them. A reinstalled su needs both added back
    (`--add-scope su:groups:write --add-scope su:groups:read`) or the private-channel
    live tests fail with missing_scope.
    """
    from src.services.slack_provisioning import BOT_SCOPES

    def _scopes(tok):
        # Every Slack response carries the token's granted scopes in this header.
        # apps.permissions.scopes is the documented endpoint but returns
        # `not_allowed_token_type` for granular-scope apps, which these are.
        r = httpx.post(f"{SLACK_API}/auth.test",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        raw = r.headers.get("x-oauth-scopes")
        assert raw, "Slack did not report the granted scopes"
        return {s.strip() for s in raw.split(",") if s.strip()}

    granted = {aid: _scopes(tok) for aid, tok in slack_bot_tokens_all.items()}
    for aid, scopes in granted.items():
        missing = sorted(set(BOT_SCOPES) - scopes)
        assert not missing, f"{aid} lacks scopes BOT_SCOPES requests: {missing}"
    su = granted["su"]
    assert "groups:write" in su, f"su was expected to have groups:write: {sorted(su)}"
    assert "groups:read" in su, f"su was expected to have groups:read: {sorted(su)}"
