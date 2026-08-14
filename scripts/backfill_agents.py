"""Backfill AgentRegistry rows + export profile markdown for users that have
a populated ResearcherProfile but no agent yet.

This is the missing step between `cli seed-profiles` (which creates User +
ResearcherProfile via the worker pipeline) and the rest of the agent system
(which requires an AgentRegistry row to participate in Slack and to have the
profile exported to profiles/public/{agent_id}.md).

Usage (inside the app container):

    docker compose cp scripts/backfill_agents.py app:/app/scripts/
    docker compose exec app python scripts/backfill_agents.py \
        --orcids data/cohorts/newuserlist01_orcids.txt

The --orcids file is the same format as orcids.txt: one ORCID per line,
'# Name' comment lines OK. The script processes ORCIDs in file order so the
first listed user gets the bare last-name slug when there's a collision.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_agents")


def _slugify_last_name(name: str) -> str:
    last = name.strip().split()[-1].lower()
    return "".join(c for c in last if c.isalpha()) or "lab"


async def _resolve_agent_id(name: str, db: AsyncSession) -> str:
    """Same collision logic as scripts/generate_sparsedata_user.py + agent_page.py.

    Order: bare last name → first-initial prefix → numeric suffix.
    """
    base = _slugify_last_name(name)
    candidate = base
    coll = await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id == candidate))
    if coll.scalar_one_or_none() is None:
        return candidate
    initial = name.strip()[0].lower() if name.strip() else "x"
    candidate = f"{initial}{base}"
    coll = await db.execute(select(AgentRegistry).where(AgentRegistry.agent_id == candidate))
    if coll.scalar_one_or_none() is None:
        return candidate
    for i in range(2, 20):
        candidate = f"{base}{i}"
        coll = await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == candidate)
        )
        if coll.scalar_one_or_none() is None:
            return candidate
    raise RuntimeError(f"Could not find unique agent_id for {name!r}")


def _bot_name_for(agent_id: str, name: str) -> str:
    last = name.strip().split()[-1]
    last_alpha = "".join(c for c in last if c.isalpha())
    if agent_id.lower() == last_alpha.lower():
        return f"{last_alpha.capitalize()}Bot"
    return f"{agent_id[0].upper()}{last_alpha.capitalize()}Bot"


def _parse_orcids_file(path: Path) -> list[str]:
    orcids: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        orcids.append(line)
    return orcids


async def _backfill_one(orcid: str, db: AsyncSession) -> str:
    """Returns a short status string for the audit line."""
    user_q = await db.execute(select(User).where(User.orcid == orcid))
    user = user_q.scalar_one_or_none()
    if not user:
        return f"no_user (orcid={orcid})"

    agent_q = await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user.id))
    agent = agent_q.scalar_one_or_none()
    if agent:
        return f"already_has_agent ({user.name} → {agent.agent_id})"

    profile_q = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )
    profile = profile_q.scalar_one_or_none()
    if not profile or not (profile.research_summary or "").strip():
        return f"no_profile ({user.name}) — run the worker pipeline first"

    agent_id = await _resolve_agent_id(user.name, db)
    bot_name = _bot_name_for(agent_id, user.name)
    db.add(AgentRegistry(
        agent_id=agent_id,
        user_id=user.id,
        bot_name=bot_name,
        pi_name=user.name,
        status="pending",
    ))
    await db.flush()

    pubs_q = await db.execute(select(Publication).where(Publication.user_id == user.id))
    pubs = pubs_q.scalars().all()
    exported = export_profile_to_markdown(user, profile, agent_id, publications=pubs)
    md_marker = exported.name if exported else "(no md written)"
    return f"created agent {agent_id} / {bot_name} ({user.name}) → {md_marker}"


async def _run(orcids_path: Path) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    orcids = _parse_orcids_file(orcids_path)
    logger.info("Backfilling agents for %d ORCIDs from %s", len(orcids), orcids_path)

    created = 0
    skipped = 0
    errors = 0
    async with factory() as db:
        for orcid in orcids:
            try:
                status = await _backfill_one(orcid, db)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.exception("Failed on %s", orcid)
                errors += 1
                status = f"error: {exc}"
            if status.startswith("created"):
                created += 1
            else:
                skipped += 1
            logger.info("%s — %s", orcid, status)

    await engine.dispose()
    logger.info("Created: %d, skipped: %d, errors: %d", created, skipped, errors)
    if created:
        # The DB (AgentRegistry) is now the source of truth — no PILOT_LABS edit
        # needed. New agents are created status='pending'; provision a Slack
        # token from the admin approve page to activate them.
        logger.info(
            "%d agent(s) created as status='pending'. Provision a Slack token "
            "from the admin approve page to activate each.", created,
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orcids", required=True, help="Path to orcids text file")
    args = parser.parse_args()

    path = Path(args.orcids)
    if not path.exists():
        logger.error("ORCID list not found: %s", path)
        sys.exit(1)

    sys.exit(asyncio.run(_run(path)))


if __name__ == "__main__":
    main()
