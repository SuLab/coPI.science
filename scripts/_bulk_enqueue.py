"""Paced bulk enqueue for every repair script (spec 2026-10-05 §4.2, D53, D67). Preview by default.

Jobs go in at BULK priority through job_queue.insert_job_if_absent (idempotent: a user
with a pending or processing job of the type gets none). OpenAlex use must stay inside
the keyless budget: 1000 credits a day per IP, shared with org1.

By default (adaptive pacing, D67) jobs are not staggered: the worker reads OpenAlex's
free daily meter before each BULK job that spends credits and defers the job to the
meter's reset once it would eat into the 10% reserve (src/services/openalex_budget.py).

`--fixed-schedule` restores the D53 stagger: each job's `not_before` is a slot sized for
the parent job's credits plus its step-10 follow-ons (OPENALEX_CREDITS_PER_JOB), times
RETRY_HEADROOM, because an automatic retry (4 then 16 minutes later) spends the budget
again, against DEFAULT_BUDGET_SHARE of the day. A staggered `generate_profile` parent
carries `followon_not_before` in its payload; step 10 gives its follow-ons that
not_before. Every job carries `bulk_tag`, so `--resume` re-enqueues only this run's dead
jobs and `--wait` counts only this run's jobs.

  $DC run --rm blackbird-app python scripts/_bulk_enqueue.py --type enrich_grants          # preview
  $DC run --rm blackbird-app python scripts/_bulk_enqueue.py --type enrich_grants --apply --wait
  $DC run --rm blackbird-app python scripts/_bulk_enqueue.py --type enrich_grants --tag <tag> --resume --apply --wait
  $DC run --rm blackbird-app python scripts/_bulk_enqueue.py --type industry_evidence --missing-scorer-version 2.0.0 --apply   # only PIs not yet scored at 2.0.0
  $DC run --rm blackbird-app python scripts/_bulk_enqueue.py --type industry_evidence --partial 2.0.0 --apply   # only PIs whose latest 2.0.0 row lost a source to a transient failure
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, or_, select, union  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, Job, PiIndustryScore, User  # noqa: E402
from src.models.job import BULK_PRIORITY  # noqa: E402
from src.services.industry_evidence import NOT_REFRESHED  # noqa: E402
from src.services.job_queue import insert_job_if_absent  # noqa: E402

OPENALEX_DAILY_CREDITS = 1000
DEFAULT_BUDGET_SHARE = 0.5
RETRY_HEADROOM = 2
#: OpenAlex credits one parent job spends, its follow-ons included. Estimates from the
#: call sites, not measurements.
OPENALEX_CREDITS_PER_JOB: dict[str, int] = {
    "enrich_grants": 0,         # RePORTER + ORCID only (src/services/grant_enrichment.py)
    "industry_evidence": 8,     # openalex_industry: /works per 100 PMIDs, /funders, /institutions
    "company_discovery": 0,
    "generate_profile": 13,     # corpus fetch_works_by_orcid (<= 5 pages) + its industry_evidence
}
TAG_KEY = "bulk_tag"


@dataclass(frozen=True)
class Slot:
    user_id: uuid.UUID
    not_before: datetime | None
    followon_not_before: datetime | None


async def pi_population(db: AsyncSession) -> list[tuple[uuid.UUID, str, str]]:
    """(user_id, orcid, name) of every per-PI predicate's population (spec §3):
    user_role 'pi' plus owners of a pi_lab agent, ordered by name."""
    ids = union(
        select(User.id.label("uid")).where(User.user_role == "pi"),
        select(AgentRegistry.user_id.label("uid")).where(
            AgentRegistry.role == "pi_lab", AgentRegistry.user_id.isnot(None)),
    ).subquery()
    rows = (await db.execute(
        select(User.id, User.orcid, User.name).join(ids, ids.c.uid == User.id)
        .order_by(User.name, User.id)
    )).all()
    return [(r.id, r.orcid, r.name) for r in rows]


async def users_with_scorer_version(db: AsyncSession, version: str) -> set[uuid.UUID]:
    """Users with a `version` industry score row that is not a veto's not-refreshed row
    (src/services/industry_evidence.py NOT_REFRESHED): the PIs `--missing-scorer-version`
    leaves out, because a real `industry_evidence` run already scored them at `version`."""
    rows = (await db.execute(
        select(PiIndustryScore.user_id, PiIndustryScore.coverage)
        .where(PiIndustryScore.scorer_version == version)
    )).all()
    return {uid for uid, coverage in rows if NOT_REFRESHED not in (coverage or {}).values()}


#: Coverage reasons a re-run cannot fix (a missing key or name), which `--partial` ignores.
PERMANENT_UNAVAILABLE = ("unavailable:no_api_key", "unavailable:no_usable_name")


async def users_with_transient_gaps(db: AsyncSession, version: str) -> set[uuid.UUID]:
    """Users whose latest `version` score row has a source "unavailable" for a reason a
    re-run can fix (an HTTP status, a transport error, not refreshed), i.e. not
    PERMANENT_UNAVAILABLE: the PIs `--partial` re-runs."""
    rows = (await db.execute(
        select(PiIndustryScore.user_id, PiIndustryScore.coverage)
        .where(PiIndustryScore.scorer_version == version)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc(),
                  PiIndustryScore.id.desc())
        .distinct(PiIndustryScore.user_id)  # DISTINCT ON (user_id): the latest row
    )).all()
    return {uid for uid, coverage in rows
            if any(str(v).startswith("unavailable:") and v not in PERMANENT_UNAVAILABLE
                   for v in (coverage or {}).values())}


def plan_schedule(user_ids: Sequence[uuid.UUID], *, job_type: str, start: datetime,
                  credits_per_job: int, daily_credits: float) -> list[Slot]:
    """One slot per user, in order, `86400 * credits_per_job * RETRY_HEADROOM /
    daily_credits` seconds apart from ``start``. A job that spends no credits is not
    staggered (every not_before None). A `generate_profile` slot's follow-ons go half an
    interval after it."""
    cost = credits_per_job * RETRY_HEADROOM
    if cost <= 0:
        return [Slot(uid, None, None) for uid in user_ids]
    interval = timedelta(seconds=86400 * cost / daily_credits)
    out = []
    for i, uid in enumerate(user_ids):
        at = start + i * interval
        follow = at + interval / 2 if job_type == "generate_profile" else None
        out.append(Slot(uid, at, follow))
    return out


async def _insert_slot(db: AsyncSession, job_type: str, slot: Slot,
                       payload: dict) -> uuid.UUID | None:
    if slot.followon_not_before is not None:
        payload["followon_not_before"] = slot.followon_not_before.isoformat()
    return await insert_job_if_absent(db, type=job_type, user_id=slot.user_id, payload=payload,
                                      priority=BULK_PRIORITY, not_before=slot.not_before)


async def enqueue_paced(db: AsyncSession, *, job_type: str,
                        users: Sequence[tuple[uuid.UUID, str]], tag: str, start: datetime,
                        credits_per_job: int, daily_credits: float) -> list[uuid.UUID]:
    """Insert one paced, tagged BULK job per (user_id, orcid), skipping users with an
    active one; returns the new ids. Commits."""
    orcid_by = dict(users)
    created: list[uuid.UUID] = []
    for slot in plan_schedule([u for u, _ in users], job_type=job_type, start=start,
                              credits_per_job=credits_per_job, daily_credits=daily_credits):
        payload = {"user_id": str(slot.user_id), "orcid": orcid_by[slot.user_id], TAG_KEY: tag}
        new_id = await _insert_slot(db, job_type, slot, payload)
        if new_id is not None:
            created.append(new_id)
    await db.commit()
    return created


def _newest_per_user(*criteria):
    """(user_id, status, payload, last_error) of each user's newest job matching ``criteria``."""
    return (
        select(Job.user_id, Job.status, Job.payload, Job.last_error)
        .where(*criteria)
        .order_by(Job.user_id, Job.enqueued_at.desc(), Job.id.desc())
        .distinct(Job.user_id)  # DISTINCT ON (user_id)
    )


