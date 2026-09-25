"""Backfill: copy each agent's Slack bot token from ``.env`` into the
``AgentRegistry.slack_bot_token`` DB column.

The simulation reads tokens from the DB column (so newly-activated agents go
live without a restart). ``scripts/provision_slack_bots.py`` writes each token
to ``.env`` as ``SLACK_BOT_TOKEN_<AGENT_ID upper>``; this script imports them.

Tokens are read from the process environment (``os.environ``, not
``Settings``): ``Settings`` ignores undeclared keys, so a newly provisioned
agent's key is visible only in the environment, where compose's ``env_file:``
puts every ``.env`` key. The legacy ``config.get_slack_tokens()`` map is the
fallback for an agent with no valid per-agent key (see ``env_token_for``).

Idempotent: only fills rows whose ``slack_bot_token`` is currently null/blank,
and only from a valid (non-placeholder) env token. Safe to re-run.

Usage — a one-off container, not ``exec``: ``.env`` is read when a container is
created, so a long-running container does not see keys added since it started:

    docker compose -f docker-compose.prod.yml run --rm --no-deps -T blackbird-app python scripts/backfill_agent_tokens.py
    # preview only:
    docker compose -f docker-compose.prod.yml run --rm --no-deps -T blackbird-app python scripts/backfill_agent_tokens.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# Prefer the project root (``/app`` in the image) over any baked-in copy of `src` in
# site-packages (the image installs src/ non-editable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill_agent_tokens")


def _valid(tok: str | None) -> bool:
    return bool(tok) and not tok.startswith("xoxb-placeholder")


def env_token_for(agent_id: str, legacy: Mapping[str, str], environ: Mapping[str, str]) -> str:
    """Return the env token for ``agent_id``, or ``""`` if none is known.

    Prefers ``environ["SLACK_BOT_TOKEN_<AGENT_ID upper>"]`` (the key
    ``provision_slack_bots.py`` writes) when it is valid; otherwise falls back
    to ``legacy[agent_id]``. The fallback may itself be a placeholder, so the
    caller still checks the result with ``_valid``.
    """
    tok = environ.get(f"SLACK_BOT_TOKEN_{agent_id.upper()}")
    if _valid(tok):
        return tok
    return legacy.get(agent_id, "")


async def main(dry_run: bool) -> None:
    settings = get_settings()
    env_tokens = settings.get_slack_tokens()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    filled, skipped_present, skipped_no_env = 0, 0, 0
    async with sf() as db:
        agents = (await db.execute(select(AgentRegistry))).scalars().all()
        for a in agents:
            if _valid(a.slack_bot_token):
                skipped_present += 1
                continue
            env_tok = env_token_for(a.agent_id, env_tokens, os.environ)
            if not _valid(env_tok):
                skipped_no_env += 1
                continue
            logger.info("%s %s ← env token", "[dry-run] would fill" if dry_run else "filling", a.agent_id)
            if not dry_run:
                a.slack_bot_token = env_tok
            filled += 1
        if not dry_run:
            await db.commit()

    await engine.dispose()
    logger.info(
        "Done. %s %d agent(s); %d already had a DB token; %d have no valid env token.",
        "would fill" if dry_run else "filled", filled, skipped_present, skipped_no_env,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
