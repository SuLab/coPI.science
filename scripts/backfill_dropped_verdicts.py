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

* **Stamps the rubric version/hash from what the RUN ITSELF already wrote**,
  not from a hardcoded default and not from whatever ``blackbird_rubric``
  imports at backfill time — see ``_derive_rubric_stamp``. A row stamped with
  today's rubric but scored under the run's would silently break every
  cross-run comparison the stamp exists to protect; a row stamped from one
  run's default applied to a DIFFERENT run would do the same thing more
  quietly. The supervised re-run spans three run ids (``88d81cd8``,
  ``076e80b6``, ``8b64a0e0``), which is exactly the scenario a single
  hardcoded default cannot serve.
* **Does not fire the ``#assessments-summary`` headline.** Those posts are
  announcements of a live verdict; replaying them weeks later would be noise,
  and design D14's promise is about what a drop does NOT post, not about
  retroactive completeness.

Idempotent: skips any ``(run, thread_id)`` that already has an assessment row;
when EITHER side of a candidate pair lacks a ``thread_id`` — the existing row
(every row this script wrote before this fix, since it never populated the
column) or the drop itself (not producible by ``_persist_assessment`` today,
but not this script's invariant to assume) — falls back to ``(run,
subject_agent_id)``. See ``_existing_assessment_for``.

(This docstring claimed "(run, thread)" from the day this script was written,
while the code beneath it keyed on ``subject_agent_id`` alone with no
``thread_id`` column to key on at all — a docstring/code mismatch, not a
behaviour change here: this fix is what finally makes the two agree, now that
migration 0036 gives ``opportunity_assessments`` a ``thread_id`` to key on.)

The ``llm_call_logs`` fallback (when a drop has no ``raw_verdict`` of its own)
walks backward from the drop, bounded to ``--max-lookback-seconds`` (default
``_MAX_LOOKBACK_SECONDS``), and accepts only a candidate whose OWN sidecar
names this drop's subject — see ``_subject_matches`` and
``_recover_from_llm_logs``. Both defences are load-bearing independently:
measured on the nine real recoverable drops across all three runs, the 60s
cap alone would still hand ``pienta`` a candidate list containing
``huganir``'s sidecar (two candidates inside the window, the nearer one
huganir's), and the subject check alone would still accept ``hart``'s
4½-minute-old superseded sidecar if a nearer same-subject one didn't exist to
out-rank it.

Usage (inside the app container, AFTER migration 0036 is applied — that is
what added ``opportunity_assessments.panel_owed`` and ``.thread_id``, both
written below):

    docker compose -f docker-compose.prod.yml exec blackbird-app \
        python scripts/backfill_dropped_verdicts.py --run 8b64a0e0-1fa7-40c4-b9a2-f57a4e058fb0
    # preview only:
    ... python scripts/backfill_dropped_verdicts.py --run <uuid> --dry-run
    # override the derived rubric stamp (must be given together):
    ... python scripts/backfill_dropped_verdicts.py --run <uuid> \
            --rubric-version 2.0.0 --rubric-hash e3ef75f84c48
    # widen the llm_call_logs lookback if a row comes back unrecoverable:
    ... python scripts/backfill_dropped_verdicts.py --run <uuid> --max-lookback-seconds 300
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

# NOT configured here (fix round 1, FIX 7): `logging.basicConfig` used to run
# at import time, which fired on every test collection too. Configured only
# under `if __name__ == "__main__"` below.
logger = logging.getLogger("backfill_dropped_verdicts")

_SIDECAR_RE = re.compile(
    r"<assessment_json>\s*(.*?)\s*</assessment_json>", re.DOTALL
)

# The reasons that mean "a complete verdict existed and was thrown away".
# `missing_sidecar` and `empty_reply` are NOT recoverable — nothing was emitted.
_RECOVERABLE = ("premature_sidecar", "duplicate_thread_verdict", "unparseable_sidecar")

# Fix round 1, FIX 2: how far back `_recover_from_llm_logs` may walk before
# giving up, in seconds. Measured directly on the nine real recoverable drops
# across all three runs in the supervised re-run (88d81cd8, 076e80b6,
# 8b64a0e0): every one of them has its own matching sidecar 0.2-0.3s before
# the drop, so 60s is a ~200x margin on the real cases — and it is tight
# enough to exclude hart's 272.6s-old SUPERSEDED sidecar (a
# `duplicate_thread_verdict` drop whose near candidate failed `json.loads`;
# without a cap, the unbounded walk falls through to the stale one 4.5
# minutes back and writes it, unmarked, as this interview's verdict — it
# doesn't bite in practice only because hart is also caught by
# `_existing_assessment_for` first). Overridable via `--max-lookback-seconds`
# for a supervisor who hits a row that comes back unrecoverable.
_MAX_LOOKBACK_SECONDS = 60.0


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


