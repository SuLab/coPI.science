"""Repair publication title/abstract text truncated by the pre-`itertext()` parser.

Until #22 COR-16 (`2c1d504`) the PubMed parser read ElementTree's `.text`, which
stops at the first child element, so
`<ArticleTitle>Role of <i>TP53</i> in cancer.</ArticleTitle>` was stored as
`"Role of "`. `<AbstractText>` had the identical defect, and an `<AbstractText>`
whose first node is markup was stored as `""`. Those stored rows -- not the live
parser -- are what `profile_export.py` and the synthesis context read.

`35cc010` made the pipeline's existing-row branch refresh
title/abstract/journal/year, so a per-user pipeline re-run
(`python -m src.cli seed-profile --orcid <ORCID>`) repairs any PMID that is still
listed on that PI's ORCID record. It cannot repair a row whose PMID ORCID does not
list -- notably every row `scripts/backfill_publications.py` inserted, which exists
precisely because those PIs' ORCID works lists are empty. This script repairs those
rows directly: it re-fetches the affected PMIDs through the fixed parser and
rewrites the four text columns.

Usage (inside the app container, dry run first):

    docker compose exec app python scripts/repair_publication_text.py --all
    docker compose exec app python scripts/repair_publication_text.py --all --apply

Without `--all` it considers only the rows matching SELECTION_PREDICATE_SQL below,
which is cheap but under-detects; `--all` sweeps the whole table.

Safe to re-run. A repaired row is byte-identical to the fresh record, so a second
run reports it `no-change`; a row whose PubMed record genuinely carries no abstract
also reports `no-change`, because the write guard below never blanks or shortens a
value that is already there.

Deliberately does NOT touch `doi`/`pmcid`: the DOI has its own reconciliation gate
(`reconcile_pub_doi`, the fix for the bad-links incident) and re-deciding it is not
this repair's job.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The selection predicate, in one place so the runbook can quote it verbatim.
#   * `title ~ '[[:space:]]$'` is the truncation signature: ElementTree hands back
#     everything before the first child element, so a title cut at an inline tag
#     ends in the space that preceded it. No PubMed title legitimately does.
#   * `char_length(btrim(title)) < 2` catches the cut-at-the-first-character cases
#     ("T", "H", "") that leave nothing to end in a space.
#   * the abstract arm is deliberately generous. An abstract under 80 characters is
#     either truncated or absent; re-fetching a genuinely abstract-less record costs
#     one slot in a 100-PMID efetch batch and reports `no-change`.
SELECTION_PREDICATE_SQL = (
    "title ~ '[[:space:]]$' "
    "OR char_length(btrim(title)) < 2 "
    "OR char_length(btrim(coalesce(abstract, ''))) < 80"
)

# Columns the repair may rewrite, and the fetched-record key each reads.
REPAIRABLE_COLUMNS = ("title", "abstract", "journal", "year")

# (pmid, action, detail, orcid) -- action is one of:
#   would-repair / repair / no-change / error-no-record
# The owning PI's ORCID rides along because the follow-up half of the deploy step is
# "re-run the pipeline for the PIs whose evidence just changed", and deriving that
# list from the corruption predicate afterwards would under-select (see
# docs/plans/2026-09-04-decisions/task-20.md).
ReportEntry = tuple[str, str, str, str]


async def repair(
    db: Any,
    fetch: Any = None,
    apply: bool = False,
    limit: int | None = None,
    all_rows: bool = False,
) -> list[ReportEntry]:
    """Re-fetch every corrupted publication row and rewrite its text columns.

    Dry run (the default) reports what WOULD change and writes nothing.

    `all_rows=True` ignores SELECTION_PREDICATE_SQL and considers every row with a
    PMID. That predicate is a signature, not an oracle: a title cut at markup that
    is neither empty nor followed by a space ("Inhibition of STEP" for "Inhibition
    of STEP61 …") does not match it, and neither does an abstract truncated past 80
    characters. Measured on the production copy, the predicate selects 512 rows of
    which 248 are genuinely wrong, while a full sweep of all 4,508 finds 896.
    """
    from sqlalchemy import select, text

    from src.models import Publication, User
    from src.services.pubmed import fetch_pubmed_records

    if fetch is None:
        fetch = fetch_pubmed_records

    stmt = (
        select(Publication, User.orcid)
        .join(User, User.id == Publication.user_id)
        .where(
            Publication.pmid.isnot(None) if all_rows else text(SELECTION_PREDICATE_SQL)
        )
        .order_by(Publication.pmid, Publication.user_id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = [(pub, orcid or "") for pub, orcid in (await db.execute(stmt)).all()]

    # One efetch slot per distinct PMID, not per row: the same paper is normally
    # stored once per co-author on the roster.
    wanted = list(dict.fromkeys(p.pmid for p, _ in rows if p.pmid))
    records: dict[str, dict[str, Any]] = {}
    if wanted:
        records = {r["pmid"]: r for r in await fetch(wanted) if r.get("pmid")}

    report: list[ReportEntry] = []
    for pub, orcid in rows:
        rec = records.get(pub.pmid) if pub.pmid else None
        if rec is None:
            report.append((pub.pmid or "", "error-no-record", "", orcid))
            continue

        changed: list[str] = []
        for column in REPAIRABLE_COLUMNS:
            fresh = rec.get(column)
            # Never blank or overwrite with nothing: a PubMed hiccup, or a record
            # that genuinely lacks an abstract, must not destroy what is on the row
            # (the same guard the pipeline's update branch uses, 35cc010).
            if not fresh:
                continue
            if getattr(pub, column) == fresh:
                continue
            changed.append(column)
            if apply:
                setattr(pub, column, fresh)

        if not changed:
            report.append((pub.pmid or "", "no-change", "", orcid))
        else:
            report.append(
                (
                    pub.pmid or "",
                    "repair" if apply else "would-repair",
                    ",".join(changed),
                    orcid,
                )
            )

    if apply:
        await db.flush()
    return report


async def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="Write the repairs (default: dry run)"
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="Only consider the first N rows"
    )
    ap.add_argument(
        "--all",
        dest="all_rows",
        action="store_true",
        help=(
            "Sweep every row with a PMID instead of only those matching the "
            "corruption signature (finds ~3.6x more; costs one efetch per 100 rows)"
        ),
    )
    args = ap.parse_args()

    from src.database import get_session_factory

    factory = get_session_factory()
    async with factory() as db:
        report = await repair(
            db, apply=args.apply, limit=args.limit, all_rows=args.all_rows
        )
        if args.apply:
            await db.commit()

    for pmid, action, detail, orcid in report:
        print(f"{orcid} {pmid}: {action} {detail}".rstrip())

    repaired = sum(1 for _, a, _, _ in report if a == "repair")
    planned = sum(1 for _, a, _, _ in report if a == "would-repair")
    unchanged = sum(1 for _, a, _, _ in report if a == "no-change")
    errors = sum(1 for _, a, _, _ in report if a.startswith("error"))
    touched = sorted(
        {o for _, a, _, o in report if o and a in ("repair", "would-repair")}
    )
    scope = "considered" if args.all_rows else "matched the selection predicate"
    print(
        f"\n{len(report)} row(s) {scope}; "
        f"{unchanged} already correct, {errors} not found on PubMed."
    )
    if args.apply:
        print(f"Repaired {repaired} row(s).")
    else:
        print(f"Dry run: {planned} row(s) would be repaired. Re-run with --apply.")
    if touched:
        print(
            f"\n{len(touched)} PI(s) own a repaired row. Their stored profile prose was "
            "synthesized from the old text; re-run the pipeline for each if you want it "
            "rebuilt (`python -m src.cli seed-profile --orcid <ORCID>`, one job each):"
        )
        for orcid in touched:
            print(f"  {orcid}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
