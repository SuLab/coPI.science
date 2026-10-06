"""Read-only checks of the PI-profile remediation's Phase 2 industry repair (spec
2026-10-05 §9, Phase 2 row). Run after the `industry_evidence` re-run has drained:

  $DC run --rm --no-deps -T blackbird-app python scripts/verify_industry_remediation.py --deploy-ts 2026-10-07T14:00:00Z
  ... --orcid 0000-0002-2214-0114     # one PI only (Phase 2's canary)

Phase 2 runs it for its canary; the full run follows Phase 3's regeneration (the
remediation's 2026-10-06 rollout decision). Prints PASS or FAIL per check, with one line per
problem, and exits 0 only when all pass:

1. completeness: every PI (user_role 'pi' plus pi_lab owners) has a SCORER_VERSION row
   computed at or after --deploy-ts that is not a veto's not-refreshed row;
2. attribution: every COI row the scorer can count names the PI in its `pi_mention`, or
   says "all/each author(s)";
3. suffixes: no COI company name is a truncated word of its sentence ("Johns Hopkins Co"
   out of "Consortium");
4. coverage: every PI's latest current-version row records its coverage;
5. ranks: every read-time percentile equals 100 x cume_dist over the cohort, within the
   0.05 a one-decimal rounding allows (Python rounds half to even, Postgres half up).

Writes nothing: SELECTs only, and the session is rolled back."""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from scripts import _bulk_enqueue as bulk  # noqa: E402
from src.database import get_session_factory  # noqa: E402
from src.models import PiIndustryEvidence, PiIndustryScore  # noqa: E402
from src.services.coi_attribution import ALL_AUTHORS, norm  # noqa: E402
from src.services.directory import industry_views  # noqa: E402
from src.services.industry_evidence import NOT_REFRESHED  # noqa: E402
from src.services.industry_score import SCORER_VERSION  # noqa: E402
from src.services.person_names import name_key, parse_person_name, strip_accents  # noqa: E402

TOLERANCE = 0.051
#: The read-time cohort (each user's latest `:v` row with a tenure start and raw_sum > 0)
#: and each member's 100 x cume_dist, unrounded.
CUME_DIST_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (user_id) user_id, raw_sum, tenure_start_used
    FROM pi_industry_scores WHERE scorer_version = :v
   ORDER BY user_id, computed_at DESC, id DESC),
cohort AS (SELECT user_id, raw_sum FROM latest WHERE tenure_start_used IS NOT NULL AND raw_sum > 0)
SELECT user_id, 100 * cume_dist() OVER (ORDER BY raw_sum) FROM cohort
"""
CHECKS = ("completeness", "attribution", "suffixes", "coverage", "ranks")


def mention_names_pi(mention: str, pi_full_name: str) -> bool:
    """Whether a COI row's `pi_mention` names this PI, judged from the PI's name alone (the
    extractor's own rule needs the record's author list): "all/each author(s)"; a word
    that is one of the PI's surname keys; or an initials form (capitals and periods only)
    starting with the PI's first initial and ending with an initial of a surname key."""
    mention = mention.strip()
    if not mention:
        return False
    if ALL_AUTHORS.fullmatch(mention):
        return True
    parsed = parse_person_name(pi_full_name)
    words = {name_key(w) for w in re.split(r"[\s.]+", mention) if w}
    if words & parsed.surname_keys or name_key(mention) in parsed.surname_keys:
        return True
    if re.fullmatch(r"[A-Z][A-Z.\s]*", strip_accents(mention)):
        letters = re.sub(r"[^A-Z]", "", strip_accents(mention))
        first = name_key(parsed.first)[:1].upper()
        last = {key[:1].upper() for key in parsed.surname_keys}
        return len(letters) >= 2 and letters[0] == first and letters[-1] in last
    return False


def truncated_suffix(company: str, span: str) -> bool:
    """True when `company` occurs in its normalised sentence only as the start of a longer
    word ("Johns Hopkins Co" in "Johns Hopkins Consortium"). A name the 400-character span
    cut off is not judged."""
    sentence = norm(span or "")
    whole = re.search(rf"(?<!\w){re.escape(company)}(?!\w)", sentence, re.IGNORECASE)
    return whole is None and company.lower() in sentence.lower()


