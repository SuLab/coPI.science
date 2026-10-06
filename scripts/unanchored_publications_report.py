"""Per-PI corpus report and the one-off unanchored-rows repair (spec 2026-10-05 §6.3,
D17; Phase 3 data repair 1-2). Re-resolves every PI (resolve_corpus, paced like the
pipeline) and compares the stored corpus with the uncapped ``ranked`` list.

--dry-run (writes nothing) lists, per PI: anchored records of ``ranked`` not stored (the
additions the old 50-cap blocked), metadata refreshes and year changes the next
generation will make on stored non-manual rows, and the stored rows absent from the
anchored records of ``ranked`` (the unanchored set); then every account whose name is an
ORCID iD or holds no letter. --apply sets provenance = 'unanchored' on exactly that set
(manual and excluded rows skipped), one PI per transaction under the persona and corpus
locks, and records the run in app_settings key ``corpus_unanchored_report_applied``
(JSON: at, pis, rows_marked, failed). A PI whose resolve fails is listed under failed and
skipped. It enqueues no job.

OpenAlex: one works-by-ORCID request per page per PI, on the free daily budget.
No credits are reserved for org1. Before each PI and charged request/retry the meter
is read (openalex_budget.read_meter, free); when the available credits are exhausted,
the run stops
with exit 1 and prints the ORCIDs still to do: re-run with those ``--orcid`` flags after
the reset (00:00 UTC). An --apply run that stopped early records no marker. An
unreadable meter stops the run without making a corpus request.

  $DC run --rm --no-deps -T blackbird-app python scripts/unanchored_publications_report.py --dry-run
  $DC run --rm --no-deps -T blackbird-app python scripts/unanchored_publications_report.py --apply

Exit 0 when every PI was resolved (and, with --apply, marked); 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from scripts._bulk_enqueue import pi_population  # noqa: E402
from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, AppSetting, Publication, User  # noqa: E402
from src.models.publication import PROVENANCE_MANUAL, PROVENANCE_UNANCHORED  # noqa: E402
from src.services.corpus import CorpusResult, CorpusStageError, resolve_corpus  # noqa: E402
from src.services.corpus_additions import (  # noqa: E402
    REFRESHED_FIELDS,
    is_anchored,
    lock_corpus,
    metadata_changes,
)
from src.services.job_queue import JobDeferred  # noqa: E402
from src.services.openalex_budget import (  # noqa: E402
    JOB_CREDITS,
    bulk_requests,
    read_meter,
    wake_time,
)
from src.services.person_names import is_orcid_like  # noqa: E402
from src.services.profile_publish import lock_persona_writer  # noqa: E402

APPLIED_KEY = "corpus_unanchored_report_applied"


@dataclass
class Comparison:
    """What one resolve says about a PI's stored rows (``compare``)."""

    #: (pmid, year, title) of anchored records of ``ranked`` with no stored row
    unstored_anchored: list[tuple[str, int | None, str]] = field(default_factory=list)
    #: (pmid, fields) of stored non-manual rows whose metadata the next run refreshes
    metadata_refresh: list[tuple[str, list[str]]] = field(default_factory=list)
    #: (pmid, stored year, resolved year)
    year_changes: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    #: (publication id, pmid, title) of stored rows absent from the anchored records
    unanchored: list[tuple[uuid.UUID, str | None, str]] = field(default_factory=list)


@dataclass(kw_only=True)
class PiReport(Comparison):
    user_id: uuid.UUID
    orcid: str
    name: str
    error: str | None = None


def compare(rows: Sequence[Publication], result: CorpusResult) -> Comparison:
    """Pure: the stored ``rows`` of one PI against one resolve's uncapped ``ranked``.
    ``rows`` may be ``Publication`` rows or ``_snapshot``s of them."""
    returned: dict[str, dict] = {}
    for rec in result.ranked:
        pmid = rec.get("pmid")
        if pmid and pmid not in returned:
            returned[pmid] = rec
    anchored = {pmid: rec for pmid, rec in returned.items() if is_anchored(rec)}
    stored_pmids = {row.pmid for row in rows if row.pmid}
    out = Comparison()
    out.unstored_anchored = [
        (pmid, rec.get("year"), rec.get("title") or "")
        for pmid, rec in anchored.items() if pmid not in stored_pmids
    ]
    for row in rows:
        if row.provenance == PROVENANCE_MANUAL:
            continue
        rec = returned.get(row.pmid) if row.pmid else None
        if rec is not None:
            changes = metadata_changes(row, rec)
            if changes:
                out.metadata_refresh.append((row.pmid, list(changes)))
            if "year" in changes:
                out.year_changes.append((row.pmid, row.year, changes["year"]))
        if row.excluded_at is None and (row.pmid is None or row.pmid not in anchored):
            out.unanchored.append((row.id, row.pmid, row.title or ""))
    return out


