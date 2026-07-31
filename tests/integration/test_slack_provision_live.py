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


def test_every_probe_bot_authenticates_into_the_same_workspace(slack_bot_tokens):
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


def test_the_granted_scopes_are_the_scopes_we_asked_for(slack_bot_tokens):
    """apps.permissions.scopes reports what the install actually granted.

    su and cravatt were installed with groups:write; wiseman deliberately was not — it
    carries exactly the BOT_SCOPES list as it shipped before this work. That asymmetry
    is the live A/B behind test_private_channel_creation_needs_groups_write.
    """
    def _scopes(tok):
        # Every Slack response carries the token's granted scopes in this header.
        # apps.permissions.scopes is the documented endpoint but returns
        # `not_allowed_token_type` for granular-scope apps, which these are.
        r = httpx.post(f"{SLACK_API}/auth.test",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        raw = r.headers.get("x-oauth-scopes")
        assert raw, "Slack did not report the granted scopes"
        return {s.strip() for s in raw.split(",") if s.strip()}

    su = _scopes(slack_bot_tokens["su"])
    wiseman = _scopes(slack_bot_tokens["wiseman"])
    assert "groups:write" in su, f"su was expected to have groups:write: {sorted(su)}"
    assert "groups:write" not in wiseman, (
        "wiseman is the control for the missing-scope finding and must NOT have "
        f"groups:write: {sorted(wiseman)}"
    )
