"""Delete publication rows where the DOI prefix doesn't match the journal.

Uses the same _DOI_PUBLISHER_PATTERNS as src/services/profile_export.py
(`_validate_doi_journal`), which currently only warns; this script deletes.

--orcids-file is one ORCID per line ('# Name' comment lines OK). Those lists are
personal data, so they live in gitignored data/ (see data/cohorts/README.md) and
are not shipped with the repo — pass whichever list you need.

Usage:
    docker cp scripts/cleanup_doi_mismatches.py copi-python-app-1:/app/scripts/
    docker exec copi-python-app-1 python scripts/cleanup_doi_mismatches.py \\
        --orcids-file data/cohorts/<your-orcid-list>.txt [--dry-run]
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
from src.models import Publication, User
from src.services.profile_export import _validate_doi_journal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("doi_cleanup")


def _is_mismatch(doi: str | None, journal: str | None) -> bool:
    """True iff DOI prefix is in our known patterns AND journal doesn't match.

    We trust _validate_doi_journal as the source of truth; it returns True for
    OK / inconclusive and False for clear mismatch. This wrapper flips that
    into "is_mismatch".
    """
    if not doi or not journal:
        return False
    # _validate_doi_journal returns False only when there's a clear mismatch.
    return not _validate_doi_journal(doi, journal)


async def _process_user(db: AsyncSession, user: User, dry_run: bool) -> tuple[int, int, list[str]]:
    """Returns (before_count, after_count, deleted_pmids)."""
    pubs_q = await db.execute(
        select(Publication).where(Publication.user_id == user.id)
    )
    pubs = pubs_q.scalars().all()
    before = len(pubs)
    to_delete: list[Publication] = []
    for pub in pubs:
        if _is_mismatch(pub.doi, pub.journal):
            to_delete.append(pub)
    deleted_pmids = [p.pmid or f"id={p.id}" for p in to_delete]
    if not dry_run and to_delete:
        for pub in to_delete:
            await db.delete(pub)
        await db.flush()
    after = before - len(to_delete)
    return before, after, deleted_pmids


async def _run(orcids: list[str], dry_run: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    flag_threshold = 5
    rows: list[tuple[str, int, int]] = []
    deletions_by_user: dict[str, list[str]] = {}

    async with factory() as db:
        for orcid in orcids:
            user_q = await db.execute(select(User).where(User.orcid == orcid))
            user = user_q.scalar_one_or_none()
            if not user:
                logger.warning("No user for ORCID %s", orcid)
                continue
            before, after, deleted = await _process_user(db, user, dry_run)
            rows.append((user.name, before, after))
            if deleted:
                deletions_by_user[user.name] = deleted
                logger.info(
                    "%s: %d → %d (deleted %d: %s)",
                    user.name, before, after, len(deleted),
                    ", ".join(deleted[:5]) + (" ..." if len(deleted) > 5 else ""),
                )
            else:
                logger.info("%s: %d (no mismatches)", user.name, before)
        if not dry_run:
            await db.commit()

    await engine.dispose()

    # Report
    print("\n=== DOI/journal mismatch cleanup summary ===")
    print(f"{'Name':30s} {'Before':>7s} {'After':>7s}  Deleted")
    for name, before, after in sorted(rows):
        marker = " ⚠️ <5" if after < flag_threshold else ""
        print(f"{name:30s} {before:>7d} {after:>7d}  {before-after}{marker}")

    flagged = [(name, after) for name, before, after in rows if after < flag_threshold]
    if flagged:
        print(f"\n=== ⚠️  Users with <{flag_threshold} publications after cleanup ===")
        for name, n in flagged:
            print(f"  - {name}: {n}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orcids-file", required=True,
                        help="File with one ORCID per line (# comments OK)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually delete; just report")
    args = parser.parse_args()

    path = Path(args.orcids_file)
    if not path.exists():
        logger.error("ORCID list not found: %s", path)
        sys.exit(1)
    orcids = [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    logger.info("Scanning %d users (dry_run=%s)", len(orcids), args.dry_run)
    sys.exit(asyncio.run(_run(orcids, args.dry_run)))


if __name__ == "__main__":
    main()
