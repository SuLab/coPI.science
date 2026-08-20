"""Seed the cohort membership of record from cohorts.json.

Creates the cohorts named in the manifest and adds their members. Idempotent:
running it twice is a no-op. Additive by default — a membership present in the
database but absent from the manifest is reported and kept, and only deleted if
you pass --prune. A changed `description` is reported (not silently ignored)
but never auto-applied; a cohort present in the DB but not named in the
manifest at all is also reported, purely for visibility, and is never a prune
target regardless of --prune.

This does NOT enable the interaction gate. `cohort_isolation_enabled` is a
separate setting, default False, read by a running `agent-run` through an
lru_cached get_settings(); flipping it needs the container recreated, not just
restarted. See docs/specs/2026-08-18-cohort-seeding-design.md §1.1 for why
enabling it against the current 33-agent roster would gate nothing.

`scripts/` and `cohorts.json` are baked into the app image, not bind-mounted, so
a code or manifest change needs a rebuild before a container can see it — and
`docker cp`-ing the files in is NOT a working substitute: the image also
`pip install`s `src/` into site-packages, so running `python scripts/seed_cohorts.py`
puts `scripts/` first on `sys.path`, and `import src.services.cohort_seed`
resolves to the STALE site-packages copy rather than whatever was just copied
in. The recipe that actually works runs from the host against the running
compose network, with the working tree mounted so `src` resolves under `/work`
instead of site-packages, and `-m` so `src` is importable as a package at all
(a bare script invocation does not add `/work` to `sys.path`):

    docker run --rm --network copi-python_default \\
      -v /home/ubuntu/copi-python:/work -w /work \\
      copi-python-app python -m scripts.seed_cohorts --dry-run

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
from src.services.cohorts import SERVICE_AGENT_IDS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("seed_cohorts")


async def _run_with_session(
    db: AsyncSession, manifest_path: Path, dry_run: bool, prune: bool
) -> int:
    """The validate -> plan -> apply core, taking an already-open session.

    Split out from `_run` so it is testable against the test suite's sandboxed
    `db_session` fixture without spinning up a real engine or invoking the CLI
    as a subprocess. `_run` is the thin wrapper that owns the real engine for
    an actual invocation; its behaviour is unchanged.
    """
    manifest = load_manifest(manifest_path)
    known = {aid for (aid,) in await db.execute(select(AgentRegistry.agent_id))}
    # Service bots (grantbot) legitimately hold memberships with no
    # AgentRegistry row — the engine never runs them, but their posts must pass
    # every cohort-mate's gate, so they are members of all three cohorts. Union
    # them into the known set rather than relaxing validate_manifest: the check
    # stays exact, so a typo'd member id ("grantbo") still aborts the seed.
    errors = validate_manifest(manifest, known | SERVICE_AGENT_IDS)
    if errors:
        logger.error("Manifest is invalid. NOTHING was written:")
        for err in errors:
            logger.error("  %s", err)
        return 1

    existing_cohorts = {n for (n,) in await db.execute(select(Cohort.name))}
    existing_descriptions: dict[str, str] = {
        n: d for n, d in await db.execute(select(Cohort.name, Cohort.description))
    }
    rows = (await db.execute(
        select(Cohort.name, CohortMembership.agent_id)
        .join(CohortMembership, CohortMembership.cohort_id == Cohort.id)
    )).all()
    existing_memberships = {(n, a) for n, a in rows}

    plan = plan_seed(
        manifest, existing_cohorts, existing_memberships, existing_descriptions
    )

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
    print(f"description drift      : {len(plan.description_drift)}")
    for name, db_desc, manifest_desc in plan.description_drift:
        print(
            f"    ! description differs for {name}: "
            f"db={db_desc!r} manifest={manifest_desc!r}"
        )
    print(f"not managed by manifest: {len(plan.unmanaged_cohorts)}")
    for name in plan.unmanaged_cohorts:
        print(f"    ~ {name}  (not named in this manifest; never a prune target)")

    if dry_run:
        print("\n[dry-run] nothing written.")
        return 0
    if plan.is_noop and not (prune and plan.extra_memberships):
        if plan.description_drift:
            print(
                "\nNo cohorts or memberships to add, but description drift "
                "exists (see above). Descriptions are not auto-updated -- "
                "edit the manifest to match the DB, or update the DB (e.g. via "
                "/admin/cohorts) to match the manifest."
            )
        else:
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


async def _run(manifest_path: Path, dry_run: bool, prune: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            return await _run_with_session(db, manifest_path, dry_run, prune)
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