_SNAPSHOT_FIELDS = ("id", "pmid", "provenance", "excluded_at", *REFRESHED_FIELDS)


def _snapshot(row: Publication) -> SimpleNamespace:
    """The row's compared fields as plain values, readable after the commit expires the
    ORM object (an async session cannot lazy-load)."""
    return SimpleNamespace(**{name: getattr(row, name) for name in _SNAPSHOT_FIELDS})


async def build_report(
    db: AsyncSession, user_id: uuid.UUID, orcid: str, name: str, institution: str | None,
) -> PiReport:
    """Read the PI's rows, end the read transaction, resolve, compare. A failed resolve
    is recorded in ``error``."""
    rows = [_snapshot(row) for row in (await db.execute(
        select(Publication).where(Publication.user_id == user_id)
    )).scalars().all()]
    # No connection sits idle in a transaction across the network round trips.
    await db.commit()
    report = PiReport(user_id=user_id, orcid=orcid, name=name)
    try:
        with bulk_requests():
            result = await resolve_corpus(orcid, name, institution)
    except (CorpusStageError, JobDeferred) as exc:
        report.error = str(exc)
        return report
    for key, value in vars(compare(rows, result)).items():
        setattr(report, key, value)
    return report


async def apply_report(db: AsyncSession, report: PiReport) -> int:
    """Set provenance 'unanchored' on the report's unanchored rows, in one transaction
    under ``lock_persona_writer`` then ``lock_corpus``, re-reading the rows under the
    locks (a row staff kept or excluded since the report is skipped). Returns the number
    of rows whose provenance changed. Commits; rolls back and re-raises on error."""
    ids = [pub_id for pub_id, _pmid, _title in report.unanchored]
    if report.error or not ids:
        return 0
    try:
        await lock_persona_writer(db, report.user_id)
        await lock_corpus(db, report.user_id)
        rows = (await db.execute(
            select(Publication).where(
                Publication.user_id == report.user_id, Publication.id.in_(ids),
            )
        )).scalars().all()
        marked = 0
        for row in rows:
            if row.provenance in (PROVENANCE_MANUAL, PROVENANCE_UNANCHORED):
                continue
            if row.excluded_at is not None:
                continue
            row.provenance = PROVENANCE_UNANCHORED
            marked += 1
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return marked


async def id_named_accounts(db: AsyncSession) -> list[tuple[str, str, str | None]]:
    """(name, role, agent_id) of every account, any role, whose name is an ORCID iD or
    holds no letter (``person_names.is_orcid_like``)."""
    rows = (await db.execute(
        select(User.name, User.user_role, AgentRegistry.agent_id)
        .outerjoin(AgentRegistry, AgentRegistry.user_id == User.id)
        .order_by(User.name)
    )).all()
    return [(n, str(role), agent) for n, role, agent in rows if is_orcid_like(n)]


async def _budget_low() -> str | None:
    """A reason to stop when the free budget cannot cover one more resolve; None
    to go on. Repair work stops when the available free credits cannot be confirmed."""
    meter = await read_meter()
    if meter is None:
        return "OpenAlex free budget meter unreadable; available credits cannot be confirmed"
    if wake_time(meter, JOB_CREDITS["generate_profile"], datetime.now(UTC)) is not None:
        return (f"OpenAlex free budget: {meter.remaining} of {meter.limit} credits left, "
                f"resets in {meter.reset_seconds} s")
    return None


