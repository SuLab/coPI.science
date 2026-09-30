"""Re-derive paper-derived JHU tenure starts against the strict corpus (RCA D7).

Before the strict corpus path, a failed PubMed batch or DOI conversion was
logged and swallowed, so ``resolve_corpus`` could return a silently thinned
corpus — and a tenure start derived from it (source ``earliest_hopkins_paper``)
may be later than the PI's real earliest Hopkins paper. ``get_tenure_start``
prefers a stored year on every later run, so those rows never self-correct.

Candidates: every per-user ``jhu_tenure_start:{user_id}`` row whose ``source``
is ``earliest_hopkins_paper``. Rows with any other source (``manual``,
``curated-2026-08-13``, ``orcid_employment``) and the legacy agent-id map are
never read as candidates and never rewritten. A paper-sourced row that
``--restore`` could not accept back from the backup — a year that is not a
JSON integer (``"2015"``, ``true``), or a key that is not
``jhu_tenure_start:<lowercase UUID>`` — is not a candidate either: it is
reported ``unparseable_value`` / ``unparseable_key`` and left as it is, since
one such row in the backup would make ``--restore`` refuse the whole file.

Re-derivation mirrors the profile pipeline's order for a PI with nothing
recorded (``src/services/profile_pipeline.py``, the tenure block):

1. fetch the ORCID profile; on failure skip the PI, no write (D8 — without
   employments, "no Hopkins employment" is not an answer);
2. ``derive_employment_start`` — a year here wins, source ``orcid_employment``;
3. otherwise ``resolve_corpus`` with the pipeline's arguments (an empty
   ``users.name``/``users.institution`` filled from ORCID, as the pipeline
   does), then ``derive_start_from_papers`` over ``ranked`` — the whole
   identity-gated corpus, not the ``CORPUS_CAP``-capped ``kept``, whose newest
   50 can miss the earliest Hopkins paper — source ``earliest_hopkins_paper``;
   any corpus exception skips the PI, no write, and so does a corpus that
   returned with ``permanently_dropped`` records (reason ``incomplete_corpus``:
   any of them could be the earliest Hopkins paper, which is the very error
   this script corrects);
4. no year at all: reported, left as it is. Nothing is ever deleted.

A row is CHANGED only when the re-derived year differs from the stored one; a
row whose year agrees is left untouched even if the source would differ.

Direction matters. The thinning this script corrects dropped papers, which
can only make a stored year too LATE, so the expected paper-derived change is
to an earlier year. A LATER paper-derived year would narrow the tenure window
and hide in-tenure papers; the preview marks it ``LATER (suspect)``, and
``--apply`` skips it (reason ``later_than_stored``, no write) unless
``--allow-later`` is given. An earlier year, and an ORCID-employment year in
either direction (the pipeline prefers employment over papers), apply without
it.

Usage (production: the app image, with host ``backups/`` mounted so the backup
outlives the ``run --rm`` container):

    DC="docker compose -f docker-compose.prod.yml"
    $DC run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app \
        python scripts/rederive_tenure_starts.py                  # preview, writes nothing
    $DC run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app \
        python scripts/rederive_tenure_starts.py --apply          # write changes + queue profiles
    $DC run --rm --no-deps -T -v "$PWD/backups:/app/backups" blackbird-app \
        python scripts/rederive_tenure_starts.py --restore /app/backups/tenure_starts_<stamp>.json

    --orcid 0000-...   scope the run (repeatable)
    --allow-later      let --apply write a re-derived year LATER than the stored one

``--apply`` refuses to start unless ``--backup-dir`` is a mount point
(``--allow-unmounted-backup-dir`` overrides), and aborts before any write when
the changes exceed ``--max-changes`` (default 10). Otherwise it first writes
``<backup-dir>/tenure_starts_<UTC stamp>.json`` (mode 0600), holding for every
candidate row its ``old`` raw value and the ``written`` value it is about to
store (``null`` for a row it leaves alone). Each write is then CONDITIONAL on
the row still holding exactly the value read at the start: the network calls
take minutes, and a manager's manual edit made meanwhile must win — such a row
is skipped and reported ``changed_since_read``. For each row written it queues
one ``generate_profile`` job unless one is already PENDING; a PROCESSING job
has already read the old year, so a new pending job is queued behind it and
reported. Everything commits once. The profile pipeline queues
``enrich_grants`` and ``industry_evidence`` itself, so this script does not.

``--restore`` puts a row's ``old`` value back only if the row still holds the
``written`` value and its user still exists; any other row is skipped and
reported (it was edited since, or its PI was deleted — a deleted PI's key is
never resurrected). It accepts only the backup format this script writes, and
validates every key and value before writing anything. It does NOT re-queue
profiles: a profile regenerated under the re-derived year keeps that scope
until its next regeneration.

Network: live ORCID/OpenAlex/PubMed per candidate; no LLM call here. Each
queued profile costs about one synthesis LLM call on the worker.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import exists, select, update  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AppSetting, Job, User  # noqa: E402
from src.services import orcid, pubmed  # noqa: E402
from src.services.corpus import CorpusStageError, resolve_corpus  # noqa: E402
from src.services.jhu_rules import (  # noqa: E402
    TENURE_KEY_PREFIX,
    derive_employment_start,
    derive_start_from_papers,
)
from src.services.orcid import fetch_orcid_profile  # noqa: E402
from src.services.profile_jobs import enqueue_profile_job_if_absent  # noqa: E402
from src.services.profile_pipeline import CORPUS_CAP  # noqa: E402

PAPER_SOURCE = "earliest_hopkins_paper"
EMPLOYMENT_SOURCE = "orcid_employment"
DEFAULT_BACKUP_DIR = Path("/app/backups")
DEFAULT_MAX_CHANGES = 10
# Marks a backup as this script's own; ``--restore`` accepts nothing else.
BACKUP_FORMAT = "rederive_tenure_starts/2"
_KEY_RE = re.compile(
    "^" + re.escape(TENURE_KEY_PREFIX)
    + r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass
class Candidate:
    key: str
    raw_value: str
    user_id: uuid.UUID
    name: str
    orcid: str
    institution: str | None
    stored_year: int


@dataclass
class Outcome:
    candidate: Candidate
    new_year: int | None = None
    new_source: str | None = None
    skip_reason: str | None = None

    @property
    def changed(self) -> bool:
        return (
            self.skip_reason is None
            and self.new_year is not None
            and self.new_year != self.candidate.stored_year
        )

    @property
    def later(self) -> bool:
        """A paper-derived change that moves the start LATER — narrowing the
        tenure window, the opposite of what re-deriving from a complete corpus
        corrects. An ORCID-employment year is not suspect in either direction:
        the pipeline prefers it over any paper year."""
        return (
            self.changed
            and self.new_source == PAPER_SOURCE
            and self.new_year > self.candidate.stored_year
        )


async def load_candidates(
    db, orcids: list[str]
) -> tuple[list[Candidate], list[str], list[tuple[str, str]]]:
    """Every ``earliest_hopkins_paper`` per-user row, optionally scoped by ORCID.

    Returns ``(candidates, orphan_keys, unparseable)``. An orphan is a
    paper-sourced key with no matching user, which cannot be re-derived and is
    only reported (and only when the run is unscoped). ``unparseable`` lists
    ``(key, reason)`` for a paper-sourced row the backup could not carry
    through ``--restore``: its key and value must pass the same checks
    ``_validate_backup`` applies (``_KEY_RE``; ``_is_tenure_value`` — an int
    year that is not a bool), or one such row would make ``--restore`` refuse
    the whole file. It is reported under the same scoping as an orphan (a
    key whose UUID names a user outside ``orcids`` is not reported). A value
    that is not JSON or names no source has no knowable source and is not
    considered at all.
    """
    rows = (
        await db.execute(
            select(AppSetting.key, AppSetting.value).where(
                AppSetting.key.startswith(TENURE_KEY_PREFIX, autoescape=True)
            )
        )
    ).all()
    parsed: dict[uuid.UUID, tuple[str, str, int]] = {}
    bad: dict[uuid.UUID | None, list[tuple[str, str]]] = {}
    for key, value in rows:
        try:
            entry = json.loads(value)
            if entry.get("source") != PAPER_SOURCE:
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        try:
            uid: uuid.UUID | None = uuid.UUID(key[len(TENURE_KEY_PREFIX):])
        except ValueError:
            uid = None
        if uid is None or not _KEY_RE.match(key):
            bad.setdefault(uid, []).append((key, "unparseable_key"))
            continue
        if not _is_tenure_value(value):
            bad.setdefault(uid, []).append((key, "unparseable_value"))
            continue
        parsed[uid] = (key, value, entry["year"])
    wanted = [u for u in (*parsed, *bad) if u is not None]
    users = []
    if wanted:
        stmt = select(User).where(User.id.in_(wanted))
        if orcids:
            stmt = stmt.where(User.orcid.in_(orcids))
        users = (
            await db.execute(stmt.order_by(User.name, User.id))
        ).scalars().all()
    in_scope = {u.id for u in users}
    candidates = [
        Candidate(
            key=parsed[u.id][0],
            raw_value=parsed[u.id][1],
            user_id=u.id,
            name=u.name,
            orcid=u.orcid,
            institution=u.institution,
            stored_year=parsed[u.id][2],
        )
        for u in users
        if u.id in parsed
    ]
    orphans = [] if orcids else [
        parsed[uid][0] for uid in parsed if uid not in in_scope
    ]
    unparseable = [
        item
        for uid, items in bad.items()
        if not orcids or uid in in_scope
        for item in items
    ]
    return candidates, orphans, unparseable


async def rederive(candidate: Candidate) -> Outcome:
    """Re-derive one candidate's start. Never writes."""
    outcome = Outcome(candidate)
    try:
        orcid_profile = await fetch_orcid_profile(candidate.orcid)
    except Exception as exc:
        outcome.skip_reason = f"orcid_unavailable: {type(exc).__name__}: {exc}"
        return outcome

    year = derive_employment_start(orcid_profile.get("employments") or [])
    if year is not None:
        outcome.new_year, outcome.new_source = year, EMPLOYMENT_SOURCE
        return outcome

    # The pipeline fills an empty users.name / users.institution from the
    # ORCID profile before it resolves the corpus; pass the values it would.
    name = candidate.name or orcid_profile.get("name")
    institution = candidate.institution or orcid_profile.get("institution")
    try:
        corpus = await resolve_corpus(
            candidate.orcid, name, institution, cap=CORPUS_CAP
        )
    except CorpusStageError as exc:
        outcome.skip_reason = f"corpus_stage_failed: {exc}"
        return outcome
    except Exception as exc:
        outcome.skip_reason = f"corpus_error: {type(exc).__name__}: {exc}"
        return outcome

    if corpus.permanently_dropped:
        # Returned normally, but missing records a retry would not recover;
        # any of them could be the earliest Hopkins paper.
        sample = ", ".join(corpus.permanently_dropped[:5])
        outcome.skip_reason = (
            f"incomplete_corpus: {len(corpus.permanently_dropped)} records "
            f"permanently unavailable ({sample}) (row left as it is)"
        )
        return outcome

    # ``ranked``, not ``kept``: the cap keeps the newest CORPUS_CAP records,
    # which can exclude the earliest Hopkins paper of a prolific PI.
    year = derive_start_from_papers(corpus.ranked)
    if year is None:
        outcome.skip_reason = "no_rederived_year (row left as it is)"
        return outcome
    outcome.new_year, outcome.new_source = year, PAPER_SOURCE
    return outcome


