"""Scrub NCBI API key values from stored ``jobs.last_error`` text (Q2 of the 2026-10-15
publication-handling design).

Rows written before the redaction at write time can carry ``api_key=<value>`` in any
status, completed included. This rewrites each such value to ``api_key=REDACTED`` with
``src.services.secret_redaction.redact_api_keys``, leaving the rest of the text as it is
(no truncation). Each write is conditional on the row still holding the text read at the
start; a row changed since then is skipped and counted as ``changed_since_read``.

It prints counts only, never a value or a job id. It writes no before-image, because a
before-image would hold the key. Preview by default; ``--apply`` writes. Exit 0; with
``--apply``, exit 1 when any row changed since it was read (re-run it). Any error ends the
run with exit 2 and only the exception's type printed: a database error's text carries
the statement's parameters, which include the unredacted ``last_error``. Nothing is
committed then (the one commit is the last statement).

    DC="docker compose -f docker-compose.prod.yml"
    $DC run --rm --no-deps -T blackbird-app python scripts/scrub_job_error_api_keys.py [--apply]
"""
import argparse
import asyncio
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import Job  # noqa: E402
from src.services.secret_redaction import redact_api_keys  # noqa: E402

#: ``job_status_enum`` order (src/models/job.py), the order the report lists statuses in.
STATUS_ORDER = ("pending", "processing", "completed", "failed", "dead")


@dataclass
class ScrubCounts:
    mentioning: Counter[str] = field(default_factory=Counter)
    holding: int = 0
    scrubbed: int = 0
    changed_since_read: int = 0


async def read_candidates(db) -> list[tuple[uuid.UUID, str, str]]:
    """``(id, status, last_error)`` of every job whose ``last_error`` contains
    ``api_key=`` in any case (``_`` escaped, so it is not a LIKE wildcard)."""
    rows = await db.execute(
        select(Job.id, Job.status, Job.last_error)
        .where(Job.last_error.ilike(r"%api\_key=%", escape="\\"))
    )
    return [(r.id, r.status, r.last_error) for r in rows]


async def scrub(db, rows: Sequence[tuple[uuid.UUID, str, str]], *, apply: bool) -> ScrubCounts:
    counts = ScrubCounts()
    for job_id, status, old in rows:
        counts.mentioning[status] += 1
        new = redact_api_keys(old)
        if new == old:
            continue
        counts.holding += 1
        if not apply:
            continue
        result = await db.execute(
            update(Job).where(Job.id == job_id, Job.last_error == old).values(last_error=new)
        )
        if result.rowcount == 0:
            counts.changed_since_read += 1
        else:
            counts.scrubbed += 1
    if apply:
        await db.commit()
    return counts


def report_lines(counts: ScrubCounts, *, apply: bool) -> list[str]:
    total = sum(counts.mentioning.values())
    by_status = ", ".join(
        f"{s}={counts.mentioning[s]}" for s in STATUS_ORDER if counts.mentioning[s]
    )
    lines = [
        f"jobs.last_error rows mentioning api_key=: {total} ({by_status})",
        f"  already redacted: {total - counts.holding}",
        f"  holding a key value: {counts.holding}",
        f"scrubbed: {counts.scrubbed}" if apply else f"would scrub: {counts.holding}",
    ]
    if apply:
        lines.append(f"changed since read, left for a re-run: {counts.changed_since_read}")
    return lines


def exit_code(counts: ScrubCounts, *, apply: bool) -> int:
    return 1 if apply and counts.changed_since_read > 0 else 0


async def _main(apply: bool) -> int:
    async with get_session_factory()() as db:
        counts = await scrub(db, await read_candidates(db), apply=apply)
    for line in report_lines(counts, apply=apply):
        print(line)
    return exit_code(counts, apply=apply)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="write the redacted text")
    args = parser.parse_args(argv)
    try:
        code = asyncio.run(_main(args.apply))
    except Exception as exc:  # its message may hold a key value: print the type only
        print(f"scrub failed: {type(exc).__name__}; nothing was committed", file=sys.stderr)
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
