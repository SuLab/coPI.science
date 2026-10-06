"""Audit the persona files in profiles/public/ against the agents table (spec 2026-10-05
§6.4 one-off, §9 row 4 verification).

Exactly one mode is required:

  --check             Read-only. Lists every lab agent whose owner has a profile but whose
                      persona file is missing or has no Research Summary (a FAIL line, or a
                      WARN line for a suspended agent), and every *.md file in the persona
                      directory whose stem has no agents row (an ORPHAN, FAIL). This is the
                      §9 row-4 verification. Exit 0 only when both checks pass.
  --archive-orphans   Lists the orphan files; with --apply moves each to
                      profiles/private/orphaned/. Never touches a file whose stem has an
                      agents row (any status) or is kept.
  --reexport-missing  Lists the non-suspended agents whose file is MISSING or NOSUMMARY
                      while the owner's profile has a summary; with --apply re-exports each
                      in its own transaction and writes its files. Enqueues no job.

--keep names persona stems never treated as orphans (default: blackbird, the hub's file; its
archive is a separate step after the new agent is up). --apply is accepted only with
--archive-orphans or --reexport-missing.

Usage (runs inside the app container):
    docker compose -f docker-compose.prod.yml exec -T blackbird-app python scripts/persona_file_audit.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.agent.role_capabilities import requires_linked_user  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.models import AgentRegistry, ResearcherProfile  # noqa: E402
from src.services import profile_export  # noqa: E402
from src.services.persona_lifecycle import (  # noqa: E402
    archive_persona_file,
    persona_has_research_summary,
)
from src.services.profile_publish import reexport_persona, write_persona_files  # noqa: E402

DEFAULT_KEEP = ("blackbird",)
_MODES = ("check", "archive-orphans", "reexport-missing")


async def _agents(db: AsyncSession):
    """(agent_id, user_id, role, status, profile summary, has profile) per agent row."""
    return (await db.execute(
        select(
            AgentRegistry.agent_id, AgentRegistry.user_id, AgentRegistry.role,
            AgentRegistry.status, ResearcherProfile.research_summary,
            ResearcherProfile.id.is_not(None),
        ).outerjoin(ResearcherProfile, ResearcherProfile.user_id == AgentRegistry.user_id)
        .order_by(AgentRegistry.agent_id)
    )).all()


def _persona_findings(rows) -> list[tuple[str, str, uuid.UUID, str, bool]]:
    """(code, slug, user_id, status, summary_nonempty) per agent whose file is wrong.
    Only agents that need a linked user, have one, and whose owner has a profile count."""
    found = []
    for slug, user_id, role, status, summary, has_profile in rows:
        if not (requires_linked_user(role) and user_id is not None and has_profile):
            continue
        nonempty = bool((summary or "").strip())
        path = profile_export.PROFILES_DIR / f"{slug}.md"
        if not path.is_file():
            code = "MISSING"
        elif persona_has_research_summary(path.read_text(encoding="utf-8")):
            continue
        else:
            code = "NOSUMMARY" if nonempty else "EMPTYPROFILE"
        found.append((code, slug, user_id, status, nonempty))
    return found


def _orphans(db_slugs: set[str], keep: frozenset[str]) -> tuple[list[Path], list[Path]]:
    """(orphan files, kept files present): *.md files with no agents row, split by --keep."""
    orphans, kept = [], []
    for p in sorted(profile_export.PROFILES_DIR.glob("*.md")):
        if p.stem in db_slugs:
            continue
        (kept if p.stem in keep else orphans).append(p)
    return orphans, kept


async def run(db: AsyncSession, mode: str, *, apply: bool, keep: frozenset[str]) -> int:
    """Run one mode and return the exit code. ``--check`` issues only SELECTs and file
    reads; ``main()`` additionally makes its session read-only."""
    rows = await _agents(db)
    findings = _persona_findings(rows)
    orphans, kept = _orphans({r[0] for r in rows}, keep)

    if mode == "check":
        failed = 0
        for code, slug, _uid, status, _ne in findings:
            if status == "suspended":
                print(f"WARN {code} {slug} (suspended)")
            else:
                failed += 1
                print(f"{code} {slug}")
        print(f"FAIL persona_files: {failed}" if failed else "PASS persona_files")
        for p in orphans:
            print(f"ORPHAN {p.name}")
        for p in kept:
            print(f"KEPT {p.name}")
        print(f"FAIL orphans: {len(orphans)}" if orphans else "PASS orphans")
        return 1 if (failed or orphans) else 0

    if mode == "archive-orphans":
        bad = 0
        for p in orphans:
            print(f"ORPHAN {p.name}")
            if apply:
                dst = archive_persona_file(p.stem)
                if dst is None:
                    bad += 1
                    print(f"FAILED {p.name}")
                else:
                    print(f"ARCHIVED {p} -> {dst}")
        return 1 if bad else 0

    if mode == "reexport-missing":
        targets = [f for f in findings
                   if f[0] in ("MISSING", "NOSUMMARY") and f[3] != "suspended" and f[4]]
        failed = 0
        for code, slug, user_id, _status, _ne in targets:
            print(f"{code} {slug}")
            if not apply:
                continue
            try:
                rendered = await reexport_persona(
                    db, user_id, mechanism="reexport",
                    change_summary="Persona file audit re-export")
                await db.commit()
                path = await write_persona_files(db, user_id) if rendered else None
            except Exception as exc:  # one agent's failure must not stop the rest
                await db.rollback()
                path = None
                print(f"  error: {exc}")
            if path is None:
                failed += 1
                print(f"FAILED {slug}")
            else:
                print(f"REEXPORTED {slug} -> {path}")
        return 1 if failed else 0

    raise ValueError(f"unknown mode {mode!r}")


async def _main(mode: str, apply: bool, keep: frozenset[str]) -> int:
    engine = create_async_engine(get_settings().database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            if mode == "check":
                from sqlalchemy import text
                await db.execute(text("SET TRANSACTION READ ONLY"))
            return await run(db, mode, apply=apply, keep=keep)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    for m in _MODES:
        group.add_argument(f"--{m}", dest="mode", action="store_const", const=m)
    parser.add_argument("--apply", action="store_true",
                        help="Perform the moves / re-exports (not with --check)")
    parser.add_argument("--keep", nargs="*", default=list(DEFAULT_KEEP), metavar="NAME",
                        help="Persona stems never treated as orphans (default: blackbird)")
    args = parser.parse_args()
    if args.apply and args.mode == "check":
        print("--apply is not accepted with --check", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_main(args.mode, args.apply, frozenset(args.keep))))


if __name__ == "__main__":
    main()
