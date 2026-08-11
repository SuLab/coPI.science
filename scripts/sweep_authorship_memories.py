"""One-time sweep: remove ungrounded authorship lines from agent working memories.

Issue #29: GoodBot's working memory carried the false note
'Co-authored "Desiderata" paper' for six weeks and re-asserted it into every
prompt. The memory-synthesis guard prevents NEW poisoned lines; this script
cleans EXISTING memory files.

Dry-run by default (prints findings). --fix writes cleaned files, keeping the
original at <file>.pre-sweep.

Usage (inside the app container / on the compose network):

    docker compose exec app python scripts/sweep_authorship_memories.py         # dry run
    docker compose exec app python scripts/sweep_authorship_memories.py --fix   # apply

NOTE for a live agent-run: Agent caches the public memory segment in-process;
an external file edit is invisible until restart. After --fix on prod, restart
agent-run per the CLAUDE.md procedure (save logs → docker stop -t 30 → rebuild
→ run).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.authorship_rules import (  # noqa: E402
    LabPublicationRecord,
    strip_ungrounded_authorship_lines,
)


def sweep(
    memory_root: Path,
    records: dict[str, LabPublicationRecord],
    fix: bool,
) -> list[tuple[str, list[str]]]:
    """Scan every profiles/memory/<agent_id>/public.md; return (agent_id, stripped_lines)."""
    findings: list[tuple[str, list[str]]] = []
    for memory_file in sorted(memory_root.glob("*/public.md")):
        agent_id = memory_file.parent.name
        own = records.get(agent_id, LabPublicationRecord(dois=set(), has_records=False))
        original = memory_file.read_text(encoding="utf-8")
        cleaned, stripped = strip_ungrounded_authorship_lines(original, own)
        if not stripped:
            continue
        findings.append((agent_id, stripped))
        if fix:
            backup = memory_file.with_suffix(".md.pre-sweep")
            backup.write_text(original, encoding="utf-8")
            memory_file.write_text(cleaned + "\n", encoding="utf-8")
    return findings


async def _load_records() -> dict[str, LabPublicationRecord]:
    from sqlalchemy import select

    from src.database import get_session_factory
    from src.models import AgentRegistry, Publication

    factory = get_session_factory()
    async with factory() as db:
        rows = (await db.execute(
            select(AgentRegistry.agent_id, Publication.doi)
            .join(Publication, Publication.user_id == AgentRegistry.user_id)
        )).all()
    records: dict[str, LabPublicationRecord] = {}
    for agent_id, doi in rows:
        rec = records.setdefault(agent_id, LabPublicationRecord(set(), True))
        if doi:
            rec.dois.add(doi.strip().rstrip(".,;").lower())
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="write cleaned files (default: dry run)")
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "profiles" / "memory",
    )
    args = parser.parse_args()

    records = asyncio.run(_load_records())
    findings = sweep(args.memory_root, records, fix=args.fix)
    if not findings:
        print("No ungrounded authorship lines found.")
        return 0
    for agent_id, lines in findings:
        print(f"\n[{agent_id}] {len(lines)} ungrounded authorship line(s):")
        for line in lines:
            print(f"  - {line}")
    print(f"\n{'FIXED' if args.fix else 'DRY RUN — rerun with --fix to apply'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
