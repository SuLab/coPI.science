"""Regenerate PI profiles from hand-curated, web-sourced context files.

Bypasses ORCID/PubMed entirely. Calls the existing LLM synthesis helpers,
overwrites the ResearcherProfile DB row, and rewrites profiles/public/{agent_id}.md.

One markdown file per agent holds whatever was gathered from the lab website /
faculty page. Those files describe real people, so they live in gitignored
`data/` and must never be committed:

    data/profile_context/{agent_id}.md

If the file opens with a "## Researcher Information" block, `- Name:`,
`- Institution:` and `- Department:` lines are read from it: the name is used
for the synthesis call (falling back to User.name) and the other two backfill
empty User columns. Everything in the file is passed to the LLM verbatim.

Usage:
    docker compose exec app python scripts/regen_profiles_from_web.py --agent-id wilson minor
    docker compose exec app python scripts/regen_profiles_from_web.py --agent-id wilson --context /tmp/wilson.md
"""

import argparse
import asyncio
import hashlib
import re
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


def _header_field(context: str, field: str) -> str | None:
    """Read a `- Field: value` entry from the context's leading header block.

    Values may wrap onto following indented lines (as multi-department
    affiliations tend to); those are folded into one whitespace-normalized
    string. A following `- Other:` entry starts at column 0 and so ends the
    match.
    """
    m = re.search(
        rf"^-\s*{re.escape(field)}:\s*(.+(?:\n[ \t]+\S.*)*)$", context, re.MULTILINE
    )
    return " ".join(m.group(1).split()) if m else None


async def regen_one(agent_id: str, context_path: Path) -> bool:
    """Regenerate one profile. Returns True on success."""
    if not context_path.is_file():
        print(f"=== {agent_id} ===\n  ERROR: no context file at {context_path}", flush=True)
        return False

    context = context_path.read_text(encoding="utf-8")
    print(f"\n=== {agent_id} ===", flush=True)
    print(f"  context: {len(context)} chars from {context_path}", flush=True)

    session_factory = get_session_factory()
    async with session_factory() as db:
        agent_result = await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
        )
        agent_reg = agent_result.scalar_one_or_none()
        if not agent_reg:
            print(f"  ERROR: no AgentRegistry row for agent_id={agent_id}", flush=True)
            return False

        user_result = await db.execute(
            select(User).where(User.id == agent_reg.user_id)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            print(f"  ERROR: no User row for {agent_id}", flush=True)
            return False

        pi_name = _header_field(context, "Name") or user.name
        if not user.institution:
            user.institution = _header_field(context, "Institution")
        if not user.department:
            user.department = _header_field(context, "Department")

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

        # Overwrite the public markdown file. Pass publications=[] so we don't
        # carry over stale ORCID-derived pubs into a web-sourced regeneration.
        exported = export_profile_to_markdown(user, profile, agent_id, publications=[])
        if not exported:
            print("  ERROR: export failed", flush=True)
            return False
        print(f"  Wrote {exported}", flush=True)

        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported.read_text(encoding="utf-8"),
            mechanism="pipeline",
            change_summary="Profile regenerated from web-sourced lab website context (no ORCID)",
        )
        await db.commit()
        print(f"  DB commit: profile v{profile.profile_version} for {user.name}", flush=True)
        return True


async def _run(agent_ids: list[str], context: Path | None, context_dir: Path) -> int:
    failures = 0
    for agent_id in agent_ids:
        path = context or (context_dir / f"{agent_id}.md")
        if not await regen_one(agent_id, path):
            failures += 1
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--agent-id", nargs="+", required=True,
                        help="Agent id(s) to regenerate (e.g. wilson minor)")
    parser.add_argument("--context", type=Path,
                        help="Explicit context file (only valid with a single --agent-id); "
                             f"default {DEFAULT_CONTEXT_DIR}/<agent_id>.md")
    parser.add_argument("--context-dir", type=Path, default=DEFAULT_CONTEXT_DIR,
                        help=f"Directory holding <agent_id>.md context files (default: {DEFAULT_CONTEXT_DIR})")
    args = parser.parse_args()

    if args.context and len(args.agent_id) > 1:
        parser.error("--context takes a single file; use --context-dir for multiple agents")

    sys.exit(asyncio.run(_run(args.agent_id, args.context, args.context_dir)))


if __name__ == "__main__":
    main()
