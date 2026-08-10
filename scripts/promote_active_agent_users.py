"""Promote users who have an ACTIVE agent to access_status='allowed'.

Several PIs were batch-seeded for the agent simulation: their AgentRegistry row
is status='active' (bot is live), but their User.access_status was never moved
past 'pending'. That makes them show up as pending access requests in the admin
UI even though their agent is already running.

This script finds every user linked to an active agent whose access_status is
not 'allowed' and promotes them. It deliberately does NOT touch users without an
active agent (genuine human access requests are left for manual review).

Usage:
    python -m scripts.promote_active_agent_users            # dry run (default)
    python -m scripts.promote_active_agent_users --apply     # write changes
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, User


async def _run(apply: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        rows = (
            await db.execute(
                select(User, AgentRegistry)
                .join(AgentRegistry, AgentRegistry.user_id == User.id)
                .where(AgentRegistry.status == "active")
                .where(User.access_status != "allowed")
                .order_by(User.name)
            )
        ).all()

        if not rows:
            print("Nothing to do: all active-agent users are already 'allowed'.")
            await engine.dispose()
            return 0

        print(f"{'Would promote' if not apply else 'Promoting'} {len(rows)} user(s):")
        for user, agent in rows:
            print(
                f"  {user.name!r:30} orcid={user.orcid:20} "
                f"{user.access_status} -> allowed   (agent={agent.agent_id})"
            )
            if apply:
                user.access_status = "allowed"

        if apply:
            await db.commit()
            print(f"\nDone. {len(rows)} user(s) set to access_status='allowed'.")
        else:
            print("\nDry run — no changes written. Re-run with --apply to commit.")

    await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default is dry run)."
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
