"""Export the agent roster from AgentRegistry to data/agent_roster.json.

The batch provisioning script (scripts/provision_slack_bots.py) runs on the
HOST, where it can neither import the ORM nor reach the postgres container.
This script runs INSIDE the container and writes a small JSON the host script
can read.

Token *values* are never written — only a ``has_token`` boolean — so secrets
don't land on disk.

Usage (inside the app container / on the compose network):

    docker run --rm --network copi-python_default \\
        -v "$PWD":/work -w /work copi-python-app \\
        python scripts/export_agent_roster.py

    # or simply:
    docker compose exec app python scripts/export_agent_roster.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# Prefer the mounted project root over any copy of `src` baked into
# site-packages (the image installs src/ non-editable), so this picks up
# freshly-added modules like src.services.slack_tokens.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry
from src.services.slack_tokens import is_valid_token

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("export_agent_roster")

OUTPUT_PATH = Path("data/agent_roster.json")


async def main() -> None:
    settings = get_settings()
    env_tokens = settings.get_slack_tokens()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async with sf() as db:
        rows = (await db.execute(
            select(
                AgentRegistry.agent_id,
                AgentRegistry.bot_name,
                AgentRegistry.pi_name,
                AgentRegistry.status,
                AgentRegistry.slack_bot_token,
            ).order_by(AgentRegistry.agent_id)
        )).all()
    await engine.dispose()

    roster = [
        {
            "id": r.agent_id,
            "name": r.bot_name,
            "pi": r.pi_name,
            "status": r.status,
            # DB token first, then the legacy .env mapping.
            "has_token": is_valid_token(r.slack_bot_token)
            or is_valid_token(env_tokens.get(r.agent_id, "")),
        }
        for r in rows
    ]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(roster, indent=2))
    logger.info(
        "Wrote %d agents to %s (%d with a token).",
        len(roster), OUTPUT_PATH, sum(1 for a in roster if a["has_token"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
