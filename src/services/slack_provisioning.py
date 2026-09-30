"""Shared Slack app/bot provisioning helpers.

Used by both the batch CLI (``scripts/provision_slack_bots.py``) and the
self-service admin endpoints (``src/routers/admin/agents.py``). The two callers differ
only in their OAuth ``redirect_uri`` (the script runs a localhost callback
server; the web app uses ``{base_url}/admin/agents/slack/callback``) and in
where the resulting ``xoxb-`` token is stored.

Functions here are transport-only (httpx) and raise ``RuntimeError`` on Slack
API errors; callers handle presentation/logging.
"""

import logging
import time

import httpx

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

# Exactly the scopes required by the Slack methods that AgentSlackClient and
# services/slack_web.py call, per Slack's method reference
# (https://docs.slack.dev/reference/methods). METHOD_SCOPES in
# tests/unit/test_slack_provisioning.py maps each of those methods to its scope, with
# the doc links, and asserts this list equals what they need. auth.test, auth.revoke
# and chat.getPermalink need none. A scope added or dropped here reaches an existing
# bot only when it is REINSTALLED; an installed app keeps the grant it was installed
# with.
BOT_SCOPES = [
    "channels:history",   # conversations.history / conversations.replies
    "channels:join",      # conversations.join
    "channels:manage",    # conversations.create
    "channels:read",      # conversations.list
    "chat:write",         # chat.postMessage / chat.delete
    # History and replies in collab_private channels. Nothing reads private channels
    # any more (the private-channel sync was retired); the scope stays so every app
    # manifest keeps the same scope set. Listing private channels would also need
    # groups:read, and no caller lists them:
    # https://docs.slack.dev/reference/scopes/groups.read
    "groups:history",
    "users:read",         # users.info
    "users:read.email",   # users.lookupByEmail
]


def lookup_team_id(bot_token: str) -> str | None:
    """Return the workspace team_id for a valid xoxb- bot token, or None."""
    if not bot_token or not bot_token.startswith("xoxb-"):
        return None
    resp = httpx.post(
        f"{SLACK_API}/auth.test",
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=10,
    )
    data = resp.json()
    return data.get("team_id") if data.get("ok") else None


def rotate_config_token(refresh_token: str) -> tuple[str, str, int]:
    """Rotate the app-config token.

    Returns ``(new_access_token, new_refresh_token, exp)`` where ``exp`` is the
    access token's expiry (unix seconds; 0 if Slack omits it). Slack rotates the
    refresh token too and it is single-use, so the caller MUST persist the new
    pair atomically. ``exp`` lets callers cache the access token and avoid
    rotating on every use (see admin_provisioning; SEC-10).
    """
    resp = httpx.post(
        f"{SLACK_API}/tooling.tokens.rotate",
        data={"refresh_token": refresh_token},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"tooling.tokens.rotate failed: {data.get('error')}")
    return data["token"], data["refresh_token"], int(data.get("exp", 0) or 0)


def create_app(
    config_token: str,
    agent_id: str,
    bot_name: str,
    pi_name: str,
    redirect_uri: str,
    max_rate_limit_retries: int = 5,
    scopes: list[str] | None = None,
) -> dict:
    """Create one Slack app via the Manifest API.

    Returns a dict with ``agent_id``, ``bot_name``, ``pi_name``, ``app_id``,
    ``client_id``, ``client_secret``, ``oauth_url``. Retries on rate-limit
    responses only; all other errors raise immediately.

    ``scopes`` defaults to ``BOT_SCOPES``. It is overridable because the scope set
    is fixed at *manifest* time and cannot be changed at install time — Slack's
    consent screen offers Allow or Cancel, not a per-scope choice, and an
    already-installed app keeps the grant it was installed with. The live Slack
    tier depends on that: its private-channel helpers create and invite as ``su``,
    and its listing test lists private channels as ``su``, which needs
    ``groups:write`` and ``groups:read`` on top of ``BOT_SCOPES`` and so has to be
    created from a widened manifest (``scripts/provision_slack_bots.py --add-scope
    su:groups:write --add-scope su:groups:read``; see
    ``test_slack_provision_live.py::test_the_granted_scopes_are_the_scopes_we_asked_for``).
    """
    scopes = list(BOT_SCOPES) if scopes is None else list(scopes)
    manifest = {
        "display_information": {
            "name": bot_name,
            "description": f"LabBot agent for {pi_name}",
        },
        "features": {
            "bot_user": {
                "display_name": bot_name,
                "always_online": False,
            }
        },
        "oauth_config": {
            "redirect_urls": [redirect_uri],
            "scopes": {"bot": scopes},
        },
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    for attempt in range(max_rate_limit_retries):
        resp = httpx.post(
            f"{SLACK_API}/apps.manifest.create",
            headers={"Authorization": f"Bearer {config_token}"},
            json={"manifest": manifest},
            timeout=20,
        )
        data = resp.json()
        if data.get("ok"):
            creds = data["credentials"]
            return {
                "agent_id": agent_id,
                "bot_name": bot_name,
                "pi_name": pi_name,
                "app_id": data["app_id"],
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "oauth_url": data["oauth_authorize_url"],
            }
        if data.get("error") == "ratelimited":
            wait = int(data.get("retry_after", 0) or resp.headers.get("Retry-After", 60))
            logger.warning("apps.manifest.create rate limited — waiting %ds before retry", wait)
            time.sleep(wait)
        else:
            detail = data.get("errors") or data.get("error", "unknown")
            raise RuntimeError(f"apps.manifest.create failed: {detail}")
    raise RuntimeError(
        f"apps.manifest.create: still rate-limited after {max_rate_limit_retries} retries"
    )


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    """Exchange a temporary OAuth code for a bot token. Returns the xoxb-... string."""
    resp = httpx.post(
        f"{SLACK_API}/oauth.v2.access",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"oauth.v2.access failed: {data.get('error')}")
    token = data.get("access_token", "")
    if not token.startswith("xoxb-"):
        # Do NOT echo any part of the token — this message can surface in logs
        # and a user-facing ?slack_error= redirect. See SEC-9.
        raise RuntimeError("Unexpected token format from Slack (expected xoxb-...)")
    return token
