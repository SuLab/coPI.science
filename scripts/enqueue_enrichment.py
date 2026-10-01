"""Enqueue enrich_grants / industry_evidence jobs for existing PIs. Preview by default.

Jobs go in through the idempotent helper at bulk priority, so a re-run adds nothing and
interactive work is claimed first.

  docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/enqueue_enrichment.py --apply
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import ResearcherProfile, User  # noqa: E402
from src.models.job import BULK_PRIORITY  # noqa: E402
from src.services.job_queue import insert_job_if_absent  # noqa: E402


async def enqueue_for_all(db, *, apply: bool, only: str | None, orcid: str | None) -> int:
    q = select(User).join(ResearcherProfile, ResearcherProfile.user_id == User.id).where(User.user_role == "pi")
    if orcid:
        q = q.where(User.orcid == orcid)
    users = (await db.execute(q)).scalars().all()
    types = {"grants": ["enrich_grants"], "industry": ["industry_evidence"], None: ["enrich_grants", "industry_evidence"]}[only]
    for u in users:
        print(f"{'ENQUEUE' if apply else 'would enqueue'} {types} for {u.name} ({u.orcid})")
        if apply:
            for t in types:
                await insert_job_if_absent(
                    db, type=t, user_id=u.id, payload={"user_id": str(u.id), "orcid": u.orcid},
                    priority=BULK_PRIORITY,
                )
    if apply:
        await db.commit()
    return len(users)


async def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--only", choices=["grants", "industry"])
    p.add_argument("--orcid")
    a = p.parse_args()
    async with get_session_factory()() as db:
        n = await enqueue_for_all(db, apply=a.apply, only=a.only, orcid=a.orcid)
    print(f"{n} PI(s) {'enqueued' if a.apply else 'previewed'}")


if __name__ == "__main__":
    asyncio.run(_main())
