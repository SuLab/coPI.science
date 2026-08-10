"""Scope the agent simulation to a cohort by flipping AgentRegistry.status.

The cohort is defined by one or more TSV files (default:
data/cohorts/newuserlist01.tsv + newuserlist02.tsv), each row being
`Name<TAB>ORCID[<TAB>Affiliation]` where ORCID may be 'null'/blank. The cohort
files hold third-party personal data and are deliberately kept out of git
(`data/` is gitignored), so pass --tsv explicitly on a fresh checkout.
Each row is resolved to an agent via the DB:
  1) match users.orcid (when a real ORCID is present), else
  2) match users.name (case-insensitive), else
  3) unique last-name match.

Effect (normal mode):
  - cohort agents            -> 'active'   (unblocks pending/suspended cohort members)
  - non-cohort agents that are currently 'active' -> 'inactive' (parked, reversible)
  - non-cohort 'pending'/'suspended' agents are left untouched

`main.py` (without --all-agents) runs only status='active' agents, so this makes
the next simulation run include exactly the cohort.

Run as a one-off container on the compose network with the live project mounted:

    docker run --rm --network copi-python_default \\
        -v /home/ubuntu/copi-python:/work -w /work \\
        copi-python-app python scripts/set_cohort_active.py --dry-run

Drop --dry-run to apply. Use --restore to undo (flip every 'inactive' -> 'active').
A JSON snapshot of all (agent_id, status) is written under data/ before any change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("set_cohort_active")


def _parse_tsv_entries(paths: list[Path]) -> list[tuple[str, str]]:
    """Return (name, orcid) pairs from the TSV files. orcid may be '' (treat null/blank as empty)."""
    entries: list[tuple[str, str]] = []
    for p in paths:
        if not p.exists():
            logger.error("TSV not found: %s", p)
            sys.exit(1)
        for raw in p.read_text().splitlines():
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            name = cols[0].strip()
            orcid = cols[1].strip() if len(cols) > 1 else ""
            if orcid.lower() == "null":
                orcid = ""
            if name:
                entries.append((name, orcid))
    return entries


async def _load_agents(db: AsyncSession) -> list[tuple[str, str, str, str]]:
    """Return [(agent_id, status, orcid, name_lower), ...] for every agent joined to its user."""
    rows = await db.execute(
        select(AgentRegistry.agent_id, AgentRegistry.status, User.orcid, User.name)
        .join(User, AgentRegistry.user_id == User.id)
    )
    out = []
    for agent_id, status, orcid, name in rows:
        out.append((agent_id, status, (orcid or ""), (name or "").strip().lower()))
    return out


def _resolve_cohort(entries: list[tuple[str, str]], agents: list[tuple[str, str, str, str]]) -> tuple[set[str], list[tuple[str, str]]]:
    """Map TSV entries to agent_ids. Returns (cohort_agent_ids, unmatched_entries)."""
    by_orcid = {a[2]: a[0] for a in agents if a[2]}
    by_name = {a[3]: a[0] for a in agents}
    cohort: set[str] = set()
    unmatched: list[tuple[str, str]] = []
    for name, orcid in entries:
        aid = None
        if orcid and orcid in by_orcid:
            aid = by_orcid[orcid]
        if aid is None:
            aid = by_name.get(name.lower())
        if aid is None:
            last = name.split()[-1].lower() if name.split() else ""
            cands = [a[0] for a in agents if a[3].split() and a[3].split()[-1] == last]
            if len(cands) == 1:
                aid = cands[0]
        if aid:
            cohort.add(aid)
        else:
            unmatched.append((name, orcid))
    return cohort, unmatched


def _write_snapshot(agents: list[tuple[str, str, str, str]], snapshot_dir: Path) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"agent_status_snapshot_{ts}.json"
    path.write_text(json.dumps({a[0]: a[1] for a in agents}, indent=2, sort_keys=True))
    return path


async def _run(tsv_paths: list[Path], dry_run: bool, restore: bool, snapshot_dir: Path) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            agents = await _load_agents(db)
            snap = _write_snapshot(agents, snapshot_dir)
            logger.info("Snapshot of %d agent statuses -> %s", len(agents), snap)

            if restore:
                to_activate = [a[0] for a in agents if a[1] == "inactive"]
                logger.info("RESTORE: %d 'inactive' agents -> 'active'", len(to_activate))
                for aid in sorted(to_activate):
                    logger.info("  reactivate %s", aid)
                if not dry_run and to_activate:
                    res = await db.execute(select(AgentRegistry).where(AgentRegistry.status == "inactive"))
                    for ag in res.scalars():
                        ag.status = "active"
                    await db.commit()
                logger.info("%s%d agents reactivated", "[dry-run] " if dry_run else "", len(to_activate))
                return 0

            entries = _parse_tsv_entries(tsv_paths)
            cohort, unmatched = _resolve_cohort(entries, agents)
            logger.info("Cohort: %d agents resolved from %d TSV entries", len(cohort), len(entries))
            if unmatched:
                logger.warning("%d TSV entries did NOT resolve to an agent:", len(unmatched))
                for name, orcid in unmatched:
                    logger.warning("  NO AGENT: %s (orcid=%s)", name, orcid or "—")

            status_by_id = {a[0]: a[1] for a in agents}
            to_activate = sorted(aid for aid in cohort if status_by_id.get(aid) != "active")
            to_inactivate = sorted(a[0] for a in agents if a[0] not in cohort and a[1] == "active")
            already_active_cohort = sorted(aid for aid in cohort if status_by_id.get(aid) == "active")

            print("\n=== Planned status changes ===")
            print(f"cohort -> active        : {len(to_activate)} (were not active)")
            for aid in to_activate:
                print(f"    + {aid:14s} ({status_by_id.get(aid)} -> active)")
            print(f"cohort already active   : {len(already_active_cohort)} -> {', '.join(already_active_cohort)}")
            print(f"non-cohort -> inactive  : {len(to_inactivate)} (parked)")
            for aid in to_inactivate:
                print(f"    - {aid}")
            final_active = len(cohort)
            print(f"\nResulting active agents : {final_active}")

            if dry_run:
                print("\n[dry-run] no changes written.")
                return 0
            if not to_activate and not to_inactivate:
                print("\nNo changes needed (already scoped to cohort).")
                return 0

            res = await db.execute(select(AgentRegistry))
            for ag in res.scalars():
                if ag.agent_id in cohort:
                    if ag.status != "active":
                        ag.status = "active"
                elif ag.status == "active":
                    ag.status = "inactive"
            await db.commit()
            print(f"\nApplied: {len(to_activate)} activated, {len(to_inactivate)} parked. Active total = {final_active}.")
            print("Restart the simulation to apply (see CLAUDE.md). Undo with --restore.")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tsv", nargs="+",
                        default=["data/cohorts/newuserlist01.tsv", "data/cohorts/newuserlist02.tsv"],
                        help="Cohort TSV file(s) (default: data/cohorts/newuserlist01.tsv "
                             "data/cohorts/newuserlist02.tsv; not in git — see module docstring)")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes; write nothing")
    parser.add_argument("--restore", action="store_true", help="Undo: flip every 'inactive' agent back to 'active'")
    parser.add_argument("--snapshot-dir", default="data", help="Directory for the pre-change status snapshot (default: data)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run([Path(p) for p in args.tsv], args.dry_run, args.restore, Path(args.snapshot_dir))))


if __name__ == "__main__":
    main()