def _subject_matches(candidate_subject: object, drop_subject: str | None) -> bool:
    """Can a sidecar's own ``subject_agent_id`` be trusted to name the drop's subject?

    Fix round 1, FIX 1 — an amendment to the original brief, ruled in: strict
    equality loses real, recoverable verdicts. The phase-4 prompt never shows
    the hub its interview partner's real ``agent_id`` — only
    ``{other_agent_name}`` (``bot_name``, generated as ``{LastName}Bot``) — so
    a sidecar naming the bot is not a wrong guess, it is the ONLY name the
    model was ever given for that PI. Measured directly on all 63 production
    ``opportunity_assessments`` rows: exactly four have a
    ``raw_verdict->>'subject_agent_id'`` that differs from the stored
    ``subject_agent_id`` —

        dang    -> dangbot
        krieger -> kriegerbot
        lee     -> leebot
        pearce  -> epearce

    — three of which are the bot-name form and MUST be accepted, and the
    fourth of which MUST be refused: ``epearce``/``pearce`` are two
    genuinely different agents (a last-name collision gets a first-initial
    prefix, ``src/agent/agent.py``'s roster rules), not a naming variant of
    one. So the match is deliberately narrow: case-folded exact equality, or
    case-folded equality against ``f"{drop_subject}bot"``. Nothing looser —
    in particular no substring or prefix check, which is exactly what would
    also accept ``epearce`` for ``pearce``.

    Returns ``False`` (never raises) for a non-string or empty
    ``candidate_subject``/``drop_subject`` — the caller already handles "no
    subject to check" as a refusal, not a crash.
    """
    if not isinstance(candidate_subject, str) or not candidate_subject:
        return False
    if not drop_subject:
        return False
    candidate_folded = candidate_subject.casefold()
    drop_folded = drop_subject.casefold()
    return candidate_folded in (drop_folded, f"{drop_folded}bot")


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
    run. The fallback covers exactly that: when EITHER side of a candidate
    pair lacks a ``thread_id`` to key on, match on ``subject_agent_id``
    instead.

    Fix round 1, FIX 6: the fallback checks EITHER side's ``thread_id``, not
    just the existing row's. Checking only the existing row's left a gap —
    a drop with ``thread_id IS NULL`` against an existing (thread-keyed) row
    for the same subject fired neither clause, and a duplicate would be
    written. Unreachable today (``_persist_assessment`` never writes
    ``thread_id``, so nothing in this fallback's INPUT can have a real
    thread_id... except a row THIS SCRIPT wrote, since fix round 1 wrote
    ``thread_id=drop.thread_id`` — so this becomes reachable the moment two
    recoverable drops for the same subject appear in one run and the first
    one's drop happened to carry a thread_id while a later one's did not, or
    the moment a later task teaches ``_persist_assessment`` to write
    ``thread_id`` at all (plan task A2.4, out of scope here). Either way it
    is this function's own invariant to hold, not something to leave
    depending on what else happens to be true today.

    The fallback still requires an exact ``subject_agent_id`` match — never
    ``drop.thread_id is not None and row.thread_id != drop.thread_id`` for
    two DIFFERENT known thread ids, which is a real second interview and
    must not be skipped (see ``test_a_second_interview_with_the_same_pi_is_not_skipped``).
    """
    for row in candidates:
        if row.simulation_run_id != run_id:
            continue
        if drop.thread_id is not None and row.thread_id == drop.thread_id:
            return row
        if row.subject_agent_id == drop.subject_agent_id and (
            row.thread_id is None or drop.thread_id is None
        ):
            return row
    return None


def _fallback_llm_log_query(run_id: uuid.UUID, drop: AssessmentDrop):
    """The narrowed ``llm_call_logs`` scrape query (F1.4).

    Selects only ``id``/``created_at``/``response_text`` — the whole-row
    ``select(LlmCallLog)`` this replaces also pulled ``system_prompt`` and
    ``messages_json``, hundreds of ~30 KB prompts, for three columns' worth of
    actual use.

    Bounded above by ``drop.created_at`` (a candidate cannot postdate the
    drop it explains) and ordered most-recent-first so
    ``_recover_from_llm_logs`` walks backward in time. The LOWER time bound
    (``--max-lookback-seconds``, fix round 1 FIX 2) is enforced in
    ``_recover_from_llm_logs`` instead of here — it is a property of the
    WALK, independently unit-testable there without a database, and cheap
    enough at this table's per-run row counts (single digits to low hundreds
    for one hub agent_id) that pushing it into SQL as well would only be a
    minor performance nicety, not a correctness requirement.
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
    rows: Iterable[tuple],
    drop: AssessmentDrop,
    max_lookback_seconds: float = _MAX_LOOKBACK_SECONDS,
) -> tuple[dict | None, str | None]:
    """Pick the ``llm_call_logs`` row that is actually THIS interview's sidecar.

    ``rows`` is ``(id, created_at, response_text)`` tuples, most recent
    first, already filtered to the drop's ``agent_id`` and to at-or-before
    its ``created_at`` (see ``_fallback_llm_log_query``). The old code took
    whichever of these was simply last in time — "it happened to work on the
    two real rows (log rows 249 ms and 232 ms before their drops) but nothing
    enforces it" (task brief).

    TWO independent defences, and both are load-bearing (fix round 1,
    addendum — measured directly on the nine real recoverable drops in the
    supervised re-run's window ``(drop.created_at - 60s, drop.created_at]``):

    * A candidate more than ``max_lookback_seconds`` before the drop is
      skipped outright (FIX 2). Without this, hart's `duplicate_thread_verdict`
      drop — whose nearest candidate fails to parse — falls through to a
      272.6s-old SUPERSEDED sidecar and would write it unmarked.
    * A candidate whose own sidecar names a different subject is skipped
      (via ``_subject_matches``, F1.4/FIX 1) even when it is inside the
      window. Without this, ``pienta`` — which has TWO candidates inside the
      60s window — would take huganir's sidecar, the nearer of the two. The
      cap alone does not make this check redundant.
    """
    for log_id, created_at, response_text in rows:
        delta = (drop.created_at - created_at).total_seconds()
        if delta > max_lookback_seconds:
            continue
        match = _SIDECAR_RE.search(response_text or "")
        if not match:
            continue
        try:
            candidate = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        if not _subject_matches(candidate.get("subject_agent_id"), drop.subject_agent_id):
            continue
        return candidate, f"llm_call_logs {log_id} ({delta:.1f}s before drop)"
    return None, None


