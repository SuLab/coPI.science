"""Backfill publications rows from a curated agent_id -> PMID mapping.

Issue #29 rollout prerequisite: eleven active labs have zero publications
rows because the only ingest path (profile pipeline: ORCID works -> PMID)
found nothing for them — their ORCID profiles list no works — so the
fail-closed authorship guard mutes every first-person paper claim they make.
PubMed author search cannot disambiguate names like Wu or Wilson reliably,
so the input here is a human-curated JSON mapping:

    {"good": ["21234567", "31234567"], "cravatt": ["19876543"]}

Usage (inside the app container, dry run first):

    docker compose exec app python scripts/backfill_publications.py --file data/backfill_pmids.json
    docker compose exec app python scripts/backfill_publications.py --file data/backfill_pmids.json --apply

Rows are visible to the running simulation on its next ~30s roster sync
(_load_publication_records) — no restart needed.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (agent_id, action, pmid) — action is one of:
#   would-insert / insert / skip-existing / error-no-agent / error-no-record
ReportEntry = tuple[str, str, str]


async def backfill(db, mapping: dict[str, list[str]], fetch=None, apply: bool = False) -> list[ReportEntry]:
    """Insert Publication rows for each agent's curated PMIDs.

    Idempotent: PMIDs the user already has are skipped (and not fetched).
    Dry run (the default) reports what WOULD be inserted and writes nothing.
    """
    from sqlalchemy import select

    from src.models import AgentRegistry, Publication
    from src.services.pubmed import fetch_pubmed_records, normalize_doi

    if fetch is None:
        fetch = fetch_pubmed_records

    report: list[ReportEntry] = []
    for agent_id, pmids in mapping.items():
        row = (
            await db.execute(
                select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        if row is None or row.user_id is None:
            report.append((agent_id, "error-no-agent", ""))
            continue

        existing = {
            p
            for (p,) in (
                await db.execute(
                    select(Publication.pmid).where(Publication.user_id == row.user_id)
                )
            ).all()
            if p
        }
        wanted = [str(p).strip() for p in pmids if str(p).strip()]
        missing: list[str] = []
        for pmid in wanted:
            if pmid in existing:
                report.append((agent_id, "skip-existing", pmid))
            else:
                missing.append(pmid)
        if not missing:
            continue

        records = {r["pmid"]: r for r in await fetch(missing) if r.get("pmid")}
        for pmid in missing:
            rec = records.get(pmid)
            if rec is None:
                report.append((agent_id, "error-no-record", pmid))
                continue
            if apply:
                db.add(
                    Publication(
                        user_id=row.user_id,
                        pmid=pmid,
                        pmcid=rec.get("pmcid"),
                        doi=normalize_doi(rec.get("doi")),
                        title=rec.get("title", ""),
                        abstract=rec.get("abstract", ""),
                        journal=rec.get("journal"),
                        year=rec.get("year"),
                    )
                )
                report.append((agent_id, "insert", pmid))
            else:
                report.append((agent_id, "would-insert", pmid))
    if apply:
        await db.flush()
    return report


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", required=True, help="JSON file: {agent_id: [pmid, ...]}")
    ap.add_argument("--apply", action="store_true", help="Write rows (default: dry run)")
    args = ap.parse_args()

    mapping = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        print("Input must be a JSON object mapping agent_id -> [pmid, ...]")
        return 2

    from src.database import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        report = await backfill(db, mapping, apply=args.apply)
        if args.apply:
            await db.commit()

    for agent_id, action, pmid in report:
        print(f"{agent_id}: {action} {pmid}".rstrip())
    inserted = sum(1 for _, a, _ in report if a == "insert")
    planned = sum(1 for _, a, _ in report if a == "would-insert")
    errors = sum(1 for _, a, _ in report if a.startswith("error"))
    if args.apply:
        print(f"\nInserted {inserted} rows ({errors} errors). The running simulation")
        print("picks them up on its next ~30s roster sync — no restart needed.")
    else:
        print(f"\nDry run: {planned} rows would be inserted ({errors} errors).")
        print("Re-run with --apply to write them.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
