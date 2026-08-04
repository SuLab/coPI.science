"""Vet a user's publications against their research scope using Claude.

For each user, sends the title+journal+year of every Publication row plus the
researcher's name and current research_summary, and asks the LLM to classify
each publication as in-scope ("keep") or off-scope ("delete"). Deletes the
"delete" set, then re-syncs the research_summary by running synthesize_profile
on the cleaned publication set (with abstracts) and re-exports the md.

This is the targeted-cleanup tool for users whose ORCID is empty or noisy and
whose Publication rows ended up contaminated by other researchers of the same
name at the same institution.

Usage:
    docker exec copi-python-app-1 python scripts/vet_publications.py \\
        --orcids 0000-0000-0000-0001 0000-0000-0000-0002 \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.services.llm import (
    _extract_json,
    get_anthropic_client,
    synthesize_profile,
)
from src.services.profile_export import export_profile_to_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vet_pubs")


VET_SYSTEM_PROMPT = """You are auditing a researcher's publication list for a research-collaboration
platform. The list was assembled via PubMed name+affiliation search and may contain papers
by *other* people with the same name at the same institution. Your job is to classify each
publication as belonging to THIS researcher (keep) or to someone else (delete).

Be conservative with deletions: only mark a publication for deletion when its topic is
clearly inconsistent with the researcher's stated scope. If a paper is plausibly within
the researcher's program — even tangentially or as a collaboration — keep it. Reviews,
correspondences, and "Author Correction" entries should be kept if the topic matches.

Output strict JSON with this exact schema:

{
  "keep_pmids": ["PMID1", "PMID2", ...],
  "delete_pmids": ["PMID3", "PMID4", ...]
}

