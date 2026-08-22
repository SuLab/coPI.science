#!/usr/bin/env python3
"""Print domain-based Slack install links for agents that have no bot token.

Why this exists
---------------
``scripts/provision_slack_bots.py`` hardcodes ``redirect_uri`` to
``http://localhost:8888/oauth/callback`` and binds its callback server to
127.0.0.1, so its links only complete OAuth from a browser *on the host* (or
through an SSH tunnel). This script instead reuses the web flow that the admin
"Provision" button uses — ``src.services.admin_provisioning.start_provisioning``
— whose redirect target is::

    {BASE_URL}/admin/agents/slack/callback

so the links work from any browser. No callback server runs here: the already
deployed web app receives the redirect and stores the token on the
``AgentRegistry`` row directly (the DB is authoritative), which also means there
is no ``.env`` write and no ``backfill_agent_tokens.py`` step afterwards.

Each call creates a real Slack app and persists a ``SlackAppProvision`` bridge
row keyed by a random ``state``; re-running for the same agent drops that
agent's previous bridge row, so the newest link is the only live one.

The redirect lands on an admin-only route, so open the links **while signed in
to {BASE_URL} as an admin**. If you are not, the 302 to ``/login`` preserves
``code`` and ``state`` in ``next``, so provisioning still completes after login —
but OAuth codes are short-lived, so signing in first is safer.

Usage (must run INSIDE the app container — needs DB + src + config token):

    docker compose -f docker-compose.prod.yml cp \\
        scripts/make_install_links.py blackbird-app:/app/scripts/
    docker compose -f docker-compose.prod.yml exec -T blackbird-app \\
        python scripts/make_install_links.py [--only a,b] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry
from src.services.admin_provisioning import (
    CALLBACK_PATH,
    ProvisioningError,
    start_provisioning,
)

PROVISIONABLE_STATUSES = ("active", "pending")


async def _run(only: set[str] | None, dry_run: bool) -> int:
    settings = get_settings()
    redirect_uri = f"{settings.base_url.rstrip('/')}{CALLBACK_PATH}"
    print(f"redirect_uri: {redirect_uri}\n")

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as db:
        rows = (await db.execute(
            select(AgentRegistry)
            .where(AgentRegistry.status.in_(PROVISIONABLE_STATUSES))
            .where(or_(
                AgentRegistry.slack_bot_token.is_(None),
                AgentRegistry.slack_bot_token == "",
            ))
            .order_by(AgentRegistry.agent_id)
        )).scalars().all()

        if only:
            unknown = only - {a.agent_id for a in rows}
            if unknown:
                print(f"warning: not token-less/provisionable, ignoring: {sorted(unknown)}")
            rows = [a for a in rows if a.agent_id in only]

        if not rows:
            print("No agents need a token.")
            return 0

        print(f"{len(rows)} agent(s) need a token: {', '.join(a.agent_id for a in rows)}\n")
        if dry_run:
            print("--dry-run: no Slack apps created, no links minted.")
            return 0

        failures = 0
        for agent in rows:
            try:
                url = await start_provisioning(db, agent)
            except ProvisioningError as exc:
                failures += 1
                print(f"### {agent.bot_name} ({agent.agent_id})\n    FAILED: {exc}\n")
                continue
            print(f"### {agent.bot_name}  ({agent.agent_id} — {agent.pi_name})")
            print(f"{url}\n")

        print(f"Done: {len(rows) - failures} link(s) minted, {failures} failed.")
        return 1 if failures else 0

    await engine.dispose()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", default="", help="Comma-separated agent_ids to limit to")
    p.add_argument("--dry-run", action="store_true",
                   help="List who needs a token without creating Slack apps")
    args = p.parse_args()
    only = {s.strip().lower() for s in args.only.split(",") if s.strip()} or None
    sys.exit(asyncio.run(_run(only, args.dry_run)))


if __name__ == "__main__":
    main()
