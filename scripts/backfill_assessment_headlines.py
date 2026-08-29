"""Re-post `#assessments-summary` headlines that were never posted.

Repairs the loss described in
docs/audits/2026-08-29-lost-assessment-headlines/README.md: an interview that
ended by the `max_thread_messages` timeout (or by abandonment, or by the run's
own shutdown) held a verdict nobody announced, and before 2026-08-29 nothing
looked. The assessment rows are intact; only the public headline is missing.

DRY RUN BY DEFAULT. `--apply` is required to post anything or write anything,
because a headline is a public Slack message that cannot be retracted.

    # see what is owed, post nothing
    python scripts/backfill_assessment_headlines.py --run <uuid>

    # post them
    python scripts/backfill_assessment_headlines.py --run <uuid> --apply

    # a headline IS already in Slack but the row predates 0041: record that
    # fact without posting a duplicate
    python scripts/backfill_assessment_headlines.py --run <uuid> \\
        --assessment <uuid> --stamp-only --apply

**The headline's band/score come from THIS ROW'S OWN stored
`weighted_score`/`band`** (`render_assessment_headline`'s `score`/`band`
override, fix round 1 2026-08-29) — never recomputed from `scores` against
whatever rubric document happens to be loaded when this script runs. That is
what makes the rubric-drift check below ADVISORY rather than a correctness
requirement: a drifted row's rendered number is already correct, because it
is the row's own stored number, not a live recomputation. See
`src/services/assessment_headline.py`'s module docstring for the full
rationale and the production measurement that forced this (run `61ccad6d`'s
rows are stamped rubric 3.2.0 against a 3.4.0 live document).

Rows whose `rubric_content_hash` differs from the live document are SKIPPED
by default anyway when POSTING — a drifted row is still worth an operator's
attention before it goes to a public channel — unless `--allow-rubric-drift`
is passed. This check applies ONLY to the posting path: `--stamp-only` never
consults it at all, regardless of `--allow-rubric-drift`, because stamping
renders nothing and posts nothing, so there is no number that could be
misreported by any rubric revision. A row with NO stamp at all
(`rubric_content_hash IS NULL` — it predates the rubric-stamp column, or the
stamping regime) is NOT drift either way: there is nothing to compare
against, so it is posted like any other owed row (see
`select_rows_needing_headline`).

Never posts a row whose `summary_posted_at` is already set (migration 0041 —
deliberately never backfilled, so a pre-0041 row reads NULL even when its
headline is already in Slack; that gap is exactly what `--stamp-only` is for).
A post that fails leaves that row's `summary_posted_at` untouched so a later
run can retry it. Exits 0 only if every intended post succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

# Prefer the mounted project root over any baked-in copy of `src` in
# site-packages (the image installs src/ non-editable) — same guard
# scripts/backfill_dropped_verdicts.py uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.slack_client import AgentSlackClient
from src.config import get_settings
from src.models import AgentChannel, AgentRegistry, OpportunityAssessment
from src.services.assessment_headline import render_assessment_headline
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.slack_tokens import env_token, is_valid_token

logger = logging.getLogger("backfill_assessment_headlines")

# Per-row outcomes `apply_headline_repairs` can report. Plain strings rather
# than an enum — nothing outside this module needs to import the type, and
# the tests assert against these same literals.
WOULD_POST = "would_post"
WOULD_STAMP = "would_stamp"
POSTED = "posted"
STAMPED = "stamped"
FAILED = "failed"


def select_rows_needing_headline(
    rows, *, live_rubric_hash: str, allow_rubric_drift: bool,
    for_stamp_only: bool = False,
):
    """Split ``rows`` into (to_post, [(row, why_skipped), ...]).

    Pure and dependency-free on purpose — this is the whole judgement the
    script makes, and it must be testable without a Slack token or a
    database. Works on any object exposing ``summary_posted_at`` and
    ``rubric_content_hash`` (a real ``OpportunityAssessment`` row, or a bare
    ``types.SimpleNamespace`` in a test).

    A row already announced (``summary_posted_at is not None``) is always
    skipped — re-posting a headline that is already public is exactly the
    unretractable duplicate this script must never create. This check
    applies regardless of ``for_stamp_only``.

    A row whose ``rubric_content_hash`` is set and DIFFERS from
    ``live_rubric_hash`` is skipped as rubric drift, unless
    ``allow_rubric_drift`` is passed — but ONLY on the posting path
    (``for_stamp_only=False``, the default). Fix round 1 (2026-08-29):
    stamping renders nothing and posts nothing, so there is no number that
    could be published wrongly, and gating it on drift made the exact
    production situation this script exists for unfixable — five
    already-in-Slack rows from run `61ccad6d`, all stamped rubric 3.2.0
    against a 3.4.0 live document, would all have been refused a
    ``--stamp-only`` pass. When ``for_stamp_only`` is true this check is
    skipped entirely, independent of ``allow_rubric_drift``.

    A row with NO stamp at all (``rubric_content_hash`` absent or ``None``)
    is deliberately NOT treated as drift — there is nothing to compare
    against, and "we don't know which rubric this was scored under" is not
    the same fact as "we know it was a different one". It is posted (or
    stamped) like any other owed row.
    """
    to_post = []
    skipped = []
    for row in rows:
        if row.summary_posted_at is not None:
            skipped.append((row, "already announced (summary_posted_at is set)"))
            continue
        if for_stamp_only:
            to_post.append(row)
            continue
        stamped = getattr(row, "rubric_content_hash", None)
        if (
            not allow_rubric_drift
            and stamped
            and live_rubric_hash
            and stamped != live_rubric_hash
        ):
            skipped.append((
                row,
                f"rubric revision differs: row stamped {stamped}, live "
                f"document is {live_rubric_hash} (advisory only — the "
                "headline uses this row's OWN stored score/band, not a live "
                "recomputation) — pass --allow-rubric-drift to post anyway",
            ))
            continue
        to_post.append(row)
    return to_post, skipped


def apply_headline_repairs(
    rows_and_texts: list[tuple[object, str]],
    *,
    client_for: Callable[[str], object | None],
    apply: bool,
    stamp_only: bool,
) -> list[tuple[object, str, str]]:
    """Execute (or preview) the post/stamp decision for each ``(row, text)``
    pair, in order. Returns a list of ``(row, text, outcome)`` triples, where
    ``outcome`` is one of the module-level ``WOULD_POST`` / ``WOULD_STAMP`` /
    ``POSTED`` / ``STAMPED`` / ``FAILED`` constants, for the caller to log
    and tally.

    Pure aside from calling ``client_for`` and the returned client's own
    ``post_message`` — no database session, no argparse, no logging — which
    is what lets this run against a bare row object (a
    ``types.SimpleNamespace`` in a test) and a fake Slack client, with no
    database and no real Slack workspace at all.

    Mutates ``row.summary_posted_at`` in place, and ONLY on an outcome of
    ``POSTED`` or ``STAMPED``: a Slack post that raises, or that returns a
    falsy result (``AgentSlackClient.post_message``'s own "not connected"
    contract returns ``None``), is recorded as ``FAILED`` and the row is left
    untouched — so a caller that commits only rows in one of the two success
    states can never durably record a post or stamp that did not actually
    happen, and a later run can retry a ``FAILED`` row.
    """
    results: list[tuple[object, str, str]] = []
    for row, text in rows_and_texts:
        if not apply:
            results.append((row, text, WOULD_STAMP if stamp_only else WOULD_POST))
            continue

        if stamp_only:
            row.summary_posted_at = datetime.now(UTC)
            results.append((row, text, STAMPED))
            continue

        client = client_for(getattr(row, "agent_id", None))
        if client is None:
            results.append((row, text, FAILED))
            continue

        try:
            result = client.post_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
        except Exception:
            logger.exception(
                "FAILED to post headline for assessment %s (%s)",
                getattr(row, "id", "?"), getattr(row, "subject_agent_id", "?"),
            )
            results.append((row, text, FAILED))
            continue

        if not result:
            results.append((row, text, FAILED))
            continue

        row.summary_posted_at = datetime.now(UTC)
        results.append((row, text, POSTED))
    return results


def exit_code_for(results: list[tuple[object, str, str]]) -> int:
    """0 if every intended post/stamp succeeded (or nothing was attempted —
    dry run, or an empty selection), 1 if any row's outcome was ``FAILED``."""
    return 1 if any(outcome == FAILED for _, _, outcome in results) else 0