def _line(o: Outcome) -> str:
    c = o.candidate
    head = f"{c.user_id}  {c.name}  {c.orcid}  {c.stored_year} -> "
    if o.skip_reason is not None:
        return head + f"SKIP {o.skip_reason}"
    if o.later:
        tag = "CHANGE LATER (suspect)"
    else:
        tag = "CHANGE" if o.changed else "same"
    return head + f"{o.new_year} ({o.new_source}) {tag}"


def _tenure_value(year: int, source: str) -> str:
    """The raw value ``set_tenure_start`` would store (same JSON shape)."""
    return json.dumps(
        {
            "year": int(year),
            "source": source,
            "derived_at": datetime.now(UTC).isoformat(),
        }
    )


def _write_backup(
    candidates: list[Candidate], written: dict[str, str], backup_dir: Path
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"tenure_starts_{stamp}.json"
    doc = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "rows": [
            {"key": c.key, "old": c.raw_value, "written": written.get(c.key)}
            for c in candidates
        ],
    }
    # O_EXCL: never overwrite an earlier backup. 0600: the file names PIs.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return path


async def _swap_value(db, key: str, expected: str, new: str) -> bool:
    """Set ``key`` to ``new`` only if it still holds exactly ``expected``."""
    result = await db.execute(
        update(AppSetting)
        .where(AppSetting.key == key, AppSetting.value == expected)
        .values(value=new)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def _has_pending_profile_job(db, user_id: uuid.UUID) -> bool:
    found = await db.execute(
        select(Job.id)
        .where(
            Job.user_id == user_id,
            Job.type == "generate_profile",
            Job.status == "pending",
        )
        .limit(1)
    )
    return found.scalars().first() is not None


async def _has_processing_profile_job(db, user_id: uuid.UUID) -> bool:
    found = await db.execute(
        select(Job.id)
        .where(
            Job.user_id == user_id,
            Job.type == "generate_profile",
            Job.status == "processing",
        )
        .limit(1)
    )
    return found.scalars().first() is not None


async def run(
    db,
    *,
    orcids: list[str],
    apply: bool,
    max_changes: int = DEFAULT_MAX_CHANGES,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    allow_unmounted_backup_dir: bool = False,
    allow_later: bool = False,
) -> int:
    """Preview (default) or apply. Returns the process exit code.

    Under ``apply`` a change to a LATER year is skipped (``later_than_stored``)
    unless ``allow_later``; the preview only marks it ``LATER (suspect)``.
    """
    if apply and not allow_unmounted_backup_dir and not os.path.ismount(backup_dir):
        print(
            f"ABORT: backup dir {backup_dir} is not a mount point, so the backup "
            "would die with the container. Mount host backups/ there "
            '(-v "$PWD/backups:/app/backups") or pass '
            "--allow-unmounted-backup-dir. Nothing read or written."
        )
        return 2
    candidates, orphans, unparseable = await load_candidates(db, orcids)
    outcomes = [await rederive(c) for c in candidates]
    if apply and not allow_later:
        for o in outcomes:
            if o.later:
                o.skip_reason = (
                    f"later_than_stored: re-derived {o.new_year} "
                    "(row left as it is; --allow-later to apply)"
                )
    for o in outcomes:
        print(_line(o))
    for key in orphans:
        print(f"{key}  SKIP no_user (row left as it is)")
    for key, reason in unparseable:
        print(f"{key}  SKIP {reason} (row left as it is)")
    changes = [o for o in outcomes if o.changed]
    skipped = sum(1 for o in outcomes if o.skip_reason is not None)
    print(
        f"candidates={len(outcomes)} changes={len(changes)} "
        f"skipped={skipped} unchanged={len(outcomes) - len(changes) - skipped}"
    )
    if not apply:
        print("Preview only: nothing written, nothing queued. Re-run with --apply.")
        return 0
    if len(changes) > max_changes:
        print(
            f"ABORT: {len(changes)} changes exceed --max-changes={max_changes}; "
            "nothing written."
        )
        return 2
    if not changes:
        print("Nothing to change; no backup written.")
        return 0

    # Fix the exact values before the backup, so the backup records what is
    # written and ``--restore`` can tell a row this run wrote from a later edit.
    written = {
        o.candidate.key: _tenure_value(o.new_year, o.new_source) for o in changes
    }
    backup = _write_backup(candidates, written, backup_dir)
    print(f"Backup: {backup}")
    rewritten = queued = already_pending = behind_processing = 0
    for o in changes:
        c = o.candidate
        if not await _swap_value(db, c.key, c.raw_value, written[c.key]):
            print(f"{c.user_id}  {c.name}  {c.orcid}  SKIP changed_since_read (row left as it is)")
            continue
        rewritten += 1
        if await _has_pending_profile_job(db, c.user_id):
            already_pending += 1
            continue
        # A processing job read the old year before this write; its output is
        # stale, so queue a fresh one behind it rather than treating it as
        # covering this change.
        if await _has_processing_profile_job(db, c.user_id):
            behind_processing += 1
            print(
                f"{c.user_id}  {c.name}  {c.orcid}  NOTE a generate_profile job is "
                "processing on the old year; queued a new one behind it"
            )
            # The one deliberate direct construction left: the helper would
            # return the processing job.
            db.add(
                Job(
                    type="generate_profile",
                    user_id=c.user_id,
                    payload={"user_id": str(c.user_id), "orcid": c.orcid},
                )
            )
        else:
            user = await db.get(User, c.user_id)
            if user is None or await enqueue_profile_job_if_absent(db, user) is None:
                continue
        queued += 1
    await db.commit()
    print(
        f"Applied: rewritten={rewritten} "
        f"changed_since_read={len(changes) - rewritten} "
        f"profile_jobs_queued={queued} "
        f"profile_jobs_queued_behind_processing={behind_processing} "
        f"profile_jobs_already_pending={already_pending}"
    )
    return 0


def _is_tenure_value(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        entry = json.loads(raw)
    except ValueError:
        return False
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("year"), int)
        and not isinstance(entry.get("year"), bool)
        and isinstance(entry.get("source"), str)
    )


