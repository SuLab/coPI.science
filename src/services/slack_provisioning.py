"""Shared Slack app/bot provisioning helpers.

Used by both the batch CLI (``scripts/provision_slack_bots.py``) and the
self-service admin endpoints (``src/routers/admin.py``). The two callers differ
only in their OAuth ``redirect_uri`` (the script runs a localhost callback
server; the web app uses ``{base_url}/admin/agents/slack/callback``) and in
where the resulting ``xoxb-`` token is stored.

Functions here are transport-only (httpx) and raise ``RuntimeError`` on Slack
API errors; callers handle presentation/logging.

The core is synchronous, matching ``slack_web.py``'s split (``AgentSlackClient``
uses ``slack_sdk``; this module uses raw ``httpx`` for the manifest/OAuth API,
which ``slack_sdk`` doesn't cover). **Async callers (the admin routes, via
``admin_provisioning.py``) MUST use the ``_async`` wrappers at the bottom of
this module, not the sync functions** — ``create_app``'s retry loop alone can
block for minutes on a rate-limited response (issue #24 C2).
"""

import asyncio
import logging
import time

import httpx

from src.agent.retry_after import parse_retry_after

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

# Cap on how long a single apps.manifest.create rate-limit wait may run, even
# though it now happens on a worker thread (asyncio.to_thread) rather than the
# event loop — an unbounded Slack-supplied Retry-After (issue #24 C2-3 measured
# up to 4500s) would otherwise still tie up that thread and the admin's request
# for an unreasonable time. Mirrors slack_web._MAX_RETRY_AFTER.
_MAX_MANIFEST_RETRY_AFTER = 30.0

# All scopes the bots actually use — derived from AgentSlackClient + routers/podcast.
BOT_SCOPES = [
    "channels:history",   # conversations.history / conversations.replies
    "channels:join",      # conversations.join
    "channels:manage",    # conversations.create
    "channels:read",      # conversations.list
    "chat:write",         # chat.postMessage
    "groups:history",     # threads in private channels
    "groups:read",        # conversations.list private
    # conversations.create(is_private=True) and conversations.invite into a private
    # channel both require this. Without it a bot provisions, connects and posts
    # perfectly, and then private-channel migration — the PI-pairing feature — fails
    # with missing_scope. Adding a scope needs every existing bot REINSTALLED; an
    # already-installed app keeps the grant it was installed with.
    "groups:write",       # conversations.create/invite for private channels
    "im:history",         # poll_dm_messages
    "im:write",           # conversations.open (DMs)
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
    retry_after_cap: float = _MAX_MANIFEST_RETRY_AFTER,
) -> dict:
    """Create one Slack app via the Manifest API.

    Returns a dict with ``agent_id``, ``bot_name``, ``pi_name``, ``app_id``,
    ``client_id``, ``client_secret``, ``oauth_url``. Retries on rate-limit
    responses only; all other errors raise immediately.

    ``scopes`` defaults to ``BOT_SCOPES``. It is overridable because the scope set
    is fixed at *manifest* time and cannot be changed at install time — Slack's
    consent screen offers Allow or Cancel, not a per-scope choice, and an
    already-installed app keeps the grant it was installed with. The live Slack
    tier depends on that: ``wiseman`` is the control that must NOT hold
    ``groups:write`` (see
    ``test_slack_provision_live.py::test_the_granted_scopes_are_the_scopes_we_asked_for``
    and ``test_private_channel_creation_needs_groups_write``), so it has to be
    created from a reduced manifest or the asymmetry is unreproducible.
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
        if data.get("error") != "ratelimited":
            detail = data.get("errors") or data.get("error", "unknown")
            raise RuntimeError(f"apps.manifest.create failed: {detail}")
        # C2: don't sleep on the last attempt -- the old code slept once more,
        # uselessly, right before giving up and raising below.
        if attempt == max_rate_limit_retries - 1:
            break
        raw_retry_after = data.get("retry_after") or resp.headers.get("Retry-After")
        wait = parse_retry_after(
            str(raw_retry_after) if raw_retry_after is not None else None,
            default=60.0,
            cap=retry_after_cap,
        )
        logger.warning(
            "apps.manifest.create rate limited — waiting %.0fs before retry (capped at %.0fs)",
            wait, retry_after_cap,
        )
        time.sleep(wait)
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


# ---------------------------------------------------------------------------
# issue #24 C2: every function above is synchronous httpx, and create_app's
# retry loop alone can now sleep up to max_rate_limit_retries * 30s (capped,
# see _MAX_MANIFEST_RETRY_AFTER) between rate-limited attempts. Called directly
# from an `async def` route (admin_provisioning.py), that blocks the whole
# event loop -- the single uvicorn worker has nothing else to run -- so every
# other request the process is serving freezes for as long as Slack keeps
# rate-limiting. asyncio.to_thread moves the whole call (including its
# internal time.sleep) to a worker thread; mirrors slack_web.py:267-300.
# ---------------------------------------------------------------------------


async def lookup_team_id_async(bot_token: str) -> str | None:
    """``lookup_team_id`` off the event loop."""
    return await asyncio.to_thread(lookup_team_id, bot_token)


async def rotate_config_token_async(refresh_token: str) -> tuple[str, str, int]:
    """``rotate_config_token`` off the event loop."""
    return await asyncio.to_thread(rotate_config_token, refresh_token)


async def create_app_async(
    config_token: str,
    agent_id: str,
    bot_name: str,
    pi_name: str,
    redirect_uri: str,
    max_rate_limit_retries: int = 5,
    scopes: list[str] | None = None,
) -> dict:
    """``create_app`` off the event loop, including its internal retry sleeps."""
    return await asyncio.to_thread(
        create_app, config_token, agent_id, bot_name, pi_name, redirect_uri,
        max_rate_limit_retries=max_rate_limit_retries, scopes=scopes,
    )


async def exchange_code_async(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> str:
    """``exchange_code`` off the event loop."""
    return await asyncio.to_thread(
        exchange_code, client_id, client_secret, code, redirect_uri
    )
