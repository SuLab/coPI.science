"""Read-only verification of the corpus remediation after the regeneration of every PI
(spec 2026-10-05 §9 row 3; decision D68: runs unattended after the final regeneration).
Each check prints PASS <name> or FAIL <name> with details; exit 1 when any check fails.

  completeness        every PI (population, spec §3) has a generate_profile job completed
                      at or after --since, or a draft staged at or after it; the others are
                      listed by name with their newest job's status and error
  unanchored_applied  app_settings corpus_unanchored_report_applied exists, failed == []
  unstored_anchored   a fresh resolve per PI (network): every anchored record of ``ranked``
                      left unstored is listed as WARN, marked "older" when its PMID is below
                      the PI's largest stored PMID (the run should have stored it) and
                      "newer" otherwise. Unstored rows are report-only. A low or unreadable
                      shared OpenAlex meter stops the scan and fails for incomplete coverage
  davis               --davis-orcid: that PI's profile generated, or a draft staged, at or
                      after --since, and its newest generate_profile job completed
  no_id_names         no PI's name is empty, an ORCID iD or letter-less
  render_diff         every persona file equals a fresh render (render_persona_from_db)

The session is read-only (SET TRANSACTION READ ONLY, on one connection whose every
transaction is read-only), so nothing can be written.

  $DC run --rm --no-deps -T blackbird-app python scripts/verify_corpus_remediation.py \\
      --since <REGEN_TS> --davis-orcid <iD>
  ... --check completeness --check no_id_names        # only these (no network)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from scripts._bulk_enqueue import pi_population  # noqa: E402
from src.database import get_engine  # noqa: E402
from src.models import (  # noqa: E402
    AgentRegistry,
    AppSetting,
    Job,
    Publication,
    ResearcherProfile,
    User,
)
from src.services.corpus import CorpusStageError, resolve_corpus  # noqa: E402
from src.services.corpus_additions import is_anchored  # noqa: E402
from src.services.job_queue import JobDeferred  # noqa: E402
from src.services.openalex_budget import (  # noqa: E402
    JOB_CREDITS,
    bulk_requests,
    read_meter,
    wake_time,
)
from src.services.person_names import is_orcid_like  # noqa: E402
from src.services.profile_publish import persona_file_text, render_persona_from_db  # noqa: E402

CHECKS = ("completeness", "unanchored_applied", "unstored_anchored", "davis", "no_id_names",
          "render_diff")
APPLIED_KEY = "corpus_unanchored_report_applied"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: list[str]


async def _population(
    db: AsyncSession, orcids: Collection[str] | None,
) -> list[tuple[uuid.UUID, str, str]]:
    population = await pi_population(db)
    if orcids:
        population = [p for p in population if p[1] in set(orcids)]
    return population


async def _newest_profile_job(db: AsyncSession, user_id: uuid.UUID) -> Job | None:
    return (await db.execute(
        select(Job).where(Job.user_id == user_id, Job.type == "generate_profile")
        .order_by(Job.enqueued_at.desc()).limit(1)
    )).scalar_one_or_none()


async def _regenerated_since(db: AsyncSession, user_id: uuid.UUID, since: datetime) -> bool:
    """A generate_profile job completed at or after ``since``, or a draft staged then."""
    completed = (await db.execute(
        select(Job.id).where(
            Job.user_id == user_id, Job.type == "generate_profile",
            Job.status == "completed", Job.completed_at >= since,
        ).limit(1)
    )).first()
    if completed is not None:
        return True
    staged = (await db.execute(
        select(ResearcherProfile.user_id).where(
            ResearcherProfile.user_id == user_id,
            ResearcherProfile.pending_profile.isnot(None),
            ResearcherProfile.pending_profile_created_at >= since,
        )
    )).first()
    return staged is not None


def _job_line(job: Job | None) -> str:
    if job is None:
        return "no generate_profile job"
    error = (job.last_error or "").replace("\n", " ")[:200]
    return f"newest job {job.status}" + (f": {error}" if error else "")


async def check_completeness(
    db: AsyncSession, since: datetime, *, orcids: Collection[str] | None = None,
) -> CheckResult:
    details: list[str] = []
    for user_id, orcid, name in await _population(db, orcids):
        if await _regenerated_since(db, user_id, since):
            continue
        job = await _newest_profile_job(db, user_id)
        details.append(f"{name} ({orcid}): not regenerated since {since.isoformat()}; "
                       f"{_job_line(job)}")
    return CheckResult("completeness", not details, details)


async def check_unanchored_applied(db: AsyncSession) -> CheckResult:
    value = (await db.execute(
        select(AppSetting.value).where(AppSetting.key == APPLIED_KEY)
    )).scalar_one_or_none()
    if value is None:
        return CheckResult("unanchored_applied", False, [f"app_settings {APPLIED_KEY} missing"])
    try:
        marker = json.loads(value)
    except ValueError:
        return CheckResult("unanchored_applied", False, [f"unreadable marker: {value[:200]}"])
    failed = marker.get("failed") if isinstance(marker, dict) else None
    if failed != []:
        return CheckResult("unanchored_applied", False, [f"failed PIs: {failed}"])
    return CheckResult("unanchored_applied", True, [f"marker: {value}"])


def _pmid_int(pmid: str | None) -> int:
    try:
        return int(pmid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def check_unstored_anchored(
    db: AsyncSession, *, orcids: Collection[str] | None = None,
) -> CheckResult:
    """Unstored papers are report-only: a live resolve can find newly indexed papers.
    A failed resolve or interrupted budget check fails because coverage is unverified."""
    details: list[str] = []
    failed = False
    population = await _population(db, orcids)
    institutions = dict((await db.execute(
        select(User.id, User.institution).where(User.id.in_([p[0] for p in population]))
    )).all())
    for user_id, orcid, name in population:
        meter = await read_meter()
        if meter is None or wake_time(meter, JOB_CREDITS["generate_profile"],
                                      datetime.now(UTC)) is not None:
            details.append(f"Unverified {name} ({orcid}) and remaining PIs: "
                           "OpenAlex free budget cannot be confirmed; retry after reset")
            return CheckResult("unstored_anchored", False, details)
        stored = set((await db.execute(
            select(Publication.pmid).where(
                Publication.user_id == user_id, Publication.pmid.isnot(None))
        )).scalars())
        # End the read transaction before the network round trips.
        await db.commit()
        try:
            with bulk_requests():
                result = await resolve_corpus(orcid, name, institutions.get(user_id))
        except JobDeferred as exc:
            details.append(f"Unverified {name} ({orcid}) and remaining PIs: {exc}")
            return CheckResult("unstored_anchored", False, details)
        except CorpusStageError as exc:
            details.append(f"Unverified {name} ({orcid}): resolve failed: {exc}")
            failed = True
            continue
        newest_stored = max((_pmid_int(p) for p in stored), default=0)
        for rec in result.ranked:
            pmid = rec.get("pmid")
            if not pmid or pmid in stored or not is_anchored(rec):
                continue
            age = "older" if _pmid_int(pmid) < newest_stored else "newer"
            details.append(f"WARN {name} ({orcid}): {age} unstored anchored {pmid} "
                           f"({rec.get('year')}) {str(rec.get('title') or '')[:80]!r}")
    return CheckResult("unstored_anchored", not failed, details)


async def check_davis(db: AsyncSession, since: datetime, orcid: str) -> CheckResult:
    user = (await db.execute(select(User).where(User.orcid == orcid))).scalar_one_or_none()
    if user is None:
        return CheckResult("davis", False, [f"no user with ORCID {orcid}"])
    details: list[str] = []
    profile = (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )).scalar_one_or_none()
    generated = profile is not None and (
        (profile.profile_generated_at is not None and profile.profile_generated_at >= since)
        or (profile.pending_profile is not None
            and profile.pending_profile_created_at is not None
            and profile.pending_profile_created_at >= since)
    )
    if not generated:
        details.append(f"{user.name}: profile neither generated nor drafted since "
                       f"{since.isoformat()}")
    job = await _newest_profile_job(db, user.id)
    if job is None or job.status != "completed":
        details.append(f"{user.name}: {_job_line(job)}")
    return CheckResult("davis", not details, details)


async def check_no_id_names(
    db: AsyncSession, *, orcids: Collection[str] | None = None,
) -> CheckResult:
    details = [
        f"{orcid}: name {name!r}"
        for _uid, orcid, name in await _population(db, orcids)
        if not (name or "").strip() or is_orcid_like(name)
    ]
    return CheckResult("no_id_names", not details, details)


async def check_render_diff(db: AsyncSession) -> CheckResult:
    """Every agent with a user and a profile: its persona file equals a fresh render."""
    user_ids = (await db.execute(
        select(AgentRegistry.user_id).where(AgentRegistry.user_id.isnot(None))
    )).scalars().all()
    details: list[str] = []
    for user_id in user_ids:
        rendered = await render_persona_from_db(db, user_id)
        if rendered is None:
            continue
        agent, text_now = rendered
        on_disk = persona_file_text(agent.agent_id)
        if on_disk is None:
            details.append(f"{agent.agent_id}: persona file missing")
        elif on_disk != text_now:
            details.append(f"{agent.agent_id}: persona file differs from a fresh render")
    return CheckResult("render_diff", not details, details)


async def run(
    db: AsyncSession, *, since: datetime, checks: Collection[str], davis_orcid: str | None,
    orcids: Collection[str] | None = None,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in CHECKS:
        if name not in checks:
            continue
        if name == "completeness":
            results.append(await check_completeness(db, since, orcids=orcids))
        elif name == "unanchored_applied":
            results.append(await check_unanchored_applied(db))
        elif name == "unstored_anchored":
            results.append(await check_unstored_anchored(db, orcids=orcids))
        elif name == "davis":
            if not davis_orcid:
                results.append(CheckResult("davis", False, ["--davis-orcid is required"]))
            else:
                results.append(await check_davis(db, since, davis_orcid))
        elif name == "no_id_names":
            results.append(await check_no_id_names(db, orcids=orcids))
        elif name == "render_diff":
            results.append(await check_render_diff(db))
    return results


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _main(args: argparse.Namespace) -> int:
    checks = args.check or list(CHECKS)
    # One connection whose every transaction is read-only (the unstored_anchored check
    # commits between resolves), plus SET TRANSACTION READ ONLY for the first.
    async with get_engine().connect() as conn:
        await conn.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
        await conn.commit()
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            try:
                results = await run(db, since=args.since, checks=checks,
                                    davis_orcid=args.davis_orcid, orcids=args.orcid)
            finally:
                await db.rollback()
    for result in results:
        print(f"{'PASS' if result.passed else 'FAIL'} {result.name}"
              + ("" if result.passed else ": " + str(len(result.details))))
        for line in result.details:
            print(f"  {line}")
    return 0 if all(r.passed for r in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--since", required=True, type=_parse_since,
                        help="ISO 8601 start of the regeneration (a naive time is UTC)")
    parser.add_argument("--orcid", action="append", default=[],
                        help="scope the per-PI checks to this ORCID (repeatable)")
    parser.add_argument("--check", action="append", choices=CHECKS, default=[],
                        help="run only this check (repeatable; default: all)")
    parser.add_argument("--davis-orcid", help="the PI the davis check verifies")
    args = parser.parse_args()
    if "davis" in (args.check or CHECKS) and not args.davis_orcid:
        parser.error("--davis-orcid is required when the davis check runs")
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