def _validate_backup(doc: object) -> str | None:
    """Why ``doc`` is not a backup this script wrote, or None if it is."""
    if not isinstance(doc, dict) or doc.get("format") != BACKUP_FORMAT:
        return f"not a {BACKUP_FORMAT} backup"
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return "no rows list"
    for n, row in enumerate(rows):
        if not isinstance(row, dict):
            return f"row {n} is not an object"
        if not isinstance(row.get("key"), str) or not _KEY_RE.match(row["key"]):
            return f"row {n}: {row.get('key')!r} is not a per-user tenure key"
        if not _is_tenure_value(row.get("old")):
            return f"row {n}: old value is not tenure JSON"
        if row.get("written") is not None and not _is_tenure_value(row["written"]):
            return f"row {n}: written value is not tenure JSON"
    return None


async def restore(db, path: Path) -> int:
    """Undo one ``--apply`` from its backup file. Returns the exit code.

    Validates the whole file before any write, then restores a row's ``old``
    value only where the row still holds the ``written`` value and its user
    still exists. It never inserts, so a purged key stays purged.
    """
    doc = json.loads(path.read_text(encoding="utf-8"))
    problem = _validate_backup(doc)
    if problem is not None:
        print(f"ABORT: {path}: {problem}; nothing written.")
        return 2
    restored = skipped = 0
    for row in doc["rows"]:
        key, old, written = row["key"], row["old"], row["written"]
        if written is None:
            continue  # --apply left this row alone; nothing to undo
        uid = uuid.UUID(key[len(TENURE_KEY_PREFIX):])
        user_exists = (
            await db.execute(select(exists().where(User.id == uid)))
        ).scalar()
        if not user_exists:
            print(f"{key}  SKIP user_deleted (not resurrected)")
            skipped += 1
            continue
        if not await _swap_value(db, key, written, old):
            print(f"{key}  SKIP changed_since_apply (row left as it is)")
            skipped += 1
            continue
        restored += 1
    await db.commit()
    print(f"Restored {restored} tenure rows from {path}; skipped {skipped}.")
    return 0


