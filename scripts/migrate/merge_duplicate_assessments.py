"""Merge duplicate (simulation_run_id, thread_id) assessment groups before 0055.

Spec §10.3. Runs AFTER 0054 (its columns must exist) and BEFORE 0055 (whose
precheck refuses while any group remains). Dry run by default; ``--apply``
writes. One short transaction per group, so a failure costs one group, not the
run. It refuses to run at all while an engine holds the engine lock
(``simulation_control.engine_alive``): a live engine would be writing verdict
rows while this tool deletes them. ``--apply`` itself holds that lock
(``SessionAdvisoryLock(url, ENGINE_LOCK_KEY)``) for the whole run, so no engine
can start or resume while it merges, and it is refused if the lock cannot be
taken. The dry run takes no lock.

For each group, oldest to newest by created_at:

1. ``SELECT ... FOR UPDATE`` every row of the group first, so a concurrent FK
   insert (a review or chat turn) blocks and then fails visibly after the
   delete instead of being cascade-deleted (C24, SA4-08). ``lock_timeout`` is
   10 s.
2. Keep the newest row; set its ``verdict_revision`` to the group size n. Its
   own chat turns get ``verdict_revision = n`` so replay keeps them (SA4-19).
3. Re-point the older rows' children onto the kept row: reviews, review events
   and prompt-change suggestions; assignments, ONE row per assignee whom the
   kept row does not already have (``uq_review_assignment_once``); chat turns,
   stamped with their source row's rank (1..n-1) so B6 replay excludes them;
   chat usage rows; and pending/processing/failed jobs whose
   ``payload->>'assessment_id'`` names an older row (a failed job is retried).
4. ``summary_posted_at`` and ``summary_claimed_at`` become the group's newest
   values on the kept row, so a thread announced through any of its rows stays
   announced and a thread whose headline is in doubt (claimed, not posted)
   keeps its claim (P0-08's by-thread claim already refuses a thread with any
   posted row). No correction headline is posted.
5. Record each older row's ``raw_verdict`` as a ``duplicate_thread_verdict``
   drop with ``detail = "merged before 0055"``.
6. Delete the older rows. NULL-thread rows are never grouped or touched. The
   tool never deletes ``simulation_runs``.

Exit codes: 0 success (or nothing to do), 1 a group failed, 64 usage error,
75 an engine is alive (or, for ``--apply``, the engine lock is held). Take an explicit ``pg_dump`` of the database before
``--apply`` (spec §12 step 0).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

EX_USAGE = 64
EX_ENGINE_ALIVE = 75

GROUPS_SQL = (
    "SELECT simulation_run_id, thread_id FROM opportunity_assessments "
    "WHERE thread_id IS NOT NULL GROUP BY simulation_run_id, thread_id "
    "HAVING count(*) > 1 ORDER BY simulation_run_id, thread_id"
)

#: Job states that can still run or be retried; a completed/dead job is history.
REPOINT_JOB_STATUSES = ("pending", "processing", "failed")


@dataclass
class MergeResult:
    run_id: uuid.UUID
    thread_id: str
    kept_id: uuid.UUID
    deleted_ids: list[uuid.UUID] = field(default_factory=list)


def normalise_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://"):]
    if dsn.startswith("postgres://"):
        return "postgresql+asyncpg://" + dsn[len("postgres://"):]
    return dsn


def redact_dsn(dsn: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", dsn)


async def find_groups(conn) -> list[tuple[uuid.UUID, str]]:
    from sqlalchemy import text

    return [(r.simulation_run_id, r.thread_id) for r in (await conn.execute(text(GROUPS_SQL))).all()]


async def merge_group(conn, run_id: uuid.UUID, thread_id: str) -> MergeResult:
    """Merge one group inside the caller's transaction (see module docstring)."""
    from sqlalchemy import text

    rows = (await conn.execute(text(
        "SELECT id, raw_verdict, agent_id, subject_agent_id "
        "FROM opportunity_assessments "
        "WHERE simulation_run_id = :r AND thread_id = :t "
        "ORDER BY created_at, id FOR UPDATE"
    ), {"r": run_id, "t": thread_id})).all()
    n = len(rows)
    if n < 2:
        raise RuntimeError(f"group ({run_id}, {thread_id}) has {n} row(s); nothing to merge")
    kept = rows[-1]
    older = rows[:-1]
    older_ids = [r.id for r in older]
    await conn.execute(text(
        "UPDATE opportunity_assessments SET verdict_revision = :n, "
        "summary_posted_at = (SELECT max(summary_posted_at) FROM opportunity_assessments "
        "WHERE simulation_run_id = :r AND thread_id = :t), "
        "summary_claimed_at = (SELECT max(summary_claimed_at) FROM opportunity_assessments "
        "WHERE simulation_run_id = :r AND thread_id = :t) WHERE id = :k"
    ), {"n": n, "r": run_id, "t": thread_id, "k": kept.id})
    await conn.execute(text(
        "UPDATE assessment_chat_turns SET verdict_revision = :n WHERE assessment_id = :k"
    ), {"n": n, "k": kept.id})
    for rank, row in enumerate(older, start=1):
        await conn.execute(text(
            "UPDATE assessment_chat_turns SET assessment_id = :k, verdict_revision = :rank "
            "WHERE assessment_id = :o"
        ), {"k": kept.id, "rank": rank, "o": row.id})
    for table in ("assessment_reviews", "assessment_review_events", "prompt_change_suggestions",
                  "assessment_chat_usage"):
        await conn.execute(text(
            f"UPDATE {table} SET assessment_id = :k WHERE assessment_id = ANY(:olds)"
        ), {"k": kept.id, "olds": older_ids})
    # One assignment per assignee: two older rows may carry the same assignee, and
    # the kept row may already have one. The rest cascade away with their rows.
    await conn.execute(text(
        "UPDATE assessment_review_assignments SET assessment_id = :k WHERE id IN ("
        "SELECT DISTINCT ON (assignee_user_id) id FROM assessment_review_assignments "
        "WHERE assessment_id = ANY(:olds) AND assignee_user_id NOT IN ("
        "SELECT assignee_user_id FROM assessment_review_assignments WHERE assessment_id = :k) "
        "ORDER BY assignee_user_id, created_at, id)"
    ), {"k": kept.id, "olds": older_ids})
    await conn.execute(text(
        # jobs.payload is JSON, not JSONB (src/models/job.py): edit as jsonb, store as json.
        "UPDATE jobs SET payload = jsonb_set(payload::jsonb, '{assessment_id}', "
        "to_jsonb(CAST(:k AS text)))::json "
        "WHERE CAST(status AS text) = ANY(:statuses) "
        "AND payload->>'assessment_id' = ANY(:olds)"
    ), {
        "k": str(kept.id),
        "statuses": list(REPOINT_JOB_STATUSES),
        "olds": [str(i) for i in older_ids],
    })
    for row in older:
        await conn.execute(text(
            "INSERT INTO assessment_drops(id, simulation_run_id, agent_id, subject_agent_id, "
            "thread_id, reason, detail, raw_verdict, created_at) VALUES "
            "(gen_random_uuid(), :r, :a, :s, :t, 'duplicate_thread_verdict', "
            "'merged before 0055', CAST(:v AS jsonb), now())"
        ), {
            "r": run_id, "a": row.agent_id, "s": row.subject_agent_id, "t": thread_id,
            "v": None if row.raw_verdict is None else json.dumps(row.raw_verdict),
        })
    await conn.execute(text(
        "DELETE FROM opportunity_assessments WHERE id = ANY(:olds)"
    ), {"olds": older_ids})
    return MergeResult(run_id=run_id, thread_id=thread_id, kept_id=kept.id, deleted_ids=older_ids)


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", required=True, help="DSN of the database to merge")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    return ap


