"""Self-service Slack bot provisioning for the admin approve page.

Wraps the shared ``slack_provisioning`` helpers with web-flow concerns:
- the rotating Slack app-config token, persisted in the ``AppSetting`` KV table
  (Slack rotates it on every use, and the lru_cached Settings/.env can't be
  written from a request handler);
- a short-lived ``SlackAppProvision`` row bridging the "Provision" click and the
  OAuth callback;
- writing the resulting bot token onto the ``AgentRegistry`` row.
"""

import logging
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import AgentRegistry, AppSetting, SlackAppProvision
from src.services.slack_provisioning import (
    create_app,
    exchange_code,
    lookup_team_id,
)

logger = logging.getLogger(__name__)

_KEY_TOKEN = "slack_config_token"
_KEY_REFRESH = "slack_config_refresh_token"
CALLBACK_PATH = "/admin/agents/slack/callback"


class ProvisioningError(RuntimeError):
    """Raised for any provisioning failure surfaced to the admin UI."""


async def _kv_get(db: AsyncSession, key: str) -> str | None:
    return (
        await db.execute(select(AppSetting.value).where(AppSetting.key == key))
    ).scalar_one_or_none()


async def _kv_set(db: AsyncSession, key: str, value: str) -> None:
    row = (
        await db.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


async def _get_rotated_config_token(db: AsyncSession) -> str:
    """Return a usable app-config token, rotating + persisting the pair.

    Slack rotates the refresh token on every use, so the new pair MUST be saved.
    Seeds from ``Settings`` (``.env``) on first use, then the KV row is
    authoritative.
    """
    settings = get_settings()
    refresh = await _kv_get(db, _KEY_REFRESH) or settings.slack_config_refresh_token
    token = await _kv_get(db, _KEY_TOKEN) or settings.slack_config_token

    if refresh:
        try:
            from src.services.slack_provisioning import rotate_config_token
            new_token, new_refresh = rotate_config_token(refresh)
        except Exception as exc:
            raise ProvisioningError(f"Could not rotate the Slack config token: {exc}")
        await _kv_set(db, _KEY_TOKEN, new_token)
        await _kv_set(db, _KEY_REFRESH, new_refresh)
        await db.commit()
        return new_token

    if token:
        return token
    raise ProvisioningError(
        "Slack config token is not configured. Set SLACK_CONFIG_TOKEN and "
        "SLACK_CONFIG_REFRESH_TOKEN in the environment."
    )


def _redirect_uri() -> str:
    return f"{get_settings().base_url.rstrip('/')}{CALLBACK_PATH}"


async def start_provisioning(db: AsyncSession, agent: AgentRegistry) -> str:
    """Create a Slack app for ``agent`` and return the OAuth authorize URL.

    Persists a ``SlackAppProvision`` row keyed by a random ``state`` so the
    callback can finish the exchange.
    """
    config_token = await _get_rotated_config_token(db)
    redirect_uri = _redirect_uri()

    try:
        app = create_app(
            config_token=config_token,
            agent_id=agent.agent_id,
            bot_name=agent.bot_name,
            pi_name=agent.pi_name,
            redirect_uri=redirect_uri,
        )
    except Exception as exc:
        raise ProvisioningError(f"Could not create the Slack app: {exc}")

    state = secrets.token_urlsafe(32)
    db.add(SlackAppProvision(
        agent_registry_id=agent.id,
        state=state,
        client_id=app["client_id"],
        client_secret=app["client_secret"],
        app_id=app.get("app_id"),
    ))
    await db.commit()

    # Best-effort: pin the OAuth URL to the right workspace.
    from urllib.parse import urlencode
    from src.services.slack_tokens import get_any_bot_token
    extra = {"state": state, "redirect_uri": redirect_uri}
    team_token = await get_any_bot_token(db)
    if team_token:
        team_id = lookup_team_id(team_token)
        if team_id:
            extra["team"] = team_id
    return app["oauth_url"] + "&" + urlencode(extra)


async def complete_provisioning(db: AsyncSession, state: str, code: str) -> AgentRegistry:
    """Finish the OAuth round-trip: exchange the code, save the token on the
    agent, and delete the bridge row. Returns the updated agent."""
    prov = (
        await db.execute(
            select(SlackAppProvision).where(SlackAppProvision.state == state)
        )
    ).scalar_one_or_none()
    if not prov:
        raise ProvisioningError("Unknown or expired provisioning state.")

    agent = (
        await db.execute(
            select(AgentRegistry).where(AgentRegistry.id == prov.agent_registry_id)
        )
    ).scalar_one_or_none()
    if not agent:
        await db.delete(prov)
        await db.commit()
        raise ProvisioningError("Agent no longer exists.")

    try:
        token = exchange_code(
            prov.client_id, prov.client_secret, code, _redirect_uri()
        )
    except Exception as exc:
        # Delete the bridge row on failure: it holds the app client_secret and a
        # reusable OAuth state (no TTL), so leaving it behind is a standing
        # secret + replay surface. Log details server-side only; surface a
        # generic message so no token/secret fragment reaches the redirect URL
        # or access logs. See SEC-9.
        logger.error(
            "Token exchange failed for agent %s (provision %s): %s",
            prov.agent_registry_id, prov.id, exc,
        )
        await db.delete(prov)
        await db.commit()
        raise ProvisioningError("Token exchange with Slack failed. Please retry provisioning.")

    agent.slack_bot_token = token
    await db.delete(prov)
    await db.commit()
    logger.info("Provisioned Slack bot token for agent %s via admin UI", agent.agent_id)
    return agent
