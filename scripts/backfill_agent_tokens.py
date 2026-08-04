"""One-time backfill: copy each agent's Slack bot token from the legacy
``.env`` / ``config.get_slack_tokens()`` mapping into the
``AgentRegistry.slack_bot_token`` DB column.

The simulation now reads tokens from the DB column (so newly-activated agents go
live without a restart). Run this once BEFORE deploying that change, or existing
active agents will start with a null token and be skipped.

Idempotent: only fills rows whose ``slack_bot_token`` is currently null/blank,
and only from a valid (non-placeholder) env token. Safe to re-run.

Usage (inside the app container):

    docker compose exec app python scripts/backfill_agent_tokens.py
    # preview only:
    docker compose exec app python scripts/backfill_agent_tokens.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Prefer the mounted project root over any baked-in copy of `src` in
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
            env_tok = env_tokens.get(a.agent_id, "")
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
