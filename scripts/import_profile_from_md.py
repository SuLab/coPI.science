"""Propagate a hand-edited profiles/public/<agent_id>.md back into the DB.

The public .md is normally a one-way export of ResearcherProfile
(src/services/profile_export.py). This is the inverse: parse the markdown
sections and write them onto the ResearcherProfile row, so a manual edit to the
file becomes the source of truth. Publications / grants sections are ignored
(they're derived from their own tables).

Run inside the app container (profiles/ is mounted, scripts/ is not):
    docker cp scripts/import_profile_from_md.py copi-python-app-1:/app/scripts/
    docker exec copi-python-app-1 python scripts/import_profile_from_md.py diercks
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.database import get_session_factory
from src.models import AgentRegistry, ResearcherProfile, User

# Export section header -> ResearcherProfile field + kind ("para" | "bullets" | "csv")
SECTION_MAP = {
    "Research Summary": ("research_summary", "para"),
    "Key Methods and Technologies": ("techniques", "bullets"),
    "Model Systems": ("experimental_models", "bullets"),
    "Disease Areas / Biological Processes": ("disease_areas", "bullets"),
    "Key Molecular Targets": ("key_targets", "bullets"),
    "Keywords": ("keywords", "csv"),
}


def parse_md(path: Path) -> dict:
    sections: dict[str, list[str]] = {}
    cur = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            cur = m.group(1).strip()
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    out: dict = {}
    for header, (field, kind) in SECTION_MAP.items():
        body = sections.get(header, [])
        if kind == "para":
            out[field] = "\n".join(body).strip()
        elif kind == "bullets":
            out[field] = [l[2:].strip() for l in body if l.startswith("- ") and l[2:].strip()]
        elif kind == "csv":
            joined = " ".join(l.strip() for l in body if l.strip())
            out[field] = [k.strip() for k in joined.split(",") if k.strip()]
    return out


async def main(agent_id: str) -> None:
    fields = parse_md(Path(f"/app/profiles/public/{agent_id}.md"))
    async with get_session_factory()() as db:
        row = (
            await db.execute(
                select(ResearcherProfile)
                .join(User, User.id == ResearcherProfile.user_id)
                .join(AgentRegistry, AgentRegistry.user_id == User.id)
                .where(AgentRegistry.agent_id == agent_id)
            )
        ).scalar_one()
        changed = []
        for field, new in fields.items():
            old = getattr(row, field)
            if old != new:
                changed.append((field, old, new))
            setattr(row, field, new)
        row.profile_version = (row.profile_version or 0) + 1
        row.profile_generated_at = datetime.now(timezone.utc)
        await db.commit()
    if not changed:
        print(f"[{agent_id}] no field changes (DB already matched the file)")
    for field, old, new in changed:
        print(f"[{agent_id}] CHANGED {field}\n   before: {old}\n   after:  {new}")
    print(f"[{agent_id}] profile_version -> {row.profile_version}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
