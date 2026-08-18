"""Seed the cohort membership of record from cohorts.json.

Creates the cohorts named in the manifest and adds their members. Idempotent:
running it twice is a no-op. Additive by default — a membership present in the
database but absent from the manifest is reported and kept, and only deleted if
you pass --prune.

This does NOT enable the interaction gate. `cohort_isolation_enabled` is a
separate setting, default False, read by a running `agent-run` through an
lru_cached get_settings(); flipping it needs the container recreated, not just
restarted. See docs/specs/2026-08-18-cohort-seeding-design.md §1.1 for why
enabling it against the current 33-agent roster would gate nothing.

`scripts/` is baked into the image, not bind-mounted, so a freshly added script
must be copied in before it can run:

    docker cp scripts/seed_cohorts.py copi-python-app-1:/app/scripts/
    docker cp cohorts.json copi-python-app-1:/app/
    docker compose exec -T app python scripts/seed_cohorts.py --dry-run

Drop --dry-run to apply.
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
from src.models import AgentRegistry, Cohort, CohortMembership
from src.services.cohort_seed import apply_plan, load_manifest, plan_seed, validate_manifest

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("seed_cohorts")


async def _run(manifest_path: Path, dry_run: bool, prune: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        manifest = load_manifest(manifest_path)
        async with factory() as db:
            known = {aid for (aid,) in await db.execute(select(AgentRegistry.agent_id))}
            errors = validate_manifest(manifest, known)
            if errors:
                logger.error("Manifest is invalid. NOTHING was written:")
                for err in errors:
                    logger.error("  %s", err)
                return 1

            existing_cohorts = {n for (n,) in await db.execute(select(Cohort.name))}
            rows = (await db.execute(
                select(Cohort.name, CohortMembership.agent_id)
                .join(CohortMembership, CohortMembership.cohort_id == Cohort.id)
            )).all()
            existing_memberships = {(n, a) for n, a in rows}

            plan = plan_seed(manifest, existing_cohorts, existing_memberships)

            print("\n=== Seed plan ===")
            print(f"cohorts to create      : {len(plan.cohorts_to_create)}")
            for name in plan.cohorts_to_create:
                size = len(manifest["cohorts"][name]["members"])
                print(f"    + {name}  ({size} members)")
            print(f"memberships to add     : {len(plan.memberships_to_add)}")
            for name, agent_id in plan.memberships_to_add:
                print(f"    + {name}/{agent_id}")
            print(f"in DB, not in manifest : {len(plan.extra_memberships)}")
            for name, agent_id in plan.extra_memberships:
                suffix = " -> WILL DELETE" if prune else " (kept; --prune to delete)"
                print(f"    ? {name}/{agent_id}{suffix}")

            if dry_run:
                print("\n[dry-run] nothing written.")
                return 0
            if plan.is_noop and not (prune and plan.extra_memberships):
                print("\nAlready seeded; nothing to do.")
                return 0

            await apply_plan(db, manifest, plan, prune=prune)
            await db.commit()
            print(
                f"\nApplied: {len(plan.cohorts_to_create)} cohort(s) created, "
                f"{len(plan.memberships_to_add)} membership(s) added"
                + (f", {len(plan.extra_memberships)} pruned." if prune else ".")
            )
            print("The interaction gate is unchanged (isolation stays off).")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", default="cohorts.json", help="Manifest path (default: cohorts.json)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing")
    parser.add_argument("--prune", action="store_true", help="Delete memberships absent from the manifest")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(Path(args.manifest), args.dry_run, args.prune)))


if __name__ == "__main__":
    main()
