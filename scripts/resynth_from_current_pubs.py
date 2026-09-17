"""Re-synthesize a user's research profile from their CURRENT Publication rows.

Useful after manually deleting suspect publications: the user's research_summary
still reflects the pre-deletion set, so this script reruns synthesize_profile
on the cleaned set and re-exports the md.

Usage:
    docker exec copi-python-app-1 python scripts/resynth_from_current_pubs.py \\
        --orcids 0000-0000-0000-0001 0000-0000-0000-0002
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.services.llm import synthesize_profile
from src.services.profile_export import export_profile_to_markdown
from src.services.profile_pipeline import _validate_profile, apply_synthesis, bump_profile_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("resynth")


def _build_context(name: str, institution: str | None, pubs: list[Publication]) -> str:
    parts = [f"## Researcher\n- Name: {name}"]
    if institution:
        parts.append(f"- Institution: {institution}")
    if pubs:
        parts.append("\n## Publications")
        sorted_pubs = sorted(pubs, key=lambda p: p.year or 0, reverse=True)[:30]
        for pub in sorted_pubs:
            parts.append(
                f"\n### {pub.title or '(no title)'} ({pub.journal or '?'}, {pub.year or 'n.d.'})"
            )
            if pub.abstract:
                parts.append(f"Abstract: {pub.abstract[:1500]}")
    return "\n".join(parts)


async def _process(orcid: str, db: AsyncSession) -> str:
    user = (await db.execute(select(User).where(User.orcid == orcid))).scalar_one_or_none()
    if not user:
        return f"{orcid}: no_user"
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )).scalar_one_or_none()
    pubs = (await db.execute(
        select(Publication).where(Publication.user_id == user.id)
    )).scalars().all()
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == user.id)
    )).scalar_one_or_none()
    if not profile or not agent:
        return f"{user.name}: missing profile or agent"
    if not pubs:
        logger.info("%s: no pubs to synthesize from, exporting current state", user.name)
        export_profile_to_markdown(user, profile, agent.agent_id, publications=pubs)
        return f"{user.name}: 0 pubs, md exported"

    ctx = _build_context(user.name, user.institution, pubs)
    try:
        synthesized = await synthesize_profile(ctx, user.name)
    except Exception as exc:
        logger.error("%s: synthesis failed: %s", user.name, exc)
        return f"{user.name}: synthesis_failed: {exc}"

    validated = _validate_profile(synthesized)
    applied = apply_synthesis(profile, synthesized, validated=validated)
    if not applied:
        # Two reasons now, so this summary line reports the outcome and
        # `validated` rather than asserting a cause it cannot know: either the
        # keep-what-you-have gate refused an unvalidated synthesis over a good
        # stored one (validated=False), or the response carried none of the
        # fields apply_synthesis writes — and in that second case
        # apply_synthesis has already logged its keys, immediately above this
        # line, instead of blanking the profile with it.
        return (
            f"{user.name}: kept existing profile (validated={validated}); "
            "the new synthesis was not applied"
        )

    profile.profile_version = await bump_profile_version(db, profile.id)
    abstracts = "\n".join(p.abstract or "" for p in pubs)
    profile.raw_abstracts_hash = hashlib.sha256(abstracts.encode()).hexdigest()
    await db.flush()

    export_profile_to_markdown(user, profile, agent.agent_id, publications=pubs)
    return f"{user.name}: resynthesized v{profile.profile_version} from {len(pubs)} pubs"


async def _run(orcids: list[str]) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        for orcid in orcids:
            result = await _process(orcid, db)
            await db.commit()
            logger.info(result)

    await engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orcids", nargs="+", required=True)
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.orcids)))


if __name__ == "__main__":
    main()
