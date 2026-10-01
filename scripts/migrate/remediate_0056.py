"""Remediations for the 0056 prechecks (spec §10.5). Dry run by default.

  --jobs          mark all but the OLDEST pending/processing row of each duplicate
                  (user_id, type) group `failed`, last_error "duplicate — superseded by <id>".
                  A `processing` row is left alone (the worker would write its result
                  over our `failed`) while a worker holds WORKER_LOCK_KEY, and also
                  unless --worker-stopped is given: the pre-Phase-3 worker that is
                  still running before 0056 never takes that lock. Stop the worker
                  (or wait for the job to finish) and re-run with --worker-stopped.
  --provisions    keep the NEWEST slack_app_provisions row per agent, delete the rest,
                  and list their app_ids for manual deletion in Slack
  --publications  LIST ONLY: each affected PI, the duplicate count, and whether a
                  duplicate sits in the exported top 20. The dedupe itself is
                  `scripts/repair_pi_corpus.py --only duplicate-pmids --apply`, run only
                  with the owner's go-ahead (it changes those PIs' exported
                  ## Recent Publications at their next export; spec §15.3).
  --emails        LIST ONLY: users whose emails differ only by case. Resolve by hand.

Take an explicit pg_dump before --apply (§12 step 0). Run from a one-off container
off the NEW image:
  $DC run --rm --no-deps -T blackbird-app python scripts/migrate/remediate_0056.py --jobs --provisions
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_JOBS_SQL = """
SELECT user_id::text, type::text, array_agg(id::text ORDER BY enqueued_at, id)
  FROM jobs
 WHERE status IN ('pending','processing') AND user_id IS NOT NULL
   AND type IN ('generate_profile','enrich_grants','industry_evidence')
 GROUP BY user_id, type HAVING count(*) > 1
"""
_JOB_STATUS_SQL = "SELECT id::text, status::text FROM jobs WHERE id = ANY(CAST(:ids AS uuid[]))"
_PROVISIONS_SQL = """
SELECT agent_registry_id::text, array_agg(id::text ORDER BY created_at DESC, id)
  FROM slack_app_provisions GROUP BY agent_registry_id HAVING count(*) > 1
"""
_PUBLICATIONS_SQL = """
WITH d AS (
  SELECT user_id, pmid, count(*) AS n FROM publications
   WHERE pmid IS NOT NULL GROUP BY user_id, pmid HAVING count(*) > 1
), ranked AS (
  SELECT p.user_id, p.pmid,
         row_number() OVER (PARTITION BY p.user_id
                            ORDER BY COALESCE(p.year, 0) DESC, p.id) AS pos
    FROM publications p WHERE p.title IS NOT NULL AND p.title <> ''
)
SELECT u.orcid, u.name, d.pmid, d.n,
       EXISTS (SELECT 1 FROM ranked r WHERE r.user_id = d.user_id
                 AND r.pmid = d.pmid AND r.pos <= 20) AS in_top_20
  FROM d JOIN users u ON u.id = d.user_id ORDER BY u.name, d.pmid
"""
_EMAILS_SQL = """
SELECT lower(email), array_agg(id::text || ' ' || email ORDER BY created_at, id)
  FROM users WHERE email IS NOT NULL GROUP BY lower(email) HAVING count(*) > 1
"""


def superseded_note(keep_id: str) -> str:
    return f"duplicate — superseded by {keep_id}"


def plan_job_remediation(rows) -> list[tuple[str, str]]:
    """``(job id, superseding id)`` for every row but the oldest of each group."""
    out: list[tuple[str, str]] = []
    for _user_id, _type, ids in rows:
        keep, *rest = ids
        out.extend((jid, keep) for jid in rest)
    return out


def plan_provision_remediation(rows) -> list[str]:
    """The provision ids to delete: every row but the newest of each agent's group."""
    out: list[str] = []
    for _agent, ids in rows:
        out.extend(ids[1:])
    return out


def worker_may_be_live(lock_held: bool, worker_stopped: bool) -> bool:
    """A held worker lock is a live worker. A free one proves nothing before 0056:
    the pre-Phase-3 worker image still serving then never takes the lock, so only
    the operator's ``--worker-stopped`` rules it out."""
    return lock_held or not worker_stopped


