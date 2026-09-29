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

Every validity check, on the DB column and on the env values alike, is
``src.services.slack_tokens.is_valid_token`` — the same test the app applies
when it reads a token. A column value that test rejects (a ``REPLACE_ME``, a
user ``xoxp-`` token, the ``xoxe.`` config token pasted into the wrong key) is
treated as unfilled and replaced from a valid env token, and counted as
``replacing_invalid``; otherwise it would count as filled forever, and
``delete_user_account`` would later skip it as not a bot token, leaving any real
bot token unrevoked.

Idempotent: only writes rows whose ``slack_bot_token`` is null, blank, or
invalid, and only from a valid env token. Safe to re-run.

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
from src.services.slack_tokens import is_valid_token

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill_agent_tokens")


def env_token_for(agent_id: str, legacy: Mapping[str, str], environ: Mapping[str, str]) -> str:
    """Return a valid env token for ``agent_id``, or ``""`` if none is known.

    Prefers ``environ["SLACK_BOT_TOKEN_<AGENT_ID upper>"]`` (the key
    ``provision_slack_bots.py`` writes) when ``is_valid_token`` accepts it;
    otherwise ``legacy[agent_id]`` when that is valid; otherwise ``""``. A
    non-empty result is always valid, stripped of surrounding whitespace.
    """
    for tok in (environ.get(f"SLACK_BOT_TOKEN_{agent_id.upper()}"), legacy.get(agent_id)):
        if is_valid_token(tok):
            return tok.strip()
    return ""


def classify(db_token: str | None, env_tok: str) -> str:
    """Decide what to do with one row, given its column value and ``env_token_for``.

    ``"present"`` — the column already holds a valid token; leave it.
    ``"fill"`` — the column is null/blank and ``env_tok`` is valid; write it.
    ``"replacing_invalid"`` — the column holds a value ``is_valid_token``
    rejects and ``env_tok`` is valid; overwrite it.
    ``"no_env"`` — the column is not valid and there is no valid env token.
    """
    if is_valid_token(db_token):
        return "present"
    if not is_valid_token(env_tok):
        return "no_env"
    return "replacing_invalid" if db_token and db_token.strip() else "fill"


async def main(dry_run: bool) -> None:
    settings = get_settings()
    env_tokens = settings.get_slack_tokens()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    filled, replacing_invalid, skipped_present, skipped_no_env = 0, 0, 0, 0
    async with sf() as db:
        agents = (await db.execute(select(AgentRegistry))).scalars().all()
        for a in agents:
            env_tok = env_token_for(a.agent_id, env_tokens, os.environ)
            decision = classify(a.slack_bot_token, env_tok)
            if decision == "present":
                skipped_present += 1
                continue
            if decision == "no_env":
                if a.slack_bot_token and a.slack_bot_token.strip():
                    logger.warning(
                        "%s holds an invalid DB token and no valid env token; left as is",
                        a.agent_id,
                    )
                skipped_no_env += 1
                continue
            action = "replacing invalid DB token" if decision == "replacing_invalid" else "filling"
            logger.info("%s%s %s <- env token", "[dry-run] " if dry_run else "", action, a.agent_id)
            if not dry_run:
                a.slack_bot_token = env_tok
            if decision == "replacing_invalid":
                replacing_invalid += 1
            else:
                filled += 1
        if not dry_run:
            await db.commit()

    await engine.dispose()
    logger.info(
        "Done. %s %d agent(s); replacing_invalid=%d; %d already had a valid DB token; "
        "%d have no valid env token.",
        "would fill" if dry_run else "filled", filled, replacing_invalid,
        skipped_present, skipped_no_env,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