async def _load_rows(
    db, run_id: uuid.UUID, assessment_ids: list[uuid.UUID] | None,
) -> list[OpportunityAssessment]:
    stmt = select(OpportunityAssessment).where(
        OpportunityAssessment.simulation_run_id == run_id
    )
    if assessment_ids:
        stmt = stmt.where(OpportunityAssessment.id.in_(assessment_ids))
    stmt = stmt.order_by(OpportunityAssessment.created_at)
    return list((await db.execute(stmt)).scalars().all())


async def _load_pi_labels(db) -> dict[str, str]:
    """``agent_id`` -> ``pi_name`` for every registered agent, regardless of
    status — a subject that was later suspended/deleted must still resolve
    to its own name, not fall through to the id."""
    rows = (await db.execute(
        select(AgentRegistry.agent_id, AgentRegistry.pi_name)
    )).all()
    return {r.agent_id: r.pi_name for r in rows}


async def _load_posting_tokens(db) -> dict[str, str]:
    """``agent_id`` -> a usable bot token, mirroring the roster-load fallback
    in ``src/agent/main.py``/``scripts/backfill_slack_history_to_db.py``:
    prefer the DB column, fall back to an env-provided token for the same
    agent id.
    """
    rows = (await db.execute(
        select(AgentRegistry.agent_id, AgentRegistry.slack_bot_token)
    )).all()
    tokens: dict[str, str] = {}
    for r in rows:
        tok = r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(r.agent_id)
        if is_valid_token(tok):
            tokens[r.agent_id] = tok
    return tokens


