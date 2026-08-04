"""Audit publication DOI links against PubMed (the periodic DOI validation pass).

For every publication (optionally filtered to specific users), this fetches the
authoritative DOI registered for its PMID from PubMed and compares it to the
stored DOI. A stored DOI that disagrees with its PMID's record points at a
different paper than the one cited — the failure mode behind the bad-link
incident (see GitHub issue #5).

Classifies each row and, with --fix, corrects the DB and re-exports the
affected public profiles.

Categories:
  ok           stored DOI matches the PMID's authoritative DOI
  canonicalize stored matches but in non-canonical form (doi: prefix, etc.)
  corrected    stored DOI disagrees with the PMID's record  -> wrong link
  filled       no stored DOI; authoritative one is available
  unverified   PMID has no DOI on file (can't validate)     -> left as-is
  no_pmid      publication has no PMID                       -> left as-is

Usage (runs inside the app container — needs DB + network):
    # Audit everyone (report only):
    docker exec copi-python-app-1 python scripts/audit_pub_dois.py

    # Audit + fix specific users by ORCID:
    docker exec copi-python-app-1 python scripts/audit_pub_dois.py \\
        --orcids 0000-0000-0000-0001 --fix

    # Audit + fix specific agents, or everyone:
    docker exec copi-python-app-1 python scripts/audit_pub_dois.py --agents liu bollong --fix
    docker exec copi-python-app-1 python scripts/audit_pub_dois.py --fix
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
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.services.profile_export import export_profile_to_markdown
from src.services.pubmed import fetch_authoritative_dois, reconcile_pub_doi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audit_pub_dois")


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

    async with factory() as db:
        user_ids = await _select_user_ids(db, orcids, agents)

        q = select(Publication)
        if user_ids is not None:
            if not user_ids:
                print("No users matched the given --orcids/--agents filter.")
                await engine.dispose()
                return 0
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
            needs_write = (final_doi or None) != (p.doi or None)

            if action == "corrected":
                category = "corrected"
            elif action == "filled":
                category = "filled"
            elif action == "ok":
                category = "canonicalize" if needs_write else "ok"
            else:  # unverified / none
                category = "unverified"
            counts[category] += 1

            if action in ("corrected", "filled", "ok") and needs_write:
                changes.append((p, final_doi, category))
                affected_users.add(p.user_id)

        # Report
        print("\n=== DOI audit summary ===")
        for cat in ("ok", "canonicalize", "corrected", "filled", "unverified", "no_pmid"):
            if counts.get(cat):
                print(f"  {cat:12s} {counts[cat]}")

        if changes:
            print(f"\n{len(changes)} link(s) need updating "
                  f"(corrected={counts['corrected']}, "
                  f"filled={counts['filled']}, canonicalize={counts['canonicalize']}):")
            for p, new_doi, category in changes:
                print(f"  [{category}] pmid={p.pmid} | {(p.title or '')[:55]!r}")
                print(f"        {p.doi!r} -> {new_doi!r}")
        else:
            print("\nAll DOI links are valid. Nothing to fix.")

        if fix and changes:
            for p, new_doi, _ in changes:
                p.doi = new_doi
            await db.commit()
            print(f"\nCommitted {len(changes)} DOI updates.")

            # Re-export affected public profiles.
            print(f"Re-exporting {len(affected_users)} profile(s)...")
            for uid in affected_users:
                user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
                profile = (await db.execute(
                    select(ResearcherProfile).where(ResearcherProfile.user_id == uid)
                )).scalar_one_or_none()
                agent = (await db.execute(
                    select(AgentRegistry).where(AgentRegistry.user_id == uid)
                )).scalar_one_or_none()
                user_pubs = (await db.execute(
                    select(Publication).where(Publication.user_id == uid)
                )).scalars().all()
                if user and profile and agent:
                    exported = export_profile_to_markdown(
                        user, profile, agent.agent_id, publications=user_pubs
                    )
                    print(f"  {agent.agent_id}: {exported.name if exported else 'FAILED'}")
                else:
                    print(f"  SKIP {user.name if user else uid}: missing profile/agent")
        elif changes:
            print("\n[report only] Re-run with --fix to apply and re-export.")

    await engine.dispose()
    return 0


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