async def _resumable(db: AsyncSession, job_type: str, tag: str,
                     user_ids: Sequence[uuid.UUID] | None = None) -> list[tuple[uuid.UUID, dict]]:
    """(user_id, payload) of every user whose newest ``job_type`` job tagged ``tag`` is dead;
    only users in ``user_ids`` when it is given (an empty sequence matches nobody)."""
    criteria = [Job.type == job_type, Job.payload[TAG_KEY].as_string() == tag]
    if user_ids is not None:
        criteria.append(Job.user_id.in_(list(user_ids)))
    rows = (await db.execute(_newest_per_user(*criteria))).all()
    return [(r.user_id, r.payload) for r in rows if r.status == "dead"]


async def resume_dead(db: AsyncSession, *, job_type: str, tag: str, credits_per_job: int,
                      daily_credits: float,
                      user_ids: Sequence[uuid.UUID] | None = None) -> list[uuid.UUID]:
    """Re-enqueue every user whose newest job of this type and tag is dead (skipping users
    with an active one), on a fresh schedule starting now: the same stagger as the first
    pass (D53). ``user_ids`` limits it to those users (``--orcid``,
    ``--missing-scorer-version``); None means every
    user with such a dead job. Returns the new ids. Commits."""
    dead = await _resumable(db, job_type, tag, user_ids)
    slots = plan_schedule([uid for uid, _ in dead], job_type=job_type, start=datetime.now(UTC),
                          credits_per_job=credits_per_job, daily_credits=daily_credits)
    created: list[uuid.UUID] = []
    for slot, (_, payload) in zip(slots, dead, strict=True):
        fresh = {k: v for k, v in (payload or {}).items()
                 if k not in ("progress", "followon_not_before")}
        new_id = await _insert_slot(db, job_type, slot, fresh)
        if new_id is not None:
            created.append(new_id)
    await db.commit()
    return created


