"""Ensure every pi_lab agent has its hub-and-spoke cohort (audited, idempotent).

The logic lives in src/services/star_topology.py (tested by
tests/integration/test_star_topology.py); this is the CLI. Dry-run is the
default — nothing is written without --apply. Run it after adding PIs and
before starting a simulation run: the engine hard-fails startup for any live
pi_lab without a spoke.

Usage (inside the app container):

    docker compose -f docker-compose.prod.yml exec blackbird-app \
        python scripts/ensure_star_spokes.py                # dry run: the plan
    docker compose -f docker-compose.prod.yml exec blackbird-app \
        python scripts/ensure_star_spokes.py --apply \
        --actor-email admin@example.org                     # write + audit

--actor-email attributes the cohort_audit_events rows (and Cohort.created_by /
CohortMembership.added_by) to an existing user; omitted, the rows carry no
actor, which is how the audit trail marks "a script did this".

Exit codes: 0 clean; 1 anomalies reported (rows needing a human); 2 refused
(e.g. not exactly one scout_hub on the roster).
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import User
from src.services.star_topology import ensure_star_spokes


async def _run(apply: bool, actor_email: str | None) -> int:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            actor = None
            if actor_email:
                actor = (
                    await db.execute(select(User).where(User.email == actor_email))
                ).scalar_one_or_none()
                if actor is None:
                    print(f"REFUSED: no user with email {actor_email!r}")
                    return 2

            try:
                report = await ensure_star_spokes(db, apply=apply, actor=actor)
            except ValueError as exc:
                print(f"REFUSED: {exc}")
                return 2

            banner = "APPLIED" if apply else "DRY RUN (nothing written; use --apply)"
            print(f"== ensure_star_spokes — {banner}")
            for name in report.created_cohorts:
                print(f"create cohort   {name}")
            for name, member in report.added_members:
                print(f"add member      {name} += {member}")
            print(
                f"{len(report.created_cohorts)} cohort(s) to create, "
                f"{len(report.added_members)} membership(s) to add, "
                f"{len(report.complete)} spoke(s) already complete"
            )
            for anomaly in report.anomalies:
                print(f"ANOMALY: {anomaly}")

            if apply:
                await db.commit()
                print("committed.")
            return 1 if report.anomalies else 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="write the missing cohorts/memberships (default: dry run)",
    )
    parser.add_argument(
        "--actor-email", default=None,
        help="attribute the audit rows to this existing user",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.apply, args.actor_email)))


if __name__ == "__main__":
    main()
