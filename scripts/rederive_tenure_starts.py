"""Re-derive paper-derived JHU tenure starts against the strict corpus (RCA D7).

Before the strict corpus path, a failed PubMed batch or DOI conversion was
logged and swallowed, so ``resolve_corpus`` could return a silently thinned
corpus — and a tenure start derived from it (source ``earliest_hopkins_paper``)
may be later than the PI's real earliest Hopkins paper. ``get_tenure_start``
prefers a stored year on every later run, so those rows never self-correct.

Candidates: every per-user ``jhu_tenure_start:{user_id}`` row whose ``source``
is ``earliest_hopkins_paper``. Rows with any other source (``manual``,
``curated-2026-08-13``, ``orcid_employment``) and the legacy agent-id map are
never read as candidates and never rewritten.

Re-derivation mirrors the profile pipeline's order for a PI with nothing
recorded (``src/services/profile_pipeline.py``, the tenure block):

1. fetch the ORCID profile; on failure skip the PI, no write (D8 — without
   employments, "no Hopkins employment" is not an answer);
2. ``derive_employment_start`` — a year here wins, source ``orcid_employment``;
3. otherwise ``resolve_corpus`` with the pipeline's arguments, then
   ``derive_start_from_papers`` over ``kept``, source
   ``earliest_hopkins_paper``; a ``CorpusStageError`` (or any other corpus
   failure) skips the PI, no write;
4. no year at all: reported, left as it is. Nothing is ever deleted.

A row is CHANGED only when the re-derived year differs from the stored one; a
row whose year agrees is left untouched even if the source would differ.

Modes:
    python scripts/rederive_tenure_starts.py                    # preview, writes nothing
    python scripts/rederive_tenure_starts.py --apply            # write changes + queue profiles
    python scripts/rederive_tenure_starts.py --orcid 0000-...   # scope (repeatable)
    python scripts/rederive_tenure_starts.py --restore /app/backups/tenure_starts_<stamp>.json

``--apply`` aborts before any write when the changes exceed ``--max-changes``
(default 10). Otherwise it first saves every candidate row's key and raw value
to ``<backup-dir>/tenure_starts_<UTC stamp>.json``, then upserts each change,
queues one ``generate_profile`` job per changed PI (unless one is already
pending or processing), and commits once. The profile pipeline queues
``enrich_grants`` and ``industry_evidence`` itself, so this script does not.

``--restore`` re-applies every saved raw value (``derived_at`` included). It
does not re-queue profiles: a profile regenerated under the re-derived year
keeps that scope until its next regeneration.

Network: live ORCID/OpenAlex/PubMed per candidate; no LLM call here. Each
queued profile costs about one synthesis LLM call on the worker.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AppSetting, Job, User  # noqa: E402
from src.services import pubmed  # noqa: E402
from src.services.corpus import CorpusStageError, resolve_corpus  # noqa: E402
from src.services.jhu_rules import (  # noqa: E402
    TENURE_KEY_PREFIX,
    derive_employment_start,
    derive_start_from_papers,
    set_tenure_start,
)
from src.services.orcid import fetch_orcid_profile  # noqa: E402
from src.services.profile_pipeline import CORPUS_CAP  # noqa: E402

PAPER_SOURCE = "earliest_hopkins_paper"
EMPLOYMENT_SOURCE = "orcid_employment"
DEFAULT_BACKUP_DIR = Path("/app/backups")
DEFAULT_MAX_CHANGES = 10


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


async def load_candidates(
    db, orcids: list[str]
) -> tuple[list[Candidate], list[str]]:
    """Every ``earliest_hopkins_paper`` per-user row, optionally scoped by ORCID.

    Returns ``(candidates, orphan_keys)``: an orphan is a paper-sourced key
    with no matching user, which cannot be re-derived and is only reported
    (and only when the run is unscoped). An unreadable value or a key whose
    suffix is not a UUID has no knowable source and is not a candidate.
    """
    rows = (
        await db.execute(
            select(AppSetting.key, AppSetting.value).where(
                AppSetting.key.startswith(TENURE_KEY_PREFIX, autoescape=True)
            )
        )
    ).all()
    parsed: dict[uuid.UUID, tuple[str, str, int]] = {}
    for key, value in rows:
        try:
            entry = json.loads(value)
            if entry.get("source") != PAPER_SOURCE:
                continue
            uid = uuid.UUID(key[len(TENURE_KEY_PREFIX):])
            parsed[uid] = (key, value, int(entry["year"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    if not parsed:
        return [], []
    stmt = select(User).where(User.id.in_(list(parsed)))
    if orcids:
        stmt = stmt.where(User.orcid.in_(orcids))
    users = (await db.execute(stmt.order_by(User.name, User.id))).scalars().all()
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
    ]
    found = {c.user_id for c in candidates}
    orphans = [] if orcids else [
        parsed[uid][0] for uid in parsed if uid not in found
    ]
    return candidates, orphans


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

    # The pipeline fills an empty users.institution from the ORCID profile
    # before it resolves the corpus; pass the same value it would.
    institution = candidate.institution or orcid_profile.get("institution")
    try:
        corpus = await resolve_corpus(
            candidate.orcid, candidate.name, institution, cap=CORPUS_CAP
        )
    except CorpusStageError as exc:
        outcome.skip_reason = f"corpus_stage_failed: {exc}"
        return outcome
    except Exception as exc:
        outcome.skip_reason = f"corpus_error: {type(exc).__name__}: {exc}"
        return outcome

    year = derive_start_from_papers(corpus.kept)
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
    tag = "CHANGE" if o.changed else "same"
    return head + f"{o.new_year} ({o.new_source}) {tag}"


def _write_backup(candidates: list[Candidate], backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"tenure_starts_{stamp}.json"
    doc = {
        "created_at": datetime.now(UTC).isoformat(),
        "rows": [{"key": c.key, "value": c.raw_value} for c in candidates],
    }
    # "x": never overwrite an earlier backup.
    with path.open("x", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.flush()
    return path


async def _has_open_profile_job(db, user_id: uuid.UUID) -> bool:
    found = await db.execute(
        select(Job.id)
        .where(
            Job.user_id == user_id,
            Job.type == "generate_profile",
            Job.status.in_(("pending", "processing")),
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
) -> int:
    """Preview (default) or apply. Returns the process exit code."""
    candidates, orphans = await load_candidates(db, orcids)
    outcomes = [await rederive(c) for c in candidates]
    for o in outcomes:
        print(_line(o))
    for key in orphans:
        print(f"{key}  SKIP no_user (row left as it is)")
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

    backup = _write_backup(candidates, backup_dir)
    print(f"Backup: {backup}")
    queued = already_open = 0
    for o in changes:
        c = o.candidate
        await set_tenure_start(c.user_id, o.new_year, o.new_source, db=db)
        if await _has_open_profile_job(db, c.user_id):
            already_open += 1
            continue
        db.add(
            Job(
                type="generate_profile",
                user_id=c.user_id,
                payload={"user_id": str(c.user_id), "orcid": c.orcid},
            )
        )
        queued += 1
    await db.commit()
    print(
        f"Applied: rewritten={len(changes)} profile_jobs_queued={queued} "
        f"profile_jobs_already_open={already_open}"
    )
    return 0


async def restore(db, path: Path) -> int:
    """Re-apply every raw value saved in a backup file. Returns the exit code."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc["rows"]
    for row in rows:
        if not row["key"].startswith(TENURE_KEY_PREFIX):
            print(f"ABORT: {row['key']!r} is not a per-user tenure key; nothing written.")
            return 2
    for row in rows:
        stmt = pg_insert(AppSetting).values(key=row["key"], value=row["value"])
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSetting.key], set_={"value": row["value"]}
        )
        await db.execute(stmt)
    await db.commit()
    print(f"Restored {len(rows)} tenure rows from {path}.")
    return 0


def _require_strict_corpus() -> None:
    """Refuse to run on an image without the strict corpus path: re-deriving
    through the swallowing one would reproduce the thinning this script exists
    to correct."""
    for fn in (pubmed.fetch_pubmed_records, pubmed.convert_dois_to_pmids):
        if "strict" not in inspect.signature(fn).parameters:
            sys.exit(f"{fn.__name__} has no strict mode; this image predates it.")


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
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write changes and queue profiles")
    mode.add_argument("--restore", type=Path, default=None, help="Re-apply a backup file")
    parser.add_argument("--orcid", action="append", default=[], help="Scope to this ORCID (repeatable)")
    parser.add_argument("--max-changes", type=int, default=DEFAULT_MAX_CHANGES)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    args = parser.parse_args()
    if args.restore is None:
        _require_strict_corpus()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
