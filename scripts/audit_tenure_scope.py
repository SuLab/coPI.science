"""Read-only tenure-scope audit (plan Task B15,
docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md §5, Task 15).

Per PI: the tenure-start year and its source, in-tenure / before-tenure /
undated publication counts (via ``src.services.tenure_scope.scoped_counts``,
the one shared definition — see that module's docstring), and — the check
this script exists for — whether the ON-DISK persona
(``profiles/public/{agent_id}.md``) lists any publication older than that
tenure year. D16 found the on-disk personas clean today only because of a
one-off manual re-export; nothing enforces it going forward except this
script (until it is wired into a recurring check).

Never writes anything: no DB write, no file write, no re-export.

Exit code is non-zero when any persona leaks a pre-tenure publication, so
this can be wired into a CI/cron check later.

Usage:
    python scripts/audit_tenure_scope.py                      # every PI
    python scripts/audit_tenure_scope.py --orcid 0000-...      # scoped (repeatable)
    python scripts/audit_tenure_scope.py --profiles-dir DIR    # override profiles/public
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, AppSetting, User  # noqa: E402
from src.services.jhu_rules import LEGACY_TENURE_KEY, TENURE_KEY_PREFIX  # noqa: E402
from src.services.tenure_scope import ScopedCount, scoped_counts  # noqa: E402

DEFAULT_PROFILES_DIR = Path("profiles/public")

# The export renders each publication line as (profile_export.py):
#   "- Title. *Journal*. (YYYY). https://..."
# so the year is the LAST parenthesized 4-digit group before the link, and it
# only ever appears once per bullet.
_YEAR_RE = re.compile(r"\((\d{4})\)")
_RECENT_PUBLICATIONS_HEADING = "## Recent Publications"


# ---------------------------------------------------------------------------
# Pure logic — no DB, no network, no filesystem. This is what the unit test
# exercises (the "persona-year parser").
# ---------------------------------------------------------------------------


def parse_persona_publication_years(markdown_text: str) -> list[int]:
    """Extract the years cited in a persona file's ``## Recent Publications``
    section only — never the whole document, which may contain unrelated
    4-digit numbers (grant years, PMIDs) outside that section."""
    if _RECENT_PUBLICATIONS_HEADING not in markdown_text:
        return []
    _, _, tail = markdown_text.partition(_RECENT_PUBLICATIONS_HEADING)
    section_lines: list[str] = []
    for line in tail.splitlines():
        if line.startswith("## "):
            break
        section_lines.append(line)
    years: list[int] = []
    for line in section_lines:
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        match = _YEAR_RE.search(stripped)
        if match:
            years.append(int(match.group(1)))
    return years


def find_persona_tenure_leaks(years: list[int], tenure_start: int | None) -> list[int]:
    """Years in the persona's publication list that precede the tenure start.

    ``tenure_start is None`` means no window is recorded at all (D20) — every
    year is in scope and nothing can leak against an unset window."""
    if tenure_start is None:
        return []
    return [y for y in years if y < tenure_start]


# ---------------------------------------------------------------------------
# I/O layer
# ---------------------------------------------------------------------------


@dataclass
class PiTenureReport:
    orcid: str
    name: str
    agent_id: str | None
    tenure_start: int | None
    tenure_source: str
    in_tenure: int
    before_tenure: int
    undated_excluded: int
    persona_checked: bool
    persona_leak_years: list[int]


async def _tenure_year_and_source(
    db, user_id: uuid.UUID, agent_id: str | None
) -> tuple[int | None, str]:
    """The tenure year AND its provenance — ``scoped_counts``/``tenure_start_map``
    return only the year, so this reads the same ``app_settings`` rows
    directly to recover ``source`` for the report."""
    row = await db.execute(
        select(AppSetting.value).where(AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}")
    )
    value = row.scalar_one_or_none()
    if value:
        try:
            parsed = json.loads(value)
            return int(parsed["year"]), str(parsed.get("source") or "unknown")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass

    if agent_id:
        row = await db.execute(
            select(AppSetting.value).where(AppSetting.key == LEGACY_TENURE_KEY)
        )
        legacy = row.scalar_one_or_none()
        if legacy:
            try:
                year = (json.loads(legacy) or {}).get(agent_id)
            except (ValueError, TypeError, json.JSONDecodeError):
                year = None
            if year is not None:
                return int(year), "legacy_map"

    return None, "none"


def _check_persona(agent_id: str | None, tenure_start: int | None, profiles_dir: Path) -> tuple[bool, list[int]]:
    if not agent_id:
        return False, []
    path = profiles_dir / f"{agent_id}.md"
    if not path.exists():
        return False, []
    text = path.read_text(encoding="utf-8")
    years = parse_persona_publication_years(text)
    return True, find_persona_tenure_leaks(years, tenure_start)


async def _build_report(
    db, user: User, agent_id: str | None, profiles_dir: Path
) -> PiTenureReport:
    tenure_start, source = await _tenure_year_and_source(db, user.id, agent_id)
    counts_by_user = await scoped_counts(db, [user.id], {user.id: agent_id})
    counts: ScopedCount = counts_by_user[user.id]
    persona_checked, leak_years = _check_persona(agent_id, tenure_start, profiles_dir)
    return PiTenureReport(
        orcid=user.orcid,
        name=user.name,
        agent_id=agent_id,
        tenure_start=tenure_start,
        tenure_source=source,
        in_tenure=counts.in_tenure,
        before_tenure=counts.before_tenure,
        undated_excluded=counts.undated_excluded,
        persona_checked=persona_checked,
        persona_leak_years=leak_years,
    )


async def _run(orcids: list[str], profiles_dir: Path) -> int:
    async with get_session_factory()() as db:
        q = select(User).where(User.user_role == "pi").order_by(User.orcid)
        if orcids:
            q = q.where(User.orcid.in_(orcids))
        users = (await db.execute(q)).scalars().all()

        agent_rows = (
            await db.execute(
                select(AgentRegistry.user_id, AgentRegistry.agent_id).where(
                    AgentRegistry.user_id.in_([u.id for u in users])
                )
            )
        ).all()
        agent_ids = {uid: aid for uid, aid in agent_rows}

        leaking = 0
        unscoped = 0
        for user in users:
            report = await _build_report(db, user, agent_ids.get(user.id), profiles_dir)
            print(
                json.dumps(
                    {
                        "orcid": report.orcid,
                        "name": report.name,
                        "agent_id": report.agent_id,
                        "tenure_start": report.tenure_start,
                        "tenure_source": report.tenure_source,
                        "in_tenure": report.in_tenure,
                        "before_tenure": report.before_tenure,
                        "undated_excluded": report.undated_excluded,
                        "persona_checked": report.persona_checked,
                        "persona_leak_years": report.persona_leak_years,
                    },
                    sort_keys=True,
                )
            )
            if report.tenure_start is None:
                unscoped += 1
            if report.persona_leak_years:
                leaking += 1
                print(
                    f"  LEAK: {report.name} ({report.orcid}) persona "
                    f"{report.agent_id}.md lists pre-tenure years "
                    f"{report.persona_leak_years} (tenure start {report.tenure_start})",
                    file=sys.stderr,
                )

        print(
            f"\n# {len(users)} PI(s); {unscoped} with no tenure start recorded "
            f"(full-career, D20); {leaking} persona(s) leaking a pre-tenure "
            "publication",
            file=sys.stderr,
        )
        return 1 if leaking else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--orcid", action="append", default=[], help="Scope to this ORCID (repeatable)")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help="Override profiles/public (default: %(default)s)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.orcid, args.profiles_dir)))


if __name__ == "__main__":
    main()
