"""Regenerate a PI profile from an extracted CV text file only.

Bypasses ORCID/PubMed entirely. Calls the existing LLM synthesis helpers,
overwrites the ResearcherProfile DB row, and rewrites profiles/public/{agent_id}.md.

The CV text is a real person's document, so it lives in gitignored `data/` and
must never be committed:

    data/profile_context/{agent_id}_cv.txt        (pdftotext output)

Usage:
    docker compose exec app python scripts/regen_profile_from_cv.py --agent-id paulson \\
        --source-url https://www.scripps.edu/paulson/JCPcv.pdf

The researcher's name comes from the User row unless --name is given.
--institution / --department overwrite the User columns when supplied (the CV is
usually the most reliable source for those); omit them to leave the row alone.
"""

import argparse
import asyncio
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.database import get_session_factory
from src.models import AgentRegistry, ResearcherProfile, User
from src.services.llm import synthesize_profile
from src.services.profile_export import export_profile_to_markdown
from src.services.profile_versioning import create_revision

DEFAULT_CONTEXT_DIR = Path("data/profile_context")


def build_context(raw_cv: str, pi_name: str, source_url: str | None) -> str:
    source = f", sourced from {source_url}" if source_url else ""
    return (
        "## Source\n"
        f"The text below is the verbatim extracted contents of {pi_name}'s\n"
        f"curriculum vitae{source}.\n"
        "Use ONLY this content to synthesize the profile. Do not invent details\n"
        "not supported by the CV. Weight the most recent publications most heavily\n"
        "when summarizing current research focus.\n\n"
        "## Verbatim CV (pdftotext output)\n\n"
        f"{raw_cv}"
    )


async def _run(
    agent_id: str,
    cv_path: Path,
    name: str | None,
    institution: str | None,
    department: str | None,
    source_url: str | None,
) -> int:
    if not cv_path.is_file():
        print(f"ERROR: no CV text at {cv_path}", flush=True)
        return 1

    raw_cv = cv_path.read_text(encoding="utf-8")
    print(f"=== regen profile: {agent_id} ===", flush=True)

    session_factory = get_session_factory()
    async with session_factory() as db:
        agent_result = await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
        )
        agent_reg = agent_result.scalar_one_or_none()
        if not agent_reg:
            print(f"  ERROR: no AgentRegistry row for agent_id={agent_id}", flush=True)
            return 1

        user_result = await db.execute(
            select(User).where(User.id == agent_reg.user_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            print(f"  ERROR: no User row for {agent_id}", flush=True)
            return 1

        pi_name = name or user.name
        context = build_context(raw_cv, pi_name, source_url)
        print(f"  context: {len(context)} chars from {cv_path}", flush=True)

        if institution:
            user.institution = institution
        if department:
            user.department = department

        prof_result = await db.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
        )
        profile = prof_result.scalar_one_or_none()
        if not profile:
            profile = ResearcherProfile(user_id=user.id)
            db.add(profile)
            await db.flush()

        print("  Calling synthesize_profile (Claude)...", flush=True)
        synthesized = await synthesize_profile(context, pi_name)

        print(f"  research_summary: {len(synthesized.get('research_summary', '').split())} words", flush=True)
        print(f"  techniques: {len(synthesized.get('techniques', []))}", flush=True)
        print(f"  models: {len(synthesized.get('experimental_models', []))}", flush=True)
        print(f"  diseases: {len(synthesized.get('disease_areas', []))}", flush=True)
        print(f"  targets: {len(synthesized.get('key_targets', []))}", flush=True)

        profile.research_summary = synthesized.get("research_summary", "")
        profile.techniques = synthesized.get("techniques", [])
        profile.experimental_models = synthesized.get("experimental_models", [])
        profile.disease_areas = synthesized.get("disease_areas", [])
        profile.key_targets = synthesized.get("key_targets", [])
        profile.keywords = synthesized.get("keywords", [])
        profile.profile_version = (profile.profile_version or 0) + 1
        profile.profile_generated_at = datetime.now(timezone.utc)
        profile.raw_abstracts_hash = hashlib.sha256(context.encode()).hexdigest()

        await db.flush()

        # Pass publications=[] so we don't carry stale ORCID-derived pubs into a
        # CV-sourced regeneration. The CV's publication list informs the
        # synthesized summary/techniques but is not exported as a pub list.
        exported = export_profile_to_markdown(user, profile, agent_id, publications=[])
        if not exported:
            print("  ERROR: export failed", flush=True)
            return 1
        print(f"  Wrote {exported}", flush=True)

        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported.read_text(encoding="utf-8"),
            mechanism="pipeline",
            change_summary="Profile regenerated from CV text only (no ORCID/PubMed)",
        )
        await db.commit()
        print(f"  DB commit: profile v{profile.profile_version} for {user.name}", flush=True)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent-id", required=True, help="Agent id to regenerate (e.g. paulson)")
    parser.add_argument("--cv-text", type=Path,
                        help=f"CV text file (default: {DEFAULT_CONTEXT_DIR}/<agent_id>_cv.txt)")
    parser.add_argument("--name", help="Researcher name for the synthesis call (default: User.name)")
    parser.add_argument("--institution", help="Overwrite User.institution with this CV-confirmed value")
    parser.add_argument("--department", help="Overwrite User.department with this CV-confirmed value")
    parser.add_argument("--source-url", help="CV source URL, recorded in the prompt preamble")
    args = parser.parse_args()

    cv_path = args.cv_text or (DEFAULT_CONTEXT_DIR / f"{args.agent_id}_cv.txt")
    sys.exit(asyncio.run(_run(
        args.agent_id, cv_path, args.name, args.institution, args.department, args.source_url,
    )))


if __name__ == "__main__":
    main()
