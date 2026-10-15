"""Dry run of the OpenAlex attribution re-check over the stored ``openalex`` rows (owner
decision Q11; R4.7 of the 2026-10-15 publication-handling design). Read-only: it has no
``--apply`` and writes nothing.

For every PI with an in-use, PMID-less ``openalex`` row (within
``scripts/_bulk_enqueue.pi_population`` and any ``--orcid``), ordered by name, it reads the
ORCID profile's organisations, fetches the PI's OpenAlex authorship list once
(``non_pubmed_works.orcid_authorships_by_doi``: 1 to 5 charged list requests on the free daily
OpenAlex budget, under ``openalex_budget.bulk_requests``) and classifies each row with the
pipeline's own rule (``non_pubmed_works.attribution_verdict``): would be excluded
("unattributed"), kept ("attributed"), or left alone because this run's OpenAlex data has no
authorship for it ("unknown"). The owner reviews the output before the all-PI regeneration.

Before each PI the free meter is read (``_budget_low``, the unanchored report's); when it
cannot cover another PI the run stops and prints the ORCIDs still to do, as ``--orcid`` flags
for after the reset (00:00 UTC).

    $DC run --rm --no-deps -T blackbird-app python scripts/openalex_attribution_report.py [--orcid X]...

Exit 0 when every PI was reached and classified; 1 otherwise (a budget stop, an unreadable
ORCID profile, or a failed or deferred authorship fetch).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from scripts._bulk_enqueue import pi_population  # noqa: E402
from scripts.unanchored_publications_report import _budget_low  # noqa: E402
from src.database import get_session_factory  # noqa: E402
from src.models import Publication  # noqa: E402
from src.models.publication import PROVENANCE_OPENALEX  # noqa: E402
from src.services.non_pubmed_works import (  # noqa: E402
    attribution_verdict,
    orcid_authorships_by_doi,
)
from src.services.openalex_budget import bulk_requests  # noqa: E402
from src.services.orcid import fetch_orcid_profile  # noqa: E402
from src.services.pubmed import _doi_fold  # noqa: E402


@dataclass(frozen=True)
class StoredWork:
    """One in-use ``openalex`` row, as plain values."""

    doi: str
    year: int | None
    title: str


@dataclass
class PiWorks:
    user_id: uuid.UUID
    orcid: str
    name: str
    rows: list[StoredWork] = field(default_factory=list)


def classify_openalex_rows(
    rows: Sequence[StoredWork],
    authorships: Mapping[str, Mapping[str, Any] | None],
    organizations: Sequence[str],
) -> tuple[list[StoredWork], list[StoredWork], list[StoredWork]]:
    """Pure: ``(would_exclude, kept, no_data)`` by the pipeline's rule
    (``attribution_verdict`` of the row's folded DOI): "unattributed", "attributed" and
    "unknown" respectively."""
    would_exclude: list[StoredWork] = []
    kept: list[StoredWork] = []
    no_data: list[StoredWork] = []
    if not organizations:  # F36: same skip as the pipeline; nothing would be excluded
        return [], [], list(rows)
    for row in rows:
        verdict = attribution_verdict(authorships.get(_doi_fold(row.doi)), organizations)
        if verdict == "unattributed":
            would_exclude.append(row)
        elif verdict == "attributed":
            kept.append(row)
        else:
            no_data.append(row)
    return would_exclude, kept, no_data


async def load_population(db: AsyncSession, orcids: Sequence[str]) -> list[PiWorks]:
    """The PIs to report on and their in-use ``openalex`` rows, read in a read-only
    transaction that is ended before any network call."""
    await db.execute(text("SET TRANSACTION READ ONLY"))
    rows = (await db.execute(
        select(Publication.user_id, Publication.doi, Publication.year, Publication.title)
        .where(
            Publication.provenance == PROVENANCE_OPENALEX, Publication.pmid.is_(None),
            Publication.excluded_at.is_(None), Publication.doi.isnot(None),
        )
    )).all()
    by_user: dict[uuid.UUID, list[StoredWork]] = {}
    for user_id, doi, year, title in rows:
        by_user.setdefault(user_id, []).append(StoredWork(doi, year, title or ""))
    wanted = set(orcids)
    population = [
        PiWorks(user_id, orcid, name,
                sorted(by_user[user_id], key=lambda w: (-(w.year or 0), w.doi)))
        for user_id, orcid, name in await pi_population(db)
        if user_id in by_user and (not wanted or orcid in wanted)
    ]
    await db.commit()  # nothing was written; no connection idles in a transaction below
    return population


def _print_rows(heading: str, rows: Sequence[StoredWork]) -> None:
    print(f"  {heading} ({len(rows)}):")
    for row in rows:
        print(f"    - {row.doi} ({row.year}) {row.title[:80]!r}")


def _print_pi(
    would_exclude: Sequence[StoredWork], kept: Sequence[StoredWork],
    no_data: Sequence[StoredWork], authorships: Mapping[str, Mapping[str, Any] | None],
    organizations: Sequence[str],
) -> None:
    print(f"  would_exclude ({len(would_exclude)}):")
    for row in would_exclude:
        authorship = authorships.get(_doi_fold(row.doi)) or {}
        names = [str(n) for n in authorship.get("institutions") or []][:3]
        raw = [str(r)[:120] for r in authorship.get("raw_affiliations") or []][:2]
        print(f"    - {row.doi} ({row.year}) {row.title[:80]!r}")
        print(f"        institutions: {', '.join(names) or 'none'}; raw: "
              f"{' | '.join(raw) or 'none'}")
    _print_rows("kept", kept)
    _print_rows("no OpenAlex authorship this run", no_data)
    print(f"  ORCID organisations: {len(organizations)}")


async def report(pis: Sequence[PiWorks]) -> int:
    """Classify and print every PI; no database access. Returns the exit status."""
    totals = {"would_exclude": 0, "kept": 0, "no_data": 0}
    failed: list[str] = []
    stopped: list[str] = []
    reached = 0
    for index, pi in enumerate(pis):
        reason = await _budget_low()
        if reason is not None:
            stopped = [p.orcid for p in pis[index:]]
            print(f"\nSTOPPED before {pi.name} ({pi.orcid}): {reason}")
            break
        reached += 1
        print(f"\n=== {pi.name} ({pi.orcid}) ===")
        try:
            organizations = (await fetch_orcid_profile(pi.orcid)).get("organizations") or []
        except Exception as exc:
            print(f"  ORCID profile unavailable: {type(exc).__name__}: {exc}")
            failed.append(pi.orcid)
            continue
        try:
            with bulk_requests():
                authorships = await orcid_authorships_by_doi(pi.orcid)
        except Exception as exc:  # JobDeferred included: the meter ran out mid-fetch
            print(f"  OpenAlex authorships unavailable: {type(exc).__name__}: {exc}")
            failed.append(pi.orcid)
            continue
        would_exclude, kept, no_data = classify_openalex_rows(pi.rows, authorships, organizations)
        _print_pi(would_exclude, kept, no_data, authorships, organizations)
        totals["would_exclude"] += len(would_exclude)
        totals["kept"] += len(kept)
        totals["no_data"] += len(no_data)
    print("\n=== Totals ===")
    print(f"  PIs reached: {reached} of {len(pis)}")
    for key, count in totals.items():
        print(f"  {key}: {count}")
    print(f"  failed ({len(failed)}): {', '.join(failed) or 'none'}")
    if stopped:
        print(f"  not reached ({len(stopped)}); resume after the reset with:")
        print("    " + " ".join(f"--orcid {o}" for o in stopped))
    print("  [read-only] nothing written")
    return 1 if failed or stopped else 0


async def run(db: AsyncSession, *, orcids: Sequence[str]) -> int:
    return await report(await load_population(db, orcids))


async def _main(orcids: Sequence[str]) -> int:
    async with get_session_factory()() as db:
        pis = await load_population(db, orcids)
    return await report(pis)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--orcid", action="append", default=[],
                        help="scope to this ORCID (repeatable)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.orcid)))


if __name__ == "__main__":
    main()