async def _load_channel_id_map(db, run_id: uuid.UUID) -> dict[str, str]:
    """``channel_name`` -> ``channel_id`` for this run, mirroring the engine's
    own ``self._channel_id_map`` (``SimulationEngine._sync_private_channels_from_db``
    and its seeded-channel counterpart) — used only to resolve a permalink
    for the row's own interview channel, never to post to it.
    """
    rows = (await db.execute(
        select(AgentChannel.channel_name, AgentChannel.channel_id).where(
            AgentChannel.simulation_run_id == run_id
        )
    )).all()
    return {r.channel_name: r.channel_id for r in rows}


def _render_for(row: OpportunityAssessment, pi_labels: dict[str, str], permalink: str | None) -> str:
    pi_label = pi_labels.get(row.subject_agent_id) if row.subject_agent_id else None
    pi_label = pi_label or row.subject_agent_id or "Unknown lab"
    return render_assessment_headline(
        pi_label=pi_label,
        project=row.company_or_project,
        recommendation=row.recommendation,
        scores=row.scores,
        permalink=permalink,
        # Fix round 1: replay the row's OWN stored band/score rather than
        # recomputing from `scores` against whatever rubric is live today —
        # see the module docstring and assessment_headline.py's.
        score=row.weighted_score,
        band=row.band,
    )


def _rubric_note(row: OpportunityAssessment, live_version: str, live_hash: str) -> str:
    """A one-line, human-legible statement of which rubric revision this
    row's band/score came from — preview-only. It plays no role in the
    post/stamp decision: Fix round 1 made the drift gate advisory rather than
    a correctness requirement, once the headline stopped recomputing from the
    live rubric and started replaying the row's own stored values.
    """
    stamped_hash = row.rubric_content_hash
    stamped_version = row.rubric_version
    if not stamped_hash:
        return "rubric: UNSTAMPED (no rubric_version/rubric_content_hash on this row)"
    if stamped_hash == live_hash:
        return f"rubric: {stamped_version}/{stamped_hash} (matches the live document)"
    return (
        f"rubric: {stamped_version}/{stamped_hash} (DIFFERS from the live "
        f"document, {live_version}/{live_hash} — the band/score above are "
        "this row's own stored values, not a live recomputation)"
    )


def _resolve_permalink(
    row: OpportunityAssessment,
    client: AgentSlackClient | None,
    channel_id_map: dict[str, str],
) -> str | None:
    """Best-effort permalink lookup. Never raises — a failure here must
    degrade to ``None`` (the renderer's own "(link unavailable)"), never
    abort the post, mirroring ``_post_assessment_summary``'s own inner
    try/except around ``aget_permalink``.
    """
    if not row.slack_ts or not client:
        return None
    source_channel_id = channel_id_map.get(row.channel_name)
    if not source_channel_id:
        return None
    try:
        return client.get_permalink(source_channel_id, row.slack_ts)
    except Exception:
        logger.warning(
            "Could not resolve a permalink for assessment %s (thread %s)",
            row.id, row.thread_id, exc_info=True,
        )
        return None


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Re-post #assessments-summary headlines for opportunity_assessments "
            "rows whose interview concluded but whose headline never reached "
            "Slack. Dry run by default; --apply is required to post or write "
            "anything."
        ),
    )
    ap.add_argument("--run", required=True, help="simulation_run_id to repair")
    ap.add_argument(
        "--assessment", action="append", default=None, dest="assessment_ids",
        help="Restrict to this opportunity_assessments id. Repeatable.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually post to Slack (or stamp, with --stamp-only) and write "
             "the database. Without this flag nothing is posted or written.",
    )
    ap.add_argument(
        "--stamp-only", action="store_true",
        help="Write summary_posted_at without posting to Slack — for a row "
             "whose headline a human has confirmed is already in the "
             "channel. Never gated on rubric drift: stamping renders and "
             "posts nothing, so there is no number that could be "
             "misreported by any rubric revision.",
    )
    ap.add_argument(
        "--allow-rubric-drift", action="store_true",
        help="Post a row even when its rubric_content_hash differs from the "
             "live rubric document (skipped by default). Advisory only: "
             "the headline always renders this row's OWN stored "
             "weighted_score/band, never a live recomputation, so a "
             "drifted row's rendered number is already correct — this flag "
             "only controls whether the drift SKIP (a chance for an "
             "operator to double-check) happens before posting. Has no "
             "effect with --stamp-only, which never consults rubric drift.",
    )
    return ap


