"""Resolve Slack bot tokens, preferring the DB (``AgentRegistry.slack_bot_token``)
over the legacy ``.env`` / ``config.get_slack_tokens()`` mechanism.

Tokens moved into the DB so newly-activated agents go live without a process
restart (the lru_cached ``Settings`` is frozen at startup). ``.env`` is kept as a
transitional fallback and as the source for the one-time backfill.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models import AgentRegistry

logger = logging.getLogger(__name__)


def is_valid_token(token: str | None) -> bool:
    return bool(token) and not token.startswith("xoxb-placeholder")


def env_token(agent_id: str) -> str | None:
    """Token for an agent from the legacy ``.env`` / config mapping, or None."""
    tok = get_settings().get_slack_tokens().get(agent_id, "")
    return tok if is_valid_token(tok) else None


def token_for_agent_row(agent: AgentRegistry) -> str | None:
    """Token for an already-loaded ``AgentRegistry`` row: DB column first,
    then the legacy ``.env`` fallback."""
    if is_valid_token(agent.slack_bot_token):
        return agent.slack_bot_token
    return env_token(agent.agent_id)


async def get_agent_bot_token(db: AsyncSession, agent_id: str) -> str | None:
    """Token for a specific agent_id: DB column first, then ``.env`` fallback."""
    tok = (
        await db.execute(
            select(AgentRegistry.slack_bot_token).where(
                AgentRegistry.agent_id == agent_id
            )
        )
    ).scalar_one_or_none()
    if is_valid_token(tok):
        return tok
    return env_token(agent_id)


async def slack_globally_enabled(db: AsyncSession) -> bool:
    """Whether Slack integration is on for this deployment.

    Explicit SLACK_ENABLED wins; otherwise auto-detect (on iff at least one
    usable bot token exists anywhere). Used to gate secondary Slack posters
    (GrantBot, the email→Slack relay, web-triggered posts) so they no-op in
    DB-only mode. See specs/local-db-conversations.md.
    """
    setting = get_settings().slack_enabled
    if setting is not None:
        return setting
    return await get_any_bot_token(db) is not None


async def get_any_bot_token(db: AsyncSession) -> str | None:
    """Any valid bot token, for workspace-wide lookups (e.g. users.lookupByEmail).

    Prefers any non-null DB token, then falls back to the first valid ``.env`` token.
    """
    rows = (
        await db.execute(
            select(AgentRegistry.slack_bot_token).where(
                AgentRegistry.slack_bot_token.isnot(None)
            )
        )
    ).scalars().all()
    for tok in rows:
        if is_valid_token(tok):
            return tok
    for tok in get_settings().get_slack_tokens().values():
        if is_valid_token(tok):
            return tok
    return None
