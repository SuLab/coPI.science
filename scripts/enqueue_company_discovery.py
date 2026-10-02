"""Enqueue a company_discovery job for every PI with an agent. Preview by default.

Spec §7.5 (Job) and §9 step 6: jobs go in through the idempotent helper at bulk
priority, so a re-run adds nothing, a PI already queued or running is left alone, and
interactive work (a Find companies press) is claimed first. Discovery needs
`SEC_USER_AGENT` set on the worker for funding and Wikidata; without it every
suggestion says "funding lookup unavailable".

  docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/enqueue_company_discovery.py
  docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/enqueue_company_discovery.py --apply
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, User  # noqa: E402
from src.models.job import BULK_PRIORITY  # noqa: E402
from src.services.company_discovery import enqueue_company_discovery  # noqa: E402


async def enqueue_for_all(db, *, apply: bool, orcid: str | None = None) -> int:
    """Preview (or, with `apply`, enqueue and commit) discovery for every user that has
    an AgentRegistry row, optionally only the one with `orcid`. Returns how many PIs."""
    with_agent = select(AgentRegistry.user_id).where(AgentRegistry.user_id.isnot(None))
    q = select(User).where(User.id.in_(with_agent)).order_by(User.name)
    if orcid:
        q = q.where(User.orcid == orcid)
    users = (await db.execute(q)).scalars().all()
    for u in users:
        if not apply:
            print(f"would enqueue company_discovery for {u.name} ({u.orcid})")
            continue
        job_id = await enqueue_company_discovery(db, u.id, priority=BULK_PRIORITY)
        state = "ENQUEUED" if job_id else "already pending or processing"
        print(f"{state}: company_discovery for {u.name} ({u.orcid})")
    if apply:
        await db.commit()
    return len(users)


async def _main() -> None:
    p = argparse.ArgumentParser(description="Enqueue company discovery for every PI with an agent.")
    p.add_argument("--apply", action="store_true", help="write the jobs (default: preview only)")
    p.add_argument("--orcid", help="only the PI with this ORCID iD")
    a = p.parse_args()
    async with get_session_factory()() as db:
        n = await enqueue_for_all(db, apply=a.apply, orcid=a.orcid)
    print(f"{n} PI(s) {'processed' if a.apply else 'previewed'}")


if __name__ == "__main__":
    asyncio.run(_main())