def _this_run(job_type: str, user_ids: Sequence[uuid.UUID], tag: str, since: datetime):
    """Each user's newest job OF THIS RUN: payload bulk_tag = tag (which a requested
    rerun inherits) or enqueued at/after ``since``."""
    return _newest_per_user(
        Job.type == job_type, Job.user_id.in_(list(user_ids)),
        or_(Job.payload[TAG_KEY].as_string() == tag, Job.enqueued_at >= since),
    )


async def run_status(db: AsyncSession, *, job_type: str, user_ids: Sequence[uuid.UUID], tag: str,
                     since: datetime) -> dict[str, int]:
    """Status counts of each user's newest job of this run (`_this_run`). A dead job from
    an earlier run never counts; a user with no job of this run is absent."""
    newest = _this_run(job_type, user_ids, tag, since).subquery()
    rows = (await db.execute(
        select(newest.c.status, func.count()).group_by(newest.c.status))).all()
    return {status: n for status, n in rows}


async def wait_until_drained(session_factory, *, job_type: str, user_ids: Sequence[uuid.UUID],
                             tag: str, since: datetime, poll_seconds: float = 30.0,
                             timeout_seconds: float | None = None) -> dict[str, int]:
    """Block until none of ``user_ids`` has a pending or processing ``job_type`` job (any
    active job is current, whoever enqueued it), then return ``run_status``. Raises
    TimeoutError after ``timeout_seconds`` (None: wait forever)."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    while True:
        async with session_factory() as db:
            active = await db.scalar(select(func.count()).select_from(Job).where(
                Job.type == job_type, Job.user_id.in_(list(user_ids)),
                Job.status.in_(("pending", "processing"))))
        if not active:
            break
        print(f"  {active} {job_type} job(s) pending or processing ...", flush=True)
        if deadline is not None and loop.time() >= deadline:
            raise TimeoutError(f"{active} {job_type} job(s) still active")
        await asyncio.sleep(poll_seconds)
    async with session_factory() as db:
        return await run_status(db, job_type=job_type, user_ids=user_ids, tag=tag, since=since)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--type", required=True, choices=sorted(OPENALEX_CREDITS_PER_JOB))
    p.add_argument("--orcid", help="scope to one PI")
    p.add_argument("--tag", help="bulk_tag for this run (default <type>-<UTC date>)")
    p.add_argument("--credits-per-job", type=int,
                   help="OpenAlex credits per job (default from OPENALEX_CREDITS_PER_JOB)")
    p.add_argument("--daily-credits", type=float,
                   default=OPENALEX_DAILY_CREDITS * DEFAULT_BUDGET_SHARE)
    p.add_argument("--fixed-schedule", action="store_true",
                   help="stagger not_before by --credits-per-job/--daily-credits (D53) "
                        "instead of the worker's adaptive pacing (D67)")
    p.add_argument("--missing-scorer-version", metavar="VERSION",
                   help="only PIs with no industry score row at VERSION "
                        "(the catch-up after a SCORER_VERSION bump)")
    p.add_argument("--partial", metavar="VERSION",
                   help="only PIs whose latest VERSION score row lost a source to a failure "
                        "a re-run can fix (after the regeneration's 429s or outages)")
    p.add_argument("--apply", action="store_true", help="write jobs (default: preview)")
    p.add_argument("--resume", action="store_true", help="re-enqueue this tag's dead jobs")
    p.add_argument("--wait", action="store_true", help="block until the jobs drain")
    p.add_argument("--poll-seconds", type=float, default=30.0)
    p.add_argument("--since", type=datetime.fromisoformat,
                   help="ISO time --wait counts jobs from (default: this invocation's start)")
    a = p.parse_args(argv)
    if a.credits_per_job and not a.fixed_schedule:
        p.error("--credits-per-job only sizes --fixed-schedule slots; the worker's adaptive "
                "gate uses src/services/openalex_budget.py JOB_CREDITS")
    if a.credits_per_job is None:
        a.credits_per_job = OPENALEX_CREDITS_PER_JOB[a.type]
    if not a.fixed_schedule:
        # Adaptive pacing: plan_schedule staggers nothing for a zero-credit job.
        a.credits_per_job = 0
    if a.tag is None:
        a.tag = f"{a.type}-{datetime.now(UTC).date().isoformat()}"
    if a.since is not None and a.since.tzinfo is None:
        a.since = a.since.replace(tzinfo=UTC)
    return a


async def _preview(db: AsyncSession, a: argparse.Namespace,
                   population: list[tuple[uuid.UUID, str, str]], start: datetime) -> None:
    """Print who would be enqueued and when; writes nothing."""
    names = {uid: (orcid, name) for uid, orcid, name in population}
    if a.resume:
        dead = await _resumable(db, a.type, a.tag)
        user_ids = [uid for uid, _ in dead if uid in names]
    else:
        user_ids = list(names)
    slots = plan_schedule(user_ids, job_type=a.type, start=start,
                          credits_per_job=a.credits_per_job, daily_credits=a.daily_credits)
    for slot in slots:
        orcid, name = names[slot.user_id]
        at = slot.not_before.isoformat() if slot.not_before else "now"
        print(f"  {name}  {orcid}  not_before={at}")
    print(f"{len(slots)} {a.type} job(s) previewed (tag {a.tag}); --apply to enqueue")


async def _report_dead(session_factory, a: argparse.Namespace, names: dict,
                       since: datetime) -> None:
    """Each user name and last_error of this run's dead jobs."""
    async with session_factory() as db:
        rows = (await db.execute(_this_run(a.type, list(names), a.tag, since))).all()
    for r in rows:
        if r.status == "dead":
            print(f"  DEAD {names[r.user_id]}: {r.last_error}")


