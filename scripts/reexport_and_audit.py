"""Re-export profile markdown for a list of users and audit pub counts.

For each ORCID in --orcids-file, this script:
1. Looks up the user + profile + agent_id.
2. Loads all their publications.
3. Calls export_profile_to_markdown (rewrites profiles/public/{agent_id}.md).
4. Logs the row to an audit table.

Flag threshold for "low publication count" is --min-pubs (default 5).

--orcids-file is one ORCID per line ('# Name' comment lines OK). Those lists are
personal data, so they live in gitignored data/ (see data/cohorts/README.md) and
are not shipped with the repo — pass whichever list you need.

Usage:
    docker exec copi-python-app-1 python scripts/reexport_and_audit.py \\
        --orcids-file data/cohorts/<your-orcid-list>.txt --min-pubs 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.services.profile_export import export_profile_to_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reexport_audit")


async def _process(orcid: str, db: AsyncSession) -> tuple[str, str, int, str] | None:
    user = (await db.execute(select(User).where(User.orcid == orcid))).scalar_one_or_none()
    if not user:
        logger.warning("No user for ORCID %s", orcid)
        return None
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )).scalar_one_or_none()
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )).scalar_one_or_none()
    pubs = (await db.execute(
        select(Publication).where(Publication.user_id == user.id)
    )).scalars().all()

    n_pubs = len(pubs)
    if not profile:
        return (user.name, "", n_pubs, "no_profile")
    if not agent:
        return (user.name, "", n_pubs, "no_agent")

    exported = export_profile_to_markdown(user, profile, agent.agent_id, publications=pubs)
    note = f"exported {exported.name}" if exported else "export_failed"
    return (user.name, agent.agent_id, n_pubs, note)


async def _run(orcids: list[str], min_pubs: int) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    rows: list[tuple[str, str, int, str]] = []
    async with factory() as db:
        for orcid in orcids:
            r = await _process(orcid, db)
            if r:
                rows.append(r)
                logger.info("%s [%s]: %d pubs — %s", r[0], r[1] or "?", r[2], r[3])

    await engine.dispose()

    print("\n=== Re-export & audit summary ===")
    print(f"{'Name':32s} {'AgentID':14s} {'Pubs':>5s}  Note")
    for name, agent_id, n_pubs, note in sorted(rows):
        marker = " ⚠️" if n_pubs < min_pubs else ""
        print(f"{name:32s} {agent_id:14s} {n_pubs:>5d}  {note}{marker}")

    flagged = [(n, a, c) for n, a, c, _ in rows if c < min_pubs]
    if flagged:
        print(f"\n=== ⚠️  Users with <{min_pubs} publications ({len(flagged)}) ===")
        for name, agent_id, count in flagged:
            print(f"  - {name} ({agent_id}): {count} publications")
    else:
        print(f"\nAll users have ≥{min_pubs} publications.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orcids-file", required=True)
    parser.add_argument("--min-pubs", type=int, default=5)
    args = parser.parse_args()
    path = Path(args.orcids_file)
    if not path.exists():
        logger.error("ORCID list not found: %s", path)
        sys.exit(1)
    orcids = [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    logger.info("Processing %d users", len(orcids))
    sys.exit(asyncio.run(_run(orcids, args.min_pubs)))


if __name__ == "__main__":
    main()