async def _population(db: AsyncSession, orcid: str | None) -> dict:
    """{user_id: name} of the §3 PI population, or of the PI with `orcid` alone."""
    return {uid: name for uid, pi_orcid, name in await bulk.pi_population(db)
            if orcid is None or pi_orcid == orcid}


def _refreshed(row: PiIndustryScore) -> bool:
    """Not a veto rescore's not-refreshed row (industry_evidence.NOT_REFRESHED)."""
    return NOT_REFRESHED not in (row.coverage or {}).values()


async def _latest(db: AsyncSession) -> dict:
    """{user_id: latest SCORER_VERSION row}."""
    rows = (await db.execute(
        select(PiIndustryScore).where(PiIndustryScore.scorer_version == SCORER_VERSION)
        .distinct(PiIndustryScore.user_id)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc(),
                  PiIndustryScore.id.desc())
    )).scalars().all()
    return {row.user_id: row for row in rows}


async def _coi_rows(db: AsyncSession, user_ids) -> list[PiIndustryEvidence]:
    """The un-vetoed `coi_relationship` rows of `user_ids`."""
    return list((await db.execute(
        select(PiIndustryEvidence).where(
            PiIndustryEvidence.kind == "coi_relationship", PiIndustryEvidence.vetoed_at.is_(None),
            PiIndustryEvidence.user_id.in_(list(user_ids)))
    )).scalars())


async def run_checks(
    db: AsyncSession, deploy_ts: datetime, *, orcid: str | None = None,
) -> dict[str, list[str]]:
    """Each check's problems ([] = PASS), keyed by CHECKS, over every PI or, with `orcid`,
    that PI alone. Every problem line starts with the PI's name. Only reads."""
    names = await _population(db, orcid)
    latest = await _latest(db)
    problems: dict[str, list[str]] = {k: [] for k in CHECKS}

    fresh = {row.user_id for row in (await db.execute(
        select(PiIndustryScore).where(
            PiIndustryScore.scorer_version == SCORER_VERSION,
            PiIndustryScore.computed_at >= deploy_ts)
    )).scalars() if _refreshed(row)}
    problems["completeness"] = sorted(
        f"{name}: no {SCORER_VERSION} row since {deploy_ts.isoformat()}"
        for uid, name in names.items() if uid not in fresh)

    for row in await _coi_rows(db, names):
        ev = row.evidence or {}
        name = names[row.user_id]
        if ev.get("attributed") is True and not mention_names_pi(
                str(ev.get("pi_mention") or ""), name):
            problems["attribution"].append(
                f"{name}: {row.company_name!r} credited via {ev.get('pi_mention')!r}")
        if row.source == "pubmed" and truncated_suffix(row.company_name or "",
                                                       str(ev.get("span") or "")):
            problems["suffixes"].append(f"{name}: {row.company_name!r} is a truncated word")

    problems["coverage"] = sorted(
        f"{names[uid]}: latest {SCORER_VERSION} row has no coverage"
        for uid, row in latest.items() if uid in names and row.coverage is None)

    views = await industry_views(db, list(names))
    expected = {uid: float(pct) for uid, pct in
                (await db.execute(text(CUME_DIST_SQL), {"v": SCORER_VERSION})).all()}
    for uid, name in names.items():
        view = views[uid]
        if uid in expected and view.reason not in ("ok", "cohort_too_small"):
            problems["ranks"].append(f"{name}: in the cohort but {view.reason}")
        if view.reason == "ok" and (
                uid not in expected or abs(view.percentile - expected[uid]) > TOLERANCE):
            problems["ranks"].append(
                f"{name}: percentile {view.percentile} != cume_dist {expected.get(uid)}")
    return problems


async def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deploy-ts", required=True, type=datetime.fromisoformat,
                        help="ISO 8601; Phase 2's deploy time (a naive time is UTC)")
    parser.add_argument("--orcid", help="check this PI only (the canary)")
    args = parser.parse_args(argv)
    deploy_ts = args.deploy_ts if args.deploy_ts.tzinfo else args.deploy_ts.replace(tzinfo=UTC)
    async with get_session_factory()() as db:
        try:
            problems = await run_checks(db, deploy_ts, orcid=args.orcid)
        finally:
            await db.rollback()
    for check, found in problems.items():
        print(f"{'PASS' if not found else 'FAIL'} {check}" + (f" ({len(found)})" if found else ""))
        for line in found:
            print(f"  {line}")
    return 0 if not any(problems.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
