#!/usr/bin/env python3
"""Generate a matchmaker collaboration proposal from two PI profile directories.

Usage (from repo root inside the app container):

  Single pair (positional args):
    python scripts/matchmaker_cli.py <pi_a_slug> <pi_b_slug> [--dry-run]

  Batch from TSV file (-t flag, no positional args):
    python scripts/matchmaker_cli.py -t pairs.tsv [--dry-run]

The TSV file has two tab-separated columns (pi_a, pi_b), one pair per line.
Lines starting with '#' and blank lines are ignored. A header row whose first
cell is "pi_a" (case-insensitive) is also skipped automatically.

Examples:
    python scripts/matchmaker_cli.py su wiseman
    python scripts/matchmaker_cli.py grotjahn lotz --dry-run
    python scripts/matchmaker_cli.py -t pairs.tsv
    python scripts/matchmaker_cli.py -t pairs.tsv --dry-run

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
    slug = slug.lower()
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


def _parse_tsv(path: str) -> list[tuple[str, str]]:
    """Parse a two-column TSV file into a list of (pi_a, pi_b) slug pairs."""
    pairs: list[tuple[str, str]] = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                print(f"Warning: line {lineno} has fewer than 2 columns, skipping: {line!r}")
                continue
            a, b = parts[0].strip(), parts[1].strip()
            if lineno == 1 and a.lower() == "pi_a":
                continue  # skip header row
            if not a or not b:
                print(f"Warning: line {lineno} has empty slug, skipping.")
                continue
            pairs.append((a, b))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate matchmaker proposals from PI profile slugs.",
        epilog=(
            "Single pair:  matchmaker_cli.py su wiseman\n"
            "Batch TSV:    matchmaker_cli.py -t pairs.tsv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pi_a", nargs="?", help="Slug for PI A (e.g. 'su')")
    parser.add_argument("pi_b", nargs="?", help="Slug for PI B (e.g. 'wiseman')")
    parser.add_argument(
        "-t", "--tsv",
        metavar="FILE",
        help="TSV file with two columns (pi_a, pi_b); one pair per line",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposals to stdout without writing to the database",
    )
    args = parser.parse_args()

    # Build list of pairs to process
    if args.tsv:
        if args.pi_a or args.pi_b:
            parser.error("Cannot combine -t/--tsv with positional PI arguments.")
        pairs = _parse_tsv(args.tsv)
        if not pairs:
            print("No valid pairs found in TSV file.")
            sys.exit(1)
    elif args.pi_a and args.pi_b:
        pairs = [(args.pi_a, args.pi_b)]
    else:
        parser.error("Provide either two positional slugs or -t FILE.")

    errors: list[str] = []
    for i, (slug_a, slug_b) in enumerate(pairs):
        if len(pairs) > 1:
            print(f"\n{'='*72}")
            print(f"Pair {i + 1}/{len(pairs)}: {slug_a}  ×  {slug_b}")
            print(f"{'='*72}")
        if slug_a == slug_b:
            msg = f"Skipping {slug_a} × {slug_b}: PI A and PI B must be different."
            print(msg)
            errors.append(msg)
            continue
        try:
            asyncio.run(run(slug_a, slug_b, args.dry_run))
        except SystemExit:
            errors.append(f"Failed: {slug_a} × {slug_b}")

    if errors:
        print(f"\n{len(errors)} pair(s) failed:")
        for e in errors:
            print(f"  {e}")


if __name__ == "__main__":
    main()
