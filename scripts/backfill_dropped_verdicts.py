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

Idempotent: skips any ``(run, thread_id)`` that already has an assessment row;
for a row with no ``thread_id`` recorded — every row this script wrote before
this fix, since it never populated the column, plus any drop whose own
``thread_id`` could not be identified — falls back to ``(run,
subject_agent_id)``, matched only against OTHER thread_id-less rows so a PI's
second interview is never mistaken for a re-run of its first. See
``_existing_assessment_for``.

(This docstring claimed "(run, thread)" from the day this script was written,
while the code beneath it keyed on ``subject_agent_id`` alone with no
``thread_id`` column to key on at all — a docstring/code mismatch, not a
behaviour change here: this fix is what finally makes the two agree, now that
migration 0036 gives ``opportunity_assessments`` a ``thread_id`` to key on.)

Usage (inside the app container, AFTER migration 0036 is applied — that is
what added ``opportunity_assessments.panel_owed`` and ``.thread_id``, both
written below):

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
from collections.abc import Iterable
from pathlib import Path

# Prefer the mounted project root over any baked-in copy of `src` in
# site-packages (the image installs src/ non-editable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.simulation import _bounded_str, _normalize_gating, _str_or_none
from src.config import get_settings
from src.models import AssessmentDrop, LlmCallLog, OpportunityAssessment
from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_dropped_verdicts")

_SIDECAR_RE = re.compile(
    r"<assessment_json>\s*(.*?)\s*</assessment_json>", re.DOTALL
)

# The reasons that mean "a complete verdict existed and was thrown away".
# `missing_sidecar` and `empty_reply` are NOT recoverable — nothing was emitted.
_RECOVERABLE = ("premature_sidecar", "duplicate_thread_verdict", "unparseable_sidecar")


def _score_and_band(verdict: dict) -> tuple[float | None, str | None]:
    """Exactly what ``_persist_assessment`` would have computed for this verdict.

    STAGE-AWARE, and that is the whole point of routing through the rubric module
    rather than doing the arithmetic here. Since v2.0.0 an incubation-stage
    verdict is scored on the incubation weights and banded on the incubation
    lines; everything else uses the investment scale. Both of the verdicts this
    script exists to recover are incubation-stage, and scoring them on the
    investment scale understates them — markham comes out at 2.84/conditional
    instead of 3.04/conditional, which is the wrong number to put in a corpus
    other rows will be compared against.
    """
    scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
    if not scores:
        return None, None
    stage = verdict.get("funnel_stage")
    score = rubric_weighted_score(scores, stage)
    return score, rubric_band(score, stage)


def _existing_assessment_for(
    candidates: Iterable[OpportunityAssessment],
    run_id: uuid.UUID,
    drop: AssessmentDrop,
) -> OpportunityAssessment | None:
    """The idempotency key is THE INTERVIEW, not the PI (F1.3).

    Keying on ``(run, subject_agent_id)`` alone — what this script did before
    this fix — makes a PI's SECOND interview look like a re-run of the first:
    production has five duplicate ``(run, subject)`` pairs, each one a second
    interview whose recoverable verdict this script would silently skip.

    So the primary key is ``(run, thread_id)``. But every row this script has
    ever written has ``thread_id IS NULL`` — the pre-fix code never populated
    the column at all — so a naive thread-only check would fail to find
    markham's and weeraratna's existing rows and re-write them on a second
    run. The documented fallback covers exactly that: when an existing row's
    OWN ``thread_id`` is NULL, match it on ``subject_agent_id`` instead, since
    a NULL-thread_id row predates this fix and cannot be told apart from a
    genuinely different interview any other way.

    The fallback is deliberately keyed off the CANDIDATE row's thread_id, not
    the drop's: if the drop has a known thread_id but no candidate shares it,
    we must NOT then fall back to matching any old NULL-thread_id row for the
    same subject — that would resurrect the exact bug this function exists to
    fix, just from the other direction (a second interview's drop wrongly
    matching the first interview's pre-fix row).
    """
    for row in candidates:
        if row.simulation_run_id != run_id:
            continue
        if drop.thread_id is not None and row.thread_id == drop.thread_id:
            return row
        if row.thread_id is None and row.subject_agent_id == drop.subject_agent_id:
            return row
    return None


