#!/usr/bin/env python3
"""Generate a matchmaker collaboration proposal from two PI profile directories.

Usage (from repo root inside the app container):
    python scripts/matchmaker_cli.py <pi_a_slug> <pi_b_slug> [--dry-run]

Examples:
    python scripts/matchmaker_cli.py su wiseman
    python scripts/matchmaker_cli.py grotjahn lotz --dry-run

The PI slug must match a filename in profiles/public/ (without .md extension).
Private profiles from profiles/private/{slug}.md are included if they exist.

Results are written to the matchmaker_proposals DB table and are immediately
visible in the admin Matchmaker tab at /admin/matchmaker.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path


def load_profile_files(slug: str) -> tuple[str, str, str]:
    """Load public + private profiles for a given slug.

    Returns (pi_name, public_md, private_md).
    pi_name is extracted from the '**PI:**' line in the public profile.
    """
    public_path = Path("profiles/public") / f"{slug}.md"
    private_path = Path("profiles/private") / f"{slug}.md"

    if not public_path.exists():
        available = sorted(p.stem for p in Path("profiles/public").glob("*.md"))
        print(f"Error: no public profile found for '{slug}'.")
        print(f"Available slugs: {', '.join(available)}")
        sys.exit(1)

    public_md = public_path.read_text()

    # Extract PI name from "**PI:** Name" line
    pi_name = slug.capitalize()
    match = re.search(r"\*\*PI:\*\*\s*(.+)", public_md)
    if match:
        pi_name = match.group(1).strip()

    private_md = private_path.read_text() if private_path.exists() else ""

    return pi_name, public_md, private_md


async def run(slug_a: str, slug_b: str, dry_run: bool) -> None:
    from src.config import get_settings
    from src.services.llm import generate_matchmaker_proposal

    name_a, public_a, private_a = load_profile_files(slug_a)
    name_b, public_b, private_b = load_profile_files(slug_b)

    settings = get_settings()

    print(f"Generating proposal: {name_a}  ×  {name_b}")
    print(f"Model: {settings.llm_agent_model_opus}")
    print("Calling LLM… (this may take 10–20 seconds)")

    result = await generate_matchmaker_proposal(
        name_a=name_a,
        public_profile_a=public_a,
        private_profile_a=private_a,
        publications_a="(see public profile above)",
        name_b=name_b,
        public_profile_b=public_b,
        private_profile_b=private_b,
        publications_b="(see public profile above)",
        model=settings.llm_agent_model_opus,
    )

    print(f"\nConfidence : {result['confidence'].upper()}")
    print(f"Title      : {result['title']}")
    print(f"Tokens     : {result['input_tokens']} in / {result['output_tokens']} out")
    print("\n" + "─" * 72)
    print(result["proposal_md"])
    print("─" * 72)

    if dry_run:
        print("\n[dry-run] Skipping database write.")
        return

    # Write to DB
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy import text

    from src.database import get_engine, get_session_factory
    from src.models.matchmaker import MatchmakerProposal

    engine = get_engine()
    session_factory = get_session_factory()

    async with session_factory() as session:
        proposal = MatchmakerProposal(
            id=uuid.uuid4(),
            pi_a_id=None,
            pi_b_id=None,
            pi_a_name=name_a,
            pi_b_name=name_b,
            proposal_md=result["proposal_md"],
            title=result["title"],
            confidence=result["confidence"],
            llm_model=result["model"],
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
            generated_at=datetime.now(timezone.utc),
        )
        session.add(proposal)
        await session.commit()
        print(f"\nSaved to DB: {proposal.id}")
        print(f"View at   : /admin/matchmaker/{proposal.id}")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a matchmaker proposal from two PI profile slugs."
    )
    parser.add_argument("pi_a", help="Slug for PI A (e.g. 'su', 'wiseman')")
    parser.add_argument("pi_b", help="Slug for PI B (e.g. 'grotjahn', 'lotz')")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposal to stdout without writing to the database",
    )
    args = parser.parse_args()

    if args.pi_a == args.pi_b:
        print("Error: PI A and PI B must be different.")
        sys.exit(1)

    asyncio.run(run(args.pi_a, args.pi_b, args.dry_run))


if __name__ == "__main__":
    main()