def _require_strict_corpus() -> None:
    """Refuse to run on an image without the strict corpus path: re-deriving
    through the swallowing one would reproduce the thinning this script exists
    to correct."""
    needs = (
        (pubmed.fetch_pubmed_records, ("strict", "permanently_dropped")),
        (pubmed.convert_dois_to_pmids, ("strict", "permanently_dropped")),
        (orcid.fetch_orcid_works, ("strict",)),
    )
    for fn, params in needs:
        missing = [p for p in params if p not in inspect.signature(fn).parameters]
        if missing:
            sys.exit(
                f"{fn.__name__} lacks {', '.join(missing)}; this image predates "
                "the strict corpus path."
            )


async def _main(args: argparse.Namespace) -> int:
    async with get_session_factory()() as db:
        if args.restore is not None:
            return await restore(db, args.restore)
        return await run(
            db,
            orcids=args.orcid,
            apply=args.apply,
            max_changes=args.max_changes,
            backup_dir=args.backup_dir,
            allow_unmounted_backup_dir=args.allow_unmounted_backup_dir,
            allow_later=args.allow_later,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes and queue profiles")
    mode.add_argument("--restore", type=Path, default=None, help="Undo an --apply from its backup file")
    parser.add_argument("--orcid", action="append", default=[], help="Scope to this ORCID (repeatable)")
    parser.add_argument("--max-changes", type=int, default=DEFAULT_MAX_CHANGES)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument(
        "--allow-later",
        action="store_true",
        help="Permit --apply to write a re-derived year later than the stored one",
    )
    parser.add_argument(
        "--allow-unmounted-backup-dir",
        action="store_true",
        help="Permit --apply when --backup-dir is not a mount point",
    )
    args = parser.parse_args()
    if args.restore is None:
        _require_strict_corpus()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