def _derive_rubric_stamp(
    existing: Iterable[OpportunityAssessment],
) -> tuple[str | None, str | None]:
    """Stamp from what the RUN ITSELF already wrote (fix round 1, FIX 5).

    The module docstring has always promised to stamp a backfilled row from
    the run, not from today's ``blackbird_rubric`` import or a hardcoded
    default — but the code shipped with exactly the latter: CLI defaults of
    run 8b64a0e0's own stamp (``2.0.0``/``e3ef75f84c48``), applied silently
    to ANY ``--run``. Measured directly: run 076e80b6 happens to carry the
    identical stamp (so the bug was invisible there), but 88d81cd8's rows
    carry NULL stamps — a hardcoded default would have fabricated a rubric
    version for a run that recorded none.

    ``existing`` is this run's own ``opportunity_assessments`` rows (already
    fetched once by ``main`` for idempotency — reused here rather than
    queried again).

      * Every row's pair is ``(None, None)`` (the run recorded no stamp at
        all) -> return ``(None, None)``. A backfilled row must not invent a
        stamp its own run never wrote.
      * Exactly one distinct non-NULL pair -> that IS the run's rubric;
        return it.
      * More than one distinct pair -> the run's own rows disagree (a rubric
        edit mid-run, or a genuinely mixed dataset), and guessing which one
        applies to an unstamped drop would fabricate a fact nobody recorded.
        Raises rather than picks one; the caller's job is to refuse to run
        and tell the operator to pass ``--rubric-version``/``--rubric-hash``
        explicitly.

    A row is counted as "stamped" if EITHER column is non-NULL — every real
    write path sets both together, but nothing here should assume that.
    """
    pairs = {
        (row.rubric_version, row.rubric_content_hash)
        for row in existing
        if row.rubric_version is not None or row.rubric_content_hash is not None
    }
    if not pairs:
        return None, None
    if len(pairs) > 1:
        raise ValueError(
            "this run's opportunity_assessments carry more than one rubric "
            f"stamp ({sorted(pairs)}); pass --rubric-version and "
            "--rubric-hash explicitly rather than guessing which applies"
        )
    return next(iter(pairs))