def _fallback_llm_log_query(run_id: uuid.UUID, drop: AssessmentDrop):
    """The narrowed ``llm_call_logs`` scrape query (F1.4).

    Selects only ``id``/``created_at``/``response_text`` — the whole-row
    ``select(LlmCallLog)`` this replaces also pulled ``system_prompt`` and
    ``messages_json``, hundreds of ~30 KB prompts, for three columns' worth of
    actual use.

    Ordered most-recent-first (at or before the drop) so
    ``_recover_from_llm_logs`` can take the first sidecar whose OWN embedded
    ``subject_agent_id`` matches the drop's, rather than assuming the
    chronologically nearest log row belongs to this interview at all — the
    hub interviews many PIs at once under one ``agent_id``, so "nearest in
    time" and "same interview" are not the same claim.
    """
    return (
        select(LlmCallLog.id, LlmCallLog.created_at, LlmCallLog.response_text)
        .where(
            LlmCallLog.simulation_run_id == run_id,
            LlmCallLog.agent_id == drop.agent_id,
            LlmCallLog.response_text.like("%<assessment_json>%"),
            LlmCallLog.created_at <= drop.created_at,
        )
        .order_by(LlmCallLog.created_at.desc())
    )


def _recover_from_llm_logs(
    rows: Iterable[tuple], drop: AssessmentDrop
) -> tuple[dict | None, str | None]:
    """Pick the ``llm_call_logs`` row that is actually THIS interview's sidecar.

    ``rows`` is ``(id, created_at, response_text)`` tuples, most recent first,
    already filtered to the drop's ``agent_id`` and to at-or-before its
    ``created_at`` (see ``_fallback_llm_log_query``). The old code took
    whichever of these was simply last in time — "it happened to work on the
    two real rows (log rows 249 ms and 232 ms before their drops) but nothing
    enforces it" (task brief). A row that parses but whose own
    ``subject_agent_id`` names a different PI is refused outright rather than
    returned: recovering the wrong PI's verdict under this drop's identity
    would be worse than leaving it unrecoverable.
    """
    for log_id, _created_at, response_text in rows:
        match = _SIDECAR_RE.search(response_text or "")
        if not match:
            continue
        try:
            candidate = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        if candidate.get("subject_agent_id") != drop.subject_agent_id:
            continue
        return candidate, f"llm_call_logs {log_id}"
    return None, None


