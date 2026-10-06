"""Audit publication DOI links against PubMed (the periodic DOI validation pass).

For every publication (optionally filtered to specific users), this fetches the
authoritative DOI registered for its PMID from PubMed and compares it to the
stored DOI. A stored DOI that disagrees with its PMID's record points at a
different paper than the one cited — the failure mode behind the bad-link
incident (see GitHub issue #5).

Classifies each row and, with --fix, corrects the DB and re-exports the
affected public profiles one PI at a time: under that PI's persona writer locks
(`lock_persona_writer`) it re-reads and corrects the PI's rows and records a
`reexport` revision (`reexport_persona`, skipped when the persona file already
holds the text), commits, then runs the post-commit `write_persona_files`.

--fix also sets `publications.doi_verified` from the reconcile action (spec
2026-10-05 §6.3): True for ok / filled / corrected, False for unverified. The
export links a DOI only when it is True, and otherwise prefers the PubMed link.

Categories:
  ok           stored DOI matches the PMID's authoritative DOI
  verify       stored DOI is right but doi_verified is not set to match
  canonicalize stored matches but in non-canonical form (doi: prefix, etc.)
  corrected    stored DOI disagrees with the PMID's record  -> wrong link
  filled       no stored DOI; authoritative one is available
  unverified   PMID has no DOI on file (can't validate)     -> DOI left as-is;
               doi_verified set False
  no_pmid      publication has no PMID                       -> left as-is

Usage (runs inside the app container — needs DB + network):
    # Audit everyone (report only):
    docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/audit_pub_dois.py

    # Audit + fix specific users by ORCID:
    docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/audit_pub_dois.py \\
        --orcids 0000-0002-9943-7557 --fix

    # Audit + fix specific agents, or everyone:
    docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/audit_pub_dois.py --agents liu bollong --fix
    docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/audit_pub_dois.py --fix
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, Publication, User
from src.services.profile_publish import (
    lock_persona_writer,
    reexport_persona,
    write_persona_files,
)
from src.services.pubmed import fetch_authoritative_dois, reconcile_pub_doi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audit_pub_dois")

#: reconcile_pub_doi action -> the doi_verified value it establishes ("none": no DOI, so
#: nothing to verify).
_VERIFIED_BY_ACTION = {"ok": True, "filled": True, "corrected": True, "unverified": False}


async def _select_user_ids(
    db: AsyncSession, orcids: list[str], agents: list[str]
) -> set[uuid.UUID] | None:
    """Resolve the --orcids / --agents filters to a set of user_ids.

    Returns None when no filter is given (audit everyone).
    """
    if not orcids and not agents:
        return None
    ids: set[uuid.UUID] = set()
    if orcids:
        rows = (await db.execute(select(User.id).where(User.orcid.in_(orcids)))).all()
        ids.update(r[0] for r in rows)
    if agents:
        rows = (await db.execute(
            select(AgentRegistry.user_id).where(AgentRegistry.agent_id.in_(agents))
        )).all()
        ids.update(r[0] for r in rows)
    return ids


async def _run(orcids: list[str], agents: list[str], fix: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            user_ids = await _select_user_ids(db, orcids, agents)
            if user_ids is not None and not user_ids:
                print("No users matched the given --orcids/--agents filter.")
                return 0
            await audit(db, user_ids, fix)
    finally:
        await engine.dispose()
    return 0


async def audit(db: AsyncSession, user_ids: set[uuid.UUID] | None, fix: bool) -> Counter[str]:
    """Classify every publication of ``user_ids`` (None: everyone), print the report and,
    with ``fix``, apply the changes (``_fix_per_pi``). Returns the category counts."""
    q = select(Publication)
    if user_ids is not None:
        q = q.where(Publication.user_id.in_(user_ids))
    pubs = (await db.execute(q)).scalars().all()
    logger.info("Auditing %d publications...", len(pubs))

    pmids = sorted({p.pmid for p in pubs if p.pmid})
    logger.info("Fetching authoritative DOIs for %d unique PMIDs...", len(pmids))
    authoritative = await fetch_authoritative_dois(pmids)
    logger.info("Got DOIs for %d PMIDs from PubMed.", len(authoritative))

    counts: Counter[str] = Counter()
    changes: list[tuple[Publication, str | None, str]] = []  # (pub, new_doi, category)
    affected_users: set[uuid.UUID] = set()

    for p in pubs:
        if not p.pmid:
            counts["no_pmid"] += 1
            continue
        auth = authoritative.get(str(p.pmid))
        final_doi, action = reconcile_pub_doi(p.doi, auth)
        # Compare against the raw stored value: a write is needed only when
        # the value to store actually differs (so a clean DOI that merely
        # differs in case from esummary is left untouched).
        needs_write = action in ("corrected", "filled", "ok") and (
            (final_doi or None) != (p.doi or None))
        verified = _VERIFIED_BY_ACTION.get(action)
        needs_flag = verified is not None and p.doi_verified is not verified

        if action == "corrected":
            category = "corrected"
        elif action == "filled":
            category = "filled"
        elif action == "ok":
            category = "canonicalize" if needs_write else ("verify" if needs_flag else "ok")
        else:  # unverified / none
            category = "unverified"
        counts[category] += 1

        if needs_write or needs_flag:
            changes.append((p, final_doi, category))
            affected_users.add(p.user_id)

    # Report
    print("\n=== DOI audit summary ===")
    for cat in ("ok", "verify", "canonicalize", "corrected", "filled", "unverified", "no_pmid"):
        if counts.get(cat):
            print(f"  {cat:12s} {counts[cat]}")

    if changes:
        print(f"\n{len(changes)} row(s) need updating "
              f"(corrected={counts['corrected']}, filled={counts['filled']}, "
              f"canonicalize={counts['canonicalize']}, verify={counts['verify']}; "
              "the rest set doi_verified only):")
        for p, new_doi, category in changes:
            print(f"  [{category}] pmid={p.pmid} | {(p.title or '')[:55]!r}")
            print(f"        {p.doi!r} -> {new_doi!r}")
    else:
        print("\nAll DOI links are valid. Nothing to fix.")

    if fix and changes:
        await _fix_per_pi(db, changes, authoritative, affected_users)
    elif changes:
        print("\n[report only] Re-run with --fix to apply and re-export.")
    return counts


async def _fix_per_pi(
    db: AsyncSession,
    changes: list[tuple[Publication, str | None, str]],
    authoritative: dict[str, str],
    affected_users: set[uuid.UUID],
) -> None:
    """Correct and re-export each affected PI in its own transaction, under that PI's
    persona writer locks taken before the first write (lock order: ``profile_publish``
    module docstring). The audit read its rows without the lock, so each PI's rows are
    re-read under it and reconciled again: a pipeline run that committed meanwhile wins.
    Writes ``doi`` when it differs and ``doi_verified`` from the fresh reconcile action."""
    pub_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    for p, _, _ in changes:
        pub_ids.setdefault(p.user_id, []).append(p.id)
    await db.commit()  # end the audit's read transaction before the first lock
    written = flagged = 0
    print(f"\nCorrecting and re-exporting {len(affected_users)} profile(s)...")
    for uid in affected_users:
        await lock_persona_writer(db, uid)
        fresh = (await db.execute(
            select(Publication).where(Publication.id.in_(pub_ids[uid]))
            .execution_options(populate_existing=True)
        )).scalars().all()
        for p in fresh:
            final_doi, action = reconcile_pub_doi(p.doi, authoritative.get(str(p.pmid)))
            if action in ("corrected", "filled", "ok") and (final_doi or None) != (p.doi or None):
                p.doi = final_doi
                written += 1
            verified = _VERIFIED_BY_ACTION.get(action)
            if verified is not None and p.doi_verified is not verified:
                p.doi_verified = verified
                flagged += 1
        rendered = await reexport_persona(
            db, uid, mechanism="reexport",
            change_summary="DOI links corrected (audit_pub_dois)",
            skip_if_file_matches=True,
        )
        await db.commit()
        if rendered is None:
            print(f"  SKIP {uid}: missing profile/agent, or the persona is unchanged "
                  "(DOI updates committed)")
            continue
        path = await write_persona_files(db, uid)
        print(f"  {path.name if path else 'FAILED (see the ERROR log)'}")
    print(f"Committed {written} DOI updates and {flagged} doi_verified updates.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--orcids", nargs="*", default=[], help="Limit to these ORCIDs")
    parser.add_argument("--agents", nargs="*", default=[], help="Limit to these agent_ids")
    parser.add_argument("--fix", action="store_true",
                        help="Apply corrections and re-export (default: report only)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.orcids, args.agents, args.fix)))


if __name__ == "__main__":
    main()