def _build_assessment_row(
    verdict: dict,
    drop: AssessmentDrop,
    rubric_version: str | None,
    rubric_hash: str | None,
) -> OpportunityAssessment:
    """Build the row ``_persist_assessment`` would have written for this verdict.

    Reuses three of its guards (F1.3) rather than writing new ones, because
    this script commits ONCE at the end of the whole batch: unlike the live
    engine's one-row-per-commit writes, a single bad value here would take
    every recovered row down with it, not just its own.

      * ``_bounded_str`` clips the bounded VARCHAR columns
        (``funnel_stage``, ``recommendation``, ``confidence``,
        ``channel_name``, and — fix round 1, FIX 5 — ``rubric_version``/
        ``rubric_content_hash``, both ``String(20)`` and, unlike the other
        four, OPERATOR-sourced via ``--rubric-version``/``--rubric-hash``
        rather than model-sourced) instead of letting an over-long value
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
        # Fix round 1, FIX 5: guarded the same way as every other bounded
        # VARCHAR on this row, even though these two are operator- rather
        # than model-sourced — an operator's --rubric-version typo is the
        # same StringDataRightTruncation risk as an over-long recommendation.
        rubric_version=_bounded_str(rubric_version, 20),
        rubric_content_hash=_bounded_str(rubric_hash, 20),
        # F1.2 — see the docstring above. Explicit, not omitted: this row's
        # floor never ran, so it cannot claim a verified (False/NULL) panel,
        # and must not guess an owed/not-owed panel_owed either.
        #
        # The assessment page renders this as `unverified`, NOT `unrecorded`:
        # `assessment_detail.panel_state` tests `missing_domains is not None`
        # BEFORE it looks at `panel_owed`, and `[]` is not None. Both states are
        # non-green and both mean "unvetted", so the code is right and it is
        # this comment that was wrong — but they are not interchangeable, and
        # `[]` is the more accurate of the two here: it says the floor could not
        # check, which is exactly what happened.
        panel_incomplete=False,
        missing_domains=[],
        panel_owed=None,
    )


def _positive_seconds(value: str) -> float:
    """``type=`` for ``--max-lookback-seconds`` (fix round 2, item 3).

    A non-positive lookback would silently exclude every candidate — every
    delta is >= 0, so ``delta > max_lookback_seconds`` is true for all of
    them — and report every ``llm_call_logs`` fallback drop unrecoverable,
    with no error at all. Raising ``argparse.ArgumentTypeError`` here (rather
    than checking ``args.max_lookback_seconds`` after ``parse_args()`` inside
    ``main()``) is what lets this be unit-tested against the parser alone,
    with no database: argparse converts this exception into the same
    ``ap.error(...)`` usage-and-exit behaviour automatically.
    """
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"must be positive (got {value!r}); a non-positive lookback "
            "would silently exclude every llm_call_logs candidate"
        )
    return parsed


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="simulation_run_id to backfill")
    ap.add_argument("--dry-run", action="store_true")
    # Fix round 1, FIX 5: no more hardcoded default — see _derive_rubric_stamp.
    # Both or neither: a lone override would let one column drift stamped
    # while the other is derived, defeating the whole point of a single pair.
    ap.add_argument(
        "--rubric-version", default=None,
        help="Override the derived stamp. Must be given together with "
             "--rubric-hash; if omitted, derived from this run's own "
             "opportunity_assessments rows.",
    )
    ap.add_argument(
        "--rubric-hash", default=None,
        help="Override the derived stamp. Must be given together with "
             "--rubric-version.",
    )
    ap.add_argument(
        "--max-lookback-seconds", type=_positive_seconds, default=_MAX_LOOKBACK_SECONDS,
        help="How far back the llm_call_logs fallback may walk for a "
             "matching sidecar before giving up (default: %(default)s).",
    )
    return ap


async def main() -> int:
    ap = _build_arg_parser()
    args = ap.parse_args()
    if (args.rubric_version is None) != (args.rubric_hash is None):
        ap.error("--rubric-version and --rubric-hash must be given together")
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
        # autoflush semantics. Also doubles as `_derive_rubric_stamp`'s input.
        existing = list((await db.execute(
            select(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            )
        )).scalars().all())

        if args.rubric_version is not None:
            rubric_version, rubric_hash = args.rubric_version, args.rubric_hash
        else:
            try:
                rubric_version, rubric_hash = _derive_rubric_stamp(existing)
            except ValueError as exc:
                logger.error("%s", exc)
                return 1
        logger.info(
            "rubric stamp for this run: version=%r hash=%r",
            rubric_version, rubric_hash,
        )

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
                verdict, source = _recover_from_llm_logs(
                    rows, drop, args.max_lookback_seconds
                )
                if verdict is None:
                    logger.warning(
                        "no sidecar in llm_call_logs matched subject %s for %s "
                        "within %.0fs",
                        drop.subject_agent_id, drop.id, args.max_lookback_seconds,
                    )
                    unrecoverable += 1
                    continue

            if not isinstance(verdict, dict):
                unrecoverable += 1
                continue

            row = _build_assessment_row(verdict, drop, rubric_version, rubric_hash)
            logger.info(
                "%s %s -> %s (score %s, band %s) from %s",
                "WOULD WRITE" if args.dry_run else "WRITE",
                drop.subject_agent_id, row.recommendation,
                f"{row.weighted_score:.2f}" if row.weighted_score is not None else "n/a",
                row.band, source,
            )
            # Fix round 1, FIX 7: append regardless of --dry-run, so a
            # preview over two recoverable drops for the SAME interview
            # predicts what a real run would do (write the first, skip the
            # second) instead of showing "WOULD WRITE" for both.
            existing.append(row)
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