def _print_report(report: PiReport) -> None:
    print(f"\n=== {report.name} ({report.orcid}) ===")
    if report.error:
        print(f"  FAILED to resolve: {report.error}")
        return
    print(f"  unstored_anchored ({len(report.unstored_anchored)}):")
    for pmid, year, title in report.unstored_anchored:
        print(f"    - {pmid} ({year}) {title[:80]!r}")
    print(f"  metadata_refresh ({len(report.metadata_refresh)}):")
    for pmid, fields in report.metadata_refresh:
        print(f"    - {pmid}: {', '.join(fields)}")
    print(f"  year_changes ({len(report.year_changes)}):")
    for pmid, old, new in report.year_changes:
        print(f"    - {pmid}: {old} -> {new}")
    print(f"  unanchored_rows ({len(report.unanchored)}):")
    for _pub_id, pmid, title in report.unanchored:
        print(f"    - {pmid} {title[:80]!r}")


async def _record_marker(db: AsyncSession, *, pis: int, rows_marked: int,
                         failed: list[str]) -> None:
    value = json.dumps({"at": datetime.now(UTC).isoformat(), "pis": pis,
                        "rows_marked": rows_marked, "failed": failed})
    stmt = pg_insert(AppSetting).values(key=APPLIED_KEY, value=value)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": value}))
    await db.commit()


async def run(db: AsyncSession, *, orcids: list[str], apply: bool) -> int:
    """Report on (and with ``apply`` mark) every PI of the population, or only
    ``orcids``. Returns the number of PIs that failed or were not reached (a budget
    stop); ``main`` maps it to the exit status."""
    population = await pi_population(db)
    if orcids:
        wanted = set(orcids)
        population = [p for p in population if p[1] in wanted]
    institutions = dict((await db.execute(
        select(User.id, User.institution).where(User.id.in_([p[0] for p in population]))
    )).all())
    failed: list[str] = []
    totals = {"unstored_anchored": 0, "metadata_refresh": 0, "year_changes": 0,
              "unanchored_rows": 0, "rows_marked": 0}
    done = 0
    stopped: list[str] = []
    for index, (user_id, orcid, name) in enumerate(population):
        reason = await _budget_low()
        if reason is not None:
            stopped = [p[1] for p in population[index:]]
            print(f"\nSTOPPED before {name} ({orcid}): {reason}")
            break
        report = await build_report(db, user_id, orcid, name, institutions.get(user_id))
        _print_report(report)
        done += 1
        if report.error:
            failed.append(orcid)
            continue
        totals["unstored_anchored"] += len(report.unstored_anchored)
        totals["metadata_refresh"] += len(report.metadata_refresh)
        totals["year_changes"] += len(report.year_changes)
        totals["unanchored_rows"] += len(report.unanchored)
        if apply:
            try:
                marked = await apply_report(db, report)
            except Exception as exc:  # one PI's failure must not end the run
                print(f"  apply FAILED: {type(exc).__name__}: {exc}")
                failed.append(orcid)
                continue
            totals["rows_marked"] += marked
            print(f"  marked unanchored: {marked}")

    id_named = await id_named_accounts(db)
    await db.commit()
    print("\n=== Accounts named by an ORCID iD or with no letter ===")
    for acct_name, role, agent_id in id_named:
        print(f"  - {acct_name!r} role={role} agent={agent_id}")
    if not id_named:
        print("  none")

    print("\n=== Totals ===")
    print(f"  PIs resolved: {done} of {len(population)}")
    for key, count in totals.items():
        if key != "rows_marked" or apply:
            print(f"  {key}: {count}")
    print(f"  id_named_accounts: {len(id_named)}")
    print(f"  failed ({len(failed)}): {', '.join(failed) or 'none'}")
    if stopped:
        print(f"  not reached ({len(stopped)}); resume after the reset with:")
        print("    " + " ".join(f"--orcid {o}" for o in stopped))
    elif apply:
        await _record_marker(db, pis=done, rows_marked=totals["rows_marked"], failed=failed)
        print(f"  recorded app_settings {APPLIED_KEY}")
    if not apply:
        print("  [dry run] nothing written")
    return len(failed) + len(stopped)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    mode.add_argument("--apply", action="store_true",
                      help="mark the unanchored rows and record the run marker")
    parser.add_argument("--orcid", action="append", default=[],
                        help="scope to this ORCID (repeatable)")
    args = parser.parse_args()

    async def _main() -> int:
        async with get_session_factory()() as db:
            return await run(db, orcids=args.orcid, apply=args.apply)

    sys.exit(1 if asyncio.run(_main()) else 0)


if __name__ == "__main__":
    main()