def _build_assessment_row(
    verdict: dict,
    drop: AssessmentDrop,
    rubric_version: str,
    rubric_hash: str,
) -> OpportunityAssessment:
    """Build the row ``_persist_assessment`` would have written for this verdict.

    Reuses three of its guards (F1.3) rather than writing new ones, because
    this script commits ONCE at the end of the whole batch: unlike the live
    engine's one-row-per-commit writes, a single bad value here would take
    every recovered row down with it, not just its own.

      * ``_bounded_str`` clips the four bounded VARCHAR columns
        (``funnel_stage``, ``recommendation``, ``confidence``,
        ``channel_name``) instead of letting an over-long LLM-sourced value
        raise ``StringDataRightTruncation`` at commit.
      * ``_normalize_gating`` enforces the tri-state
        ``met``/``not_met``/``unconfirmed`` contract, dropping (not
        coercing) a legacy boolean or otherwise malformed value per key.
      * ``_str_or_none`` guards the Text columns ``company_or_project`` and
        ``rationale`` against a model that emitted a dict/list instead of
        prose, which would otherwise raise at commit exactly like an
        over-long VARCHAR does.

    ``subject_agent_id`` and ``thread_id`` come from the DROP, never the
    verdict — the same authority rule ``_persist_assessment`` applies via its
    own ``subject_agent_id_fallback`` (the model is never shown its
    interview partner's real ``agent_id``). Writing ``thread_id`` here is
    also what lets ``_existing_assessment_for`` key a FUTURE re-run of this
    script on the interview instead of the PI (F1.3).

    F1.1: reads ``suggested_derisking_milestones`` — the sidecar contract key
    (``prompts/roles/scout_hub/phase4-thread-reply.md``) that
    ``src/agent/simulation.py`` reads — not the ``derisking_milestones`` key
    the pre-fix script read, which is a column name, not a verdict field, and
    is never present on any real sidecar.

    F1.2: never asserts a verified panel for a row the specialist floor never
    evaluated. ``missing_domains=[]`` is the documented UNVERIFIED state (see
    ``src/models/opportunity.py``) — not ``NULL``, which means VERIFIED
    complete, a claim no backfilled row can support. ``panel_owed`` stays
    explicitly ``None`` (the row predates the column; we do not know, and
    must not guess, whether a panel was even owed) rather than left unset —
    unset and ``None`` read back identically through the ORM, but only the
    explicit assignment documents that this is a deliberate "unknown", not an
    oversight.
    """
    scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
    weighted_score, band = _score_and_band(verdict)
    red_flags = verdict.get("red_flags")
    milestones = verdict.get("suggested_derisking_milestones")  # F1.1
    channel_name = _bounded_str(verdict.get("channel_name"), 100) or "unknown"

    return OpportunityAssessment(
        id=uuid.uuid4(),
        simulation_run_id=drop.simulation_run_id,
        agent_id=drop.agent_id,
        subject_agent_id=drop.subject_agent_id,
        thread_id=drop.thread_id,
        channel_name=channel_name,
        slack_ts=None,
        company_or_project=_str_or_none(verdict.get("company_or_project")),
        funnel_stage=_bounded_str(verdict.get("funnel_stage"), 20),
        recommendation=_bounded_str(verdict.get("recommendation"), 30),
        confidence=_bounded_str(verdict.get("confidence"), 20),
        weighted_score=weighted_score,
        band=band,
        gating=_normalize_gating(verdict.get("gating")),
        scores=scores or None,
        red_flags=red_flags if isinstance(red_flags, list) else None,
        derisking_milestones=milestones if isinstance(milestones, list) else None,
        rationale=_str_or_none(verdict.get("rationale")),
        raw_verdict=verdict,
        rubric_version=rubric_version,
        rubric_content_hash=rubric_hash,
        # F1.2 — see the docstring above. Explicit, not omitted: this row's
        # floor never ran, so it cannot claim a verified (False/NULL) panel,
        # and must not guess an owed/not-owed panel_owed either. The
        # assessment page's `unrecorded` state is what this renders as.
        panel_incomplete=False,
        missing_domains=[],
        panel_owed=None,
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="simulation_run_id to backfill")
    ap.add_argument("--dry-run", action="store_true")
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

        # Fetched once, up front, rather than per-drop: `opportunity_assessments`
        # carries none of `llm_call_logs`'s huge blob columns, so one query for
        # the whole run is both correct and cheap. Newly built rows are
        # appended to this same list below (not committed until the very end),
        # so two recoverable drops for the same interview within ONE run are
        # still caught by `_existing_assessment_for` without relying on
        # autoflush semantics.
        existing = list((await db.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().all())

        for drop in drops:
            if _existing_assessment_for(existing, run_id, drop) is not None:
                logger.info(
                    "skip %s — this interview already has an assessment",
                    drop.subject_agent_id,
                )
                skipped += 1
                continue

            # Prefer the sidecar stored on the drop row itself (migration 0035);
            # fall back to scraping llm_call_logs for rows dropped before that
            # column existed, which is the whole reason this script exists.
            verdict = drop.raw_verdict
            source = "drop.raw_verdict"
            if not verdict:
                rows = (await db.execute(
                    _fallback_llm_log_query(run_id, drop)
                )).all()
                verdict, source = _recover_from_llm_logs(rows, drop)
                if verdict is None:
                    logger.warning(
                        "no sidecar in llm_call_logs matched subject %s for %s",
                        drop.subject_agent_id, drop.id,
                    )
                    unrecoverable += 1
                    continue

            if not isinstance(verdict, dict):
                unrecoverable += 1
                continue

            row = _build_assessment_row(
                verdict, drop, args.rubric_version, args.rubric_hash
            )
            logger.info(
                "%s %s -> %s (score %s, band %s) from %s",
                "WOULD WRITE" if args.dry_run else "WRITE",
                drop.subject_agent_id, row.recommendation,
                f"{row.weighted_score:.2f}" if row.weighted_score is not None else "n/a",
                row.band, source,
            )
            if not args.dry_run:
                db.add(row)
                existing.append(row)
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