Every PMID in the input must appear in exactly one list. No prose before or after.
"""


def _build_vet_prompt(name: str, summary: str, pubs: list[dict]) -> str:
    pub_lines = []
    for p in pubs:
        pub_lines.append(
            f"- PMID {p['pmid']}: {p['title']} ({p.get('journal') or 'unknown'}, {p.get('year') or 'n.d.'})"
        )
    return (
        f"Researcher: {name}\n\n"
        f"Stated research scope (from researcher_profiles.research_summary):\n{summary}\n\n"
        f"Candidate publications ({len(pubs)}):\n" + "\n".join(pub_lines) +
        "\n\nClassify each PMID as keep or delete. Output JSON only."
    )


def _vet_with_llm(name: str, summary: str, pubs: list[dict]) -> tuple[list[str], list[str]]:
    settings = get_settings()
    if not pubs:
        return [], []
    user_message = _build_vet_prompt(name, summary, pubs)
    client = get_anthropic_client()
    message = client.messages.create(
        model=settings.llm_profile_model,
        max_tokens=4000,
        system=VET_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = message.content[0].text
    try:
        parsed = _extract_json(response_text)
    except ValueError:
        logger.error("Vet response JSON parse failed for %s; raw:\n%s", name, response_text)
        raise
    keep = [str(x) for x in parsed.get("keep_pmids", [])]
    delete = [str(x) for x in parsed.get("delete_pmids", [])]
    # Ensure all input pmids are covered. Default-keep any missing.
    seen = set(keep) | set(delete)
    for p in pubs:
        if p["pmid"] not in seen:
            keep.append(p["pmid"])
    return keep, delete


def _build_resynth_context(name: str, institution: str | None, pubs: list[dict]) -> str:
    parts = [f"## Researcher\n- Name: {name}"]
    if institution:
        parts.append(f"- Institution: {institution}")
    if pubs:
        parts.append("\n## Publications")
        sorted_pubs = sorted(pubs, key=lambda p: p.get("year") or 0, reverse=True)[:30]
        for pub in sorted_pubs:
            parts.append(
                f"\n### {pub.get('title','(no title)')} "
                f"({pub.get('journal','?')}, {pub.get('year','n.d.')})"
            )
            if pub.get("abstract"):
                parts.append(f"Abstract: {pub['abstract'][:1500]}")
    return "\n".join(parts)


async def _process(orcid: str, db: AsyncSession, dry_run: bool) -> dict:
    user = (await db.execute(select(User).where(User.orcid == orcid))).scalar_one_or_none()
    if not user:
        return {"orcid": orcid, "status": "no_user"}
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )).scalar_one_or_none()
    pubs_q = await db.execute(select(Publication).where(Publication.user_id == user.id))
    pubs = pubs_q.scalars().all()
    n_before = len(pubs)

    summary = (profile.research_summary if profile else "") or f"{user.name} is a researcher."
    pub_dicts = [
        {"pmid": p.pmid, "title": p.title, "journal": p.journal, "year": p.year,
         "abstract": p.abstract or ""}
        for p in pubs if p.pmid
    ]

    if not pub_dicts:
        logger.info("%s: 0 pubs, nothing to vet", user.name)
        return {"orcid": orcid, "user": user.name, "before": 0, "after": 0, "deleted": []}

    logger.info("%s: vetting %d publications...", user.name, len(pub_dicts))
    keep_pmids, delete_pmids = _vet_with_llm(user.name, summary, pub_dicts)
    logger.info("%s: LLM says keep %d, delete %d", user.name, len(keep_pmids), len(delete_pmids))

    if dry_run:
        deleted_titles = [
            f"{p.pmid}: {p.title[:80]}" for p in pubs if p.pmid in set(delete_pmids)
        ]
        return {
            "orcid": orcid, "user": user.name, "before": n_before,
            "after": n_before - len(delete_pmids), "deleted": deleted_titles,
            "dry_run": True,
        }

    delete_set = set(delete_pmids)
    deleted_titles: list[str] = []
    for p in pubs:
        if p.pmid in delete_set:
            deleted_titles.append(f"{p.pmid}: {p.title[:80]}")
            await db.delete(p)
    await db.flush()

    # Re-synthesize summary from cleaned set
    kept = [p for p in pubs if p.pmid in set(keep_pmids)]
    if kept and profile:
        ctx = _build_resynth_context(user.name, user.institution, [
            {"pmid": p.pmid, "title": p.title, "journal": p.journal,
             "year": p.year, "abstract": p.abstract or ""} for p in kept
        ])
        try:
            synthesized = await synthesize_profile(ctx, user.name)
            profile.research_summary = synthesized.get("research_summary", profile.research_summary)
            profile.techniques = synthesized.get("techniques", profile.techniques)
            profile.experimental_models = synthesized.get("experimental_models", profile.experimental_models)
            profile.disease_areas = synthesized.get("disease_areas", profile.disease_areas)
            profile.key_targets = synthesized.get("key_targets", profile.key_targets)
            profile.keywords = synthesized.get("keywords", profile.keywords)
            profile.profile_version = (profile.profile_version or 0) + 1
            profile.profile_generated_at = datetime.now(timezone.utc)
            # Hash for raw_abstracts_hash so the pipeline knows we resynced
            abstracts_str = "\n".join(p.abstract or "" for p in kept)
            profile.raw_abstracts_hash = hashlib.sha256(abstracts_str.encode()).hexdigest()
            logger.info("%s: research_summary re-synthesized (version=%d)",
                        user.name, profile.profile_version)
        except Exception as exc:
            logger.error("%s: resynthesis failed: %s", user.name, exc)

    # Re-export md
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )).scalar_one_or_none()
    if agent and profile:
        # Reload pubs after deletion
        pubs_after_q = await db.execute(
            select(Publication).where(Publication.user_id == user.id)
        )
        pubs_after = pubs_after_q.scalars().all()
        exported = export_profile_to_markdown(user, profile, agent.agent_id, publications=pubs_after)
        logger.info("%s: exported %s", user.name, exported.name if exported else "(failed)")

    return {
        "orcid": orcid, "user": user.name, "before": n_before,
        "after": n_before - len(delete_pmids), "deleted": deleted_titles,
    }


async def _run(orcids: list[str], dry_run: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    results = []
    async with factory() as db:
        for orcid in orcids:
            r = await _process(orcid, db, dry_run=dry_run)
            results.append(r)
            if not dry_run:
                await db.commit()

    await engine.dispose()

    print("\n=== Vet summary ===")
    for r in results:
        if r.get("status") == "no_user":
            print(f"  {r['orcid']}: no_user")
            continue
        before = r.get("before", 0)
        after = r.get("after", 0)
        print(f"  {r['user']:25s}  {before:>4d} → {after:<4d}  (deleted {before - after})")
        for t in (r.get("deleted") or [])[:15]:
            print(f"      - {t}")
        if len(r.get("deleted") or []) > 15:
            print(f"      ... and {len(r['deleted']) - 15} more")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orcids", nargs="+", required=True, help="ORCIDs to vet")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.orcids, args.dry_run)))


if __name__ == "__main__":
    main()