async def _main(argv: Sequence[str] | None = None) -> None:
    a = _parse_args(argv)
    since = a.since or datetime.now(UTC)
    print(f"since={since.isoformat()} tag={a.tag}")
    session_factory = get_session_factory()
    async with session_factory() as db:
        population = await pi_population(db)
        if a.orcid:
            population = [p for p in population if p[1] == a.orcid]
        if a.missing_scorer_version:
            scored = await users_with_scorer_version(db, a.missing_scorer_version)
            population = [p for p in population if p[0] not in scored]
            print(f"{len(population)} PI(s) without a {a.missing_scorer_version} score row")
        if a.partial:
            gaps = await users_with_transient_gaps(db, a.partial)
            population = [p for p in population if p[0] in gaps]
            print(f"{len(population)} PI(s) with a re-runnable gap in their {a.partial} row")
        if not a.apply:
            await _preview(db, a, population, datetime.now(UTC))
            return
        if a.resume:
            scope = ([uid for uid, _, _ in population]
                     if (a.orcid or a.missing_scorer_version or a.partial) else None)
            created = await resume_dead(db, job_type=a.type, tag=a.tag,
                                        credits_per_job=a.credits_per_job,
                                        daily_credits=a.daily_credits, user_ids=scope)
        else:
            created = await enqueue_paced(db, job_type=a.type,
                                          users=[(uid, orcid) for uid, orcid, _ in population],
                                          tag=a.tag, start=datetime.now(UTC),
                                          credits_per_job=a.credits_per_job,
                                          daily_credits=a.daily_credits)
    print(f"{len(created)} {a.type} job(s) enqueued (tag {a.tag})")
    if not a.wait:
        return
    names = {uid: name for uid, _, name in population}
    counts = await wait_until_drained(session_factory, job_type=a.type, user_ids=list(names),
                                      tag=a.tag, since=since, poll_seconds=a.poll_seconds)
    print(f"drained: {counts}")
    await _report_dead(session_factory, a, names, since)


if __name__ == "__main__":
    asyncio.run(_main())
