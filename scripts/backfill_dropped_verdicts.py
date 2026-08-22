"""One-time backfill: recover verdicts that the old capture gate discarded.

Run 8b64a0e0 (2026-08-22) refused two complete `<assessment_json>` sidecars as
``premature_sidecar`` because they arrived on a DECIDE turn (ordinal 10) rather
than the CONCLUDE turn (ordinal 12) the gate demanded — and the run's 180-minute
timer expired before the "later turn still owed the verdict" could arrive. One of
them, markham, recomputes to **3.04**, the highest weighted score of that run,
and is the only ``route-to-incubation`` recommendation it produced.

The gate no longer refuses these (see ``_sidecar_refusal``), so this script is
for the rows already lost. It reads the raw sidecar back out of
``llm_call_logs.response_text`` — which happens to retain the full model
response — parses it with the same extractor the engine uses, and writes a
normal ``opportunity_assessments`` row.

Two things it does deliberately:

* **Stamps the rubric version from the RUN, not from whatever
  ``blackbird_rubric`` imports at backfill time.** A row stamped with today's
  rubric but scored under the run's would silently break every cross-run
  comparison the stamp exists to protect.
* **Does not fire the ``#assessments-summary`` headline.** Those posts are
  announcements of a live verdict; replaying them weeks later would be noise,
  and design D14's promise is about what a drop does NOT post, not about
  retroactive completeness.

Idempotent: skips any (run, thread) that already has an assessment row.

Usage (inside the app container, AFTER migration 0035 is applied):

    docker compose -f docker-compose.prod.yml exec blackbird-app \
        python scripts/backfill_dropped_verdicts.py --run 8b64a0e0-1fa7-40c4-b9a2-f57a4e058fb0
    # preview only:
    ... python scripts/backfill_dropped_verdicts.py --run <uuid> --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import uuid
from pathlib import Path

# Prefer the mounted project root over any baked-in copy of `src` in
# site-packages (the image installs src/ non-editable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import AssessmentDrop, LlmCallLog, OpportunityAssessment
from src.services.blackbird_rubric import weighted_score

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_dropped_verdicts")

_SIDECAR_RE = re.compile(
    r"<assessment_json>\s*(.*?)\s*</assessment_json>", re.DOTALL
)

# The reasons that mean "a complete verdict existed and was thrown away".
# `missing_sidecar` and `empty_reply` are NOT recoverable — nothing was emitted.
_RECOVERABLE = ("premature_sidecar", "duplicate_thread_verdict", "unparseable_sidecar")


def _band(score: float, *, advance_min: float, conditional_min: float) -> str:
    if score >= advance_min:
        return "advance"
    if score >= conditional_min:
        return "conditional"
    return "pass"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="simulation_run_id to backfill")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--advance-min", type=float, default=3.4,
        help="incubation band line, from the run's rubric",
    )
    ap.add_argument("--conditional-min", type=float, default=2.7)
    # Stamped from the RUN, not from whatever blackbird_rubric imports now —
    # see the module docstring. Defaults are run 8b64a0e0's stamps.
    ap.add_argument("--rubric-version", default="2.0.0")
    ap.add_argument("--rubric-hash", default="e3ef75f84c48")
    args = ap.parse_args()
    run_id = uuid.UUID(args.run)

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    written = skipped = unrecoverable = 0
    async with factory() as db:
        drops = (await db.execute(
            select(AssessmentDrop).where(
                AssessmentDrop.simulation_run_id == run_id,
                AssessmentDrop.reason.in_(_RECOVERABLE),
            ).order_by(AssessmentDrop.created_at)
        )).scalars().all()
        logger.info("found %d recoverable drop(s) for run %s", len(drops), run_id)

        for drop in drops:
            existing = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id,
                    OpportunityAssessment.subject_agent_id == drop.subject_agent_id,
                )
            )).scalars().first()
            if existing is not None:
                logger.info("skip %s — already has an assessment", drop.subject_agent_id)
                skipped += 1
                continue

            # Prefer the sidecar stored on the drop row itself (migration 0035);
            # fall back to scraping llm_call_logs for rows dropped before that
            # column existed, which is the whole reason this script exists.
            verdict = drop.raw_verdict
            source = "drop.raw_verdict"
            if not verdict:
                rows = (await db.execute(
                    select(LlmCallLog).where(
                        LlmCallLog.simulation_run_id == run_id,
                        LlmCallLog.agent_id == drop.agent_id,
                        LlmCallLog.response_text.like("%<assessment_json>%"),
                    ).order_by(LlmCallLog.created_at)
                )).scalars().all()
                # The drop's own turn is the last logged sidecar at or before it.
                candidate = None
                for r in rows:
                    if r.created_at <= drop.created_at:
                        candidate = r
                if candidate is None:
                    logger.warning(
                        "no sidecar found in llm_call_logs for %s", drop.subject_agent_id
                    )
                    unrecoverable += 1
                    continue
                m = _SIDECAR_RE.search(candidate.response_text or "")
                if not m:
                    unrecoverable += 1
                    continue
                try:
                    verdict = json.loads(m.group(1))
                except ValueError as exc:
                    logger.warning("unparseable sidecar for %s: %s",
                                   drop.subject_agent_id, exc)
                    unrecoverable += 1
                    continue
                source = f"llm_call_logs {candidate.id}"

            if not isinstance(verdict, dict):
                unrecoverable += 1
                continue

            scores = verdict.get("scores") or {}
            # Same rule as _persist_assessment: an empty scores map is "we don't
            # know", and weighted_score({}) is a 0.00 that bands as a decline
            # nobody made — so leave both columns NULL rather than invent one.
            ws = weighted_score(scores) if scores else None
            band = (
                _band(ws, advance_min=args.advance_min,
                      conditional_min=args.conditional_min)
                if ws is not None else None
            )

            row = OpportunityAssessment(
                id=uuid.uuid4(),
                simulation_run_id=run_id,
                agent_id=drop.agent_id,
                subject_agent_id=drop.subject_agent_id,
                channel_name=verdict.get("channel_name") or "unknown",
                slack_ts=None,
                company_or_project=verdict.get("company_or_project"),
                funnel_stage=verdict.get("funnel_stage"),
                recommendation=verdict.get("recommendation"),
                confidence=verdict.get("confidence"),
                weighted_score=ws,
                band=band,
                gating=verdict.get("gating"),
                scores=scores or None,
                red_flags=verdict.get("red_flags"),
                derisking_milestones=verdict.get("derisking_milestones"),
                rationale=verdict.get("rationale"),
                raw_verdict=verdict,
                rubric_version=args.rubric_version,
                rubric_content_hash=args.rubric_hash,
            )
            logger.info(
                "%s %s -> %s (score %s, band %s) from %s",
                "WOULD WRITE" if args.dry_run else "WRITE",
                drop.subject_agent_id, row.recommendation,
                f"{ws:.2f}" if ws is not None else "n/a", band, source,
            )
            if not args.dry_run:
                db.add(row)
                written += 1

        if not args.dry_run and written:
            await db.commit()

    logger.info(
        "done: %d written, %d skipped (already present), %d unrecoverable",
        written, skipped, unrecoverable,
    )
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
