"""Read-only per-PI corpus classification (plan Task 8/Task 2,
docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md §3, §5).

Re-runnable measurement instrument, alongside ``scripts/audit_pub_dois.py``.
For every PI in scope: refetch each stored PMID and classify it against the
CURRENT ``match_pi_author`` (strong match / bare-initial-only /
no-surname-author), count ``EXCLUDED_TYPES`` hits (split pure vs. secondary,
D4), count duplicate-title groups, and report the ``resolve_corpus`` delta —
the over-match alarm for any matcher change (stored, resolved, overlap,
resolved-not-stored, stored-not-resolved, ``dropped["identity"]``).

Never writes anything — no DB write, no file write, no re-export.

Output: one JSON object per PI, stable key order, one per line, to stdout —
easy to diff between runs. A human-readable summary goes to stderr so stdout
stays clean JSONL.

Usage:
    python scripts/audit_pi_corpus.py                       # every PI
    python scripts/audit_pi_corpus.py --orcid 0000-...       # scoped (repeatable)
    python scripts/audit_pi_corpus.py --skip-resolve         # classification only,
                                                              # no resolve_corpus delta
                                                              # (no ORCID/OpenAlex/S4 calls)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from scripts.repair_pi_corpus import (  # noqa: E402
    classify_excluded_type,
    load_stored_publications,
    matched_used_bare_initial,
    normalize_title,
    refetch_pmids,
)
from src.database import get_session_factory  # noqa: E402
from src.models import User  # noqa: E402
from src.services.corpus import CorpusStageError, match_pi_author, resolve_corpus  # noqa: E402


async def _classify_pi(db, user: User, *, skip_resolve: bool = False) -> dict:
    stored = await load_stored_publications(db, user.id)
    pmids = sorted({p.pmid for p in stored if p.pmid})
    refetched = await refetch_pmids(pmids)

    identity_counts: Counter[str] = Counter()
    excluded_type_counts: Counter[str] = Counter()
    title_groups: dict[str, int] = {}
    no_pmid = unrefetchable = 0

    for pub in stored:
        if pub.pmid is None:
            no_pmid += 1
            continue
        record = refetched.get(pub.pmid)
        if record is None:
            unrefetchable += 1
            continue

        kind, _ = match_pi_author(record, user.name)
        if kind in ("no_match", "consortium"):
            identity_counts["no_surname_author"] += 1
        elif matched_used_bare_initial(record, user.name):
            identity_counts["bare_initial_only"] += 1
        else:
            identity_counts["strong"] += 1

        type_hit = classify_excluded_type(record.get("pub_types") or [])
        if type_hit:
            excluded_type_counts[type_hit] += 1

        title = record.get("title") or pub.title
        key = normalize_title(title) or f"pmid:{pub.pmid}"
        title_groups[key] = title_groups.get(key, 0) + 1

    duplicate_title_groups = sum(1 for n in title_groups.values() if n >= 2)

    resolve_delta: dict | None = None
    resolve_error: str | None = None
    if skip_resolve:
        resolve_error = "skipped (--skip-resolve)"
    else:
        try:
            resolved = await resolve_corpus(user.orcid, user.name, user.institution)
        except CorpusStageError as exc:
            resolve_error = str(exc)
        else:
            stored_pmids = {p.pmid for p in stored if p.pmid}
            resolved_pmids = {str(r.get("pmid")) for r in resolved.kept if r.get("pmid")}
            resolve_delta = {
                "stored": len(stored_pmids),
                "resolved": len(resolved_pmids),
                "overlap": len(stored_pmids & resolved_pmids),
                "resolved_not_stored": len(resolved_pmids - stored_pmids),
                "stored_not_resolved": len(stored_pmids - resolved_pmids),
                "dropped_identity": resolved.dropped.get("identity", 0),
            }

    return {
        "orcid": user.orcid,
        "name": user.name,
        "stored_count": len(stored),
        "classification": {
            "strong": identity_counts["strong"],
            "bare_initial_only": identity_counts["bare_initial_only"],
            "no_surname_author": identity_counts["no_surname_author"],
            "no_pmid": no_pmid,
            "unrefetchable": unrefetchable,
        },
        "excluded_type_hits": {
            "excluded_type": excluded_type_counts["excluded_type"],
            "secondary_excluded_type": excluded_type_counts["secondary_excluded_type"],
        },
        "duplicate_title_groups": duplicate_title_groups,
        "resolve_delta": resolve_delta,
        "resolve_error": resolve_error,
    }


async def _run(orcids: list[str], skip_resolve: bool) -> int:
    async with get_session_factory()() as db:
        q = select(User).where(User.user_role == "pi").order_by(User.orcid)
        if orcids:
            q = q.where(User.orcid.in_(orcids))
        users = (await db.execute(q)).scalars().all()

        totals: Counter[str] = Counter()
        for user in users:
            report = await _classify_pi(db, user, skip_resolve=skip_resolve)
            print(json.dumps(report, sort_keys=True))

            totals["pis"] += 1
            totals["stored"] += report["stored_count"]
            totals["strong"] += report["classification"]["strong"]
            totals["bare_initial_only"] += report["classification"]["bare_initial_only"]
            totals["no_surname_author"] += report["classification"]["no_surname_author"]
            totals["excluded_type"] += report["excluded_type_hits"]["excluded_type"]
            totals["secondary_excluded_type"] += report["excluded_type_hits"][
                "secondary_excluded_type"
            ]
            totals["duplicate_title_groups"] += report["duplicate_title_groups"]

        print(f"\n# {totals['pis']} PI(s), {totals['stored']} stored rows", file=sys.stderr)
        print(
            f"# classification: strong={totals['strong']} "
            f"bare_initial_only={totals['bare_initial_only']} "
            f"no_surname_author={totals['no_surname_author']}",
            file=sys.stderr,
        )
        print(
            f"# excluded types: pure={totals['excluded_type']} "
            f"secondary={totals['secondary_excluded_type']}",
            file=sys.stderr,
        )
        print(
            f"# duplicate-title groups: {totals['duplicate_title_groups']}",
            file=sys.stderr,
        )
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--orcid", action="append", default=[], help="Scope to this ORCID (repeatable)")
    parser.add_argument(
        "--skip-resolve",
        action="store_true",
        help="Classification only; skip the resolve_corpus delta (no ORCID/OpenAlex/S4 calls)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.orcid, args.skip_resolve)))


if __name__ == "__main__":
    main()