async def main() -> int:
    ap = _build_arg_parser()
    args = ap.parse_args()
    run_id = uuid.UUID(args.run)
    assessment_ids = (
        [uuid.UUID(a) for a in args.assessment_ids] if args.assessment_ids else None
    )

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        rows = await _load_rows(db, run_id, assessment_ids)
        logger.info("found %d assessment row(s) for run %s", len(rows), run_id)

        to_post, skip_pairs = select_rows_needing_headline(
            rows,
            live_rubric_hash=RUBRIC_CONTENT_HASH,
            allow_rubric_drift=args.allow_rubric_drift,
            for_stamp_only=args.stamp_only,
        )

        skipped_count = len(skip_pairs)
        for row, why in skip_pairs:
            logger.info("SKIP %s (%s): %s", row.id, row.subject_agent_id, why)

        if not to_post:
            logger.info("nothing owed a headline for this selection")
            await engine.dispose()
            return 0

        pi_labels = await _load_pi_labels(db)
        channel_id_map = await _load_channel_id_map(db, run_id)
        # agent_id -> token, NOT keyed by the verdict's subject — the posting
        # identity is the verdict's AUTHOR (row.agent_id, normally
        # "blackbird"), never the PI it assessed.
        tokens = await _load_posting_tokens(db)
        clients: dict[str, AgentSlackClient | None] = {}

        def _client_for(agent_id: str | None) -> AgentSlackClient | None:
            """Lazily connect (and cache) one client per authoring agent_id.

            Connecting is a read-only Slack call (auth.test) and is attempted
            regardless of --apply so a dry-run preview can show a real
            permalink — it is the post itself that is gated on --apply, not
            the connection.
            """
            if not agent_id:
                return None
            if agent_id in clients:
                return clients[agent_id]
            token = tokens.get(agent_id)
            client = AgentSlackClient(agent_id=agent_id, bot_token=token) if token else None
            if client is not None and not client.connect():
                client = None
            clients[agent_id] = client
            return client

        rows_and_texts: list[tuple[OpportunityAssessment, str]] = []
        for row in to_post:
            client = _client_for(row.agent_id)
            permalink = _resolve_permalink(row, client, channel_id_map)
            text = _render_for(row, pi_labels, permalink)
            rows_and_texts.append((row, text))

        results = apply_headline_repairs(
            rows_and_texts,
            client_for=_client_for,
            apply=args.apply,
            stamp_only=args.stamp_only,
        )

        posted = stamped = failed = 0
        for row, text, outcome in results:
            note = _rubric_note(row, RUBRIC_VERSION, RUBRIC_CONTENT_HASH)
            if outcome == WOULD_POST:
                logger.info(
                    "WOULD POST %s (%s) [%s]: %s", row.id, row.subject_agent_id, note, text,
                )
            elif outcome == WOULD_STAMP:
                logger.info(
                    "WOULD STAMP %s (%s) [%s]: %s", row.id, row.subject_agent_id, note, text,
                )
            elif outcome == POSTED:
                logger.info("POSTED %s (%s): %s", row.id, row.subject_agent_id, text)
                posted += 1
            elif outcome == STAMPED:
                logger.info("STAMPED %s (%s): %s", row.id, row.subject_agent_id, text)
                stamped += 1
            else:
                logger.error(
                    "FAILED to post/stamp %s (%s)", row.id, row.subject_agent_id,
                )
                failed += 1

        if args.apply and (posted or stamped):
            await db.commit()

    await engine.dispose()

    logger.info(
        "done: %d posted, %d stamped, %d skipped, %d failed",
        posted, stamped, skipped_count, failed,
    )
    return exit_code_for(results)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