def split_for_live_worker(
    plan: list[tuple[str, str]], statuses: dict[str, str], worker_live: bool
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(actionable, skipped)``: with a live worker, a `processing` row is skipped
    because the worker will write its own terminal status over ours."""
    if not worker_live:
        return list(plan), []
    actionable = [p for p in plan if statuses.get(p[0]) != "processing"]
    skipped = [p for p in plan if statuses.get(p[0]) == "processing"]
    return actionable, skipped


async def _remediate_jobs(conn, apply: bool, worker_live: bool) -> None:
    from sqlalchemy import text

    plan = plan_job_remediation((await conn.execute(text(_JOBS_SQL))).all())
    statuses: dict[str, str] = {}
    if plan:
        statuses = dict((await conn.execute(
            text(_JOB_STATUS_SQL), {"ids": [jid for jid, _ in plan]}
        )).all())
    actionable, skipped = split_for_live_worker(plan, statuses, worker_live)
    for jid, keep in actionable:
        print(f"job {jid}: {'MARK failed' if apply else 'would mark failed'} ({superseded_note(keep)})")
        if apply:
            await conn.execute(text(
                "UPDATE jobs SET status = 'failed', last_error = :note, completed_at = now() "
                "WHERE id = CAST(:id AS uuid) AND status IN ('pending','processing')"
            ), {"note": superseded_note(keep), "id": jid})
    for jid, keep in skipped:
        print(f"job {jid}: SKIPPED, processing while a worker holds the worker lock "
              f"(superseded by {keep}); stop the worker or wait, then re-run")
    print(f"jobs: {len(actionable)} row(s), {len(skipped)} skipped")


async def _remediate_provisions(conn, apply: bool) -> None:
    from sqlalchemy import text

    ids = plan_provision_remediation((await conn.execute(text(_PROVISIONS_SQL))).all())
    for pid in ids:
        app = (await conn.execute(text(
            "SELECT app_id FROM slack_app_provisions WHERE id = CAST(:id AS uuid)"
        ), {"id": pid})).scalar_one_or_none()
        print(f"provision {pid}: {'DELETE' if apply else 'would delete'}; "
              f"orphaned Slack app to delete by hand: {app or '(no app_id recorded)'}")
        if apply:
            await conn.execute(text(
                "DELETE FROM slack_app_provisions WHERE id = CAST(:id AS uuid)"
            ), {"id": pid})
    print(f"provisions: {len(ids)} row(s)")


async def _list_publications(conn) -> None:
    from sqlalchemy import text

    rows = (await conn.execute(text(_PUBLICATIONS_SQL))).all()
    for orcid, name, pmid, n, top in rows:
        print(f"{name} ({orcid}) pmid={pmid} copies={n} in_exported_top_20={top}")
    print(f"publications: {len(rows)} duplicate group(s). Dedupe only with the owner's "
          "go-ahead: scripts/repair_pi_corpus.py --only duplicate-pmids --apply")


async def _list_emails(conn) -> None:
    from sqlalchemy import text

    rows = (await conn.execute(text(_EMAILS_SQL))).all()
    for lowered, users in rows:
        print(f"{lowered}: {users}")
    print(f"emails: {len(rows)} case-duplicate group(s); resolve by hand before 0056")


async def _main(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.config import get_settings
    from src.services.advisory_locks import WORKER_LOCK_KEY, advisory_lock_held

    url = args.database_url or os.environ.get("DATABASE_URL") or get_settings().database_url
    engine = create_async_engine(url)
    try:
        worker_live = False
        if args.jobs:
            async with AsyncSession(engine) as probe:
                lock_held = await advisory_lock_held(probe, WORKER_LOCK_KEY)
            worker_live = worker_may_be_live(lock_held, args.worker_stopped)
            if lock_held:
                print("a worker holds the worker lock: `processing` duplicates will be skipped")
            elif worker_live:
                print("`processing` duplicates are skipped unless --worker-stopped "
                      "(confirm the worker container is stopped first)")
        async with engine.connect() as conn:
            if args.jobs:
                await _remediate_jobs(conn, args.apply, worker_live)
            if args.provisions:
                await _remediate_provisions(conn, args.apply)
            if args.publications:
                await _list_publications(conn)
            if args.emails:
                await _list_emails(conn)
            if args.apply:
                await conn.commit()
            else:
                await conn.rollback()
                print("dry run: nothing written (pass --apply)")
    finally:
        await engine.dispose()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("--jobs", "--provisions", "--publications", "--emails", "--apply",
                 "--worker-stopped"):
        ap.add_argument(flag, action="store_true")
    ap.add_argument("--database-url")
    raise SystemExit(asyncio.run(_main(ap.parse_args())))


if __name__ == "__main__":
    main()