async def _engine_alive(conn) -> bool:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.services.simulation_control import engine_alive

    return await engine_alive(AsyncSession(bind=conn))


_REFUSED_ALIVE = ("REFUSED: an engine holds the engine lock. Stop the run and confirm "
                  "with docker ps that no engine is up, then retry.")


async def run(dsn: str, *, apply: bool) -> int:
    """Dry run, or with ``apply`` merge while holding the engine lock."""
    if not apply:
        return await _run(dsn, apply=False)
    from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock

    lock = SessionAdvisoryLock(normalise_dsn(dsn), ENGINE_LOCK_KEY)
    try:
        if not await lock.acquire():
            print(_REFUSED_ALIVE, file=sys.stderr)
            return EX_ENGINE_ALIVE
        return await _run(dsn, apply=True)
    finally:
        await lock.release()


async def _run(dsn: str, *, apply: bool) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(normalise_dsn(dsn), pool_pre_ping=False)
    failures = 0
    try:
        async with engine.connect() as conn:
            # Under --apply this process holds the lock itself, so the liveness
            # read would see it; the acquire in run() was that check.
            alive = False if apply else await _engine_alive(conn)
            await conn.rollback()
            if alive:
                print(_REFUSED_ALIVE, file=sys.stderr)
                return EX_ENGINE_ALIVE
            groups = await find_groups(conn)
            await conn.rollback()
        print(f"{len(groups)} duplicate (run, thread) group(s) in {redact_dsn(dsn)}")
        for run_id, thread_id in groups:
            try:
                async with engine.connect() as conn:
                    trans = await conn.begin()
                    try:
                        await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                        result = await merge_group(conn, run_id, thread_id)
                        print(f"  run {run_id} thread {thread_id}: keep {result.kept_id}, "
                              f"merge {len(result.deleted_ids)} older row(s)"
                              + ("" if apply else " (dry run, rolled back)"))
                    except BaseException:
                        await trans.rollback()
                        raise
                    if apply:
                        await trans.commit()
                    else:
                        await trans.rollback()
            except Exception as exc:  # noqa: BLE001 — one group must not stop the rest
                failures += 1
                print(f"  run {run_id} thread {thread_id}: FAILED {type(exc).__name__}: {exc}",
                      file=sys.stderr)
    finally:
        await engine.dispose()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_arg_parser().parse_args(argv)
    except SystemExit as exc:
        return EX_USAGE if exc.code else 0
    return asyncio.run(run(args.database_url, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
