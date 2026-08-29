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

Rows whose `rubric_content_hash` differs from the live document are SKIPPED by
default: the headline renders a band and score, and rendering an old verdict
against today's rubric would publish a number the stored row does not carry.
`--allow-rubric-drift` overrides that deliberately. A row with NO stamp at all
(`rubric_content_hash IS NULL` — it predates the rubric-stamp column, or the
stamping regime) is NOT drift: there is nothing to compare against, so it is
posted like any other owed row (see `select_rows_needing_headline`).

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
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH
from src.services.slack_tokens import env_token, is_valid_token

logger = logging.getLogger("backfill_assessment_headlines")


def select_rows_needing_headline(
    rows, *, live_rubric_hash: str, allow_rubric_drift: bool,
):
    """Split ``rows`` into (to_post, [(row, why_skipped), ...]).

    Pure and dependency-free on purpose — this is the whole judgement the
    script makes, and it must be testable without a Slack token or a
    database. Works on any object exposing ``summary_posted_at`` and
    ``rubric_content_hash`` (a real ``OpportunityAssessment`` row, or a bare
    ``types.SimpleNamespace`` in a test).

    A row already announced (``summary_posted_at is not None``) is always
    skipped — re-posting a headline that is already public is exactly the
    unretractable duplicate this script must never create.

    A row whose ``rubric_content_hash`` is set and DIFFERS from
    ``live_rubric_hash`` is skipped as rubric drift, unless
    ``allow_rubric_drift`` is passed: the headline renders a band/score
    computed against the LIVE rubric document
    (``render_assessment_headline`` -> ``weighted_score``/``band``), and
    doing that for a verdict scored under a different rubric would publish a
    number the stored row does not actually carry.

    A row with NO stamp at all (``rubric_content_hash`` absent or ``None``)
    is deliberately NOT treated as drift — there is nothing to compare
    against, and "we don't know which rubric this was scored under" is not
    the same fact as "we know it was a different one". It is posted like any
    other owed row.
    """
    to_post = []
    skipped = []
    for row in rows:
        if row.summary_posted_at is not None:
            skipped.append((row, "already announced (summary_posted_at is set)"))
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
                f"rubric drift: row stamped {stamped}, live document is "
                f"{live_rubric_hash} — pass --allow-rubric-drift to post anyway",
            ))
            continue
        to_post.append(row)
    return to_post, skipped


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
             "whose headline a human has confirmed is already in the channel.",
    )
    ap.add_argument(
        "--allow-rubric-drift", action="store_true",
        help="Post a row even when its rubric_content_hash differs from the "
             "live rubric document (skipped by default).",
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

    posted = stamped = skipped_count = failed = 0

    async with factory() as db:
        rows = await _load_rows(db, run_id, assessment_ids)
        logger.info("found %d assessment row(s) for run %s", len(rows), run_id)

        to_post, skip_pairs = select_rows_needing_headline(
            rows,
            live_rubric_hash=RUBRIC_CONTENT_HASH,
            allow_rubric_drift=args.allow_rubric_drift,
        )

        for row, why in skip_pairs:
            logger.info("SKIP %s (%s): %s", row.id, row.subject_agent_id, why)
            skipped_count += 1

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

        def _client_for(agent_id: str) -> AgentSlackClient | None:
            """Lazily connect (and cache) one client per authoring agent_id.

            Connecting is a read-only Slack call (auth.test) and is attempted
            regardless of --apply so a dry-run preview can show a real
            permalink — it is `post_message`/`apost_message` that is gated on
            --apply, not the connection itself.
            """
            if agent_id in clients:
                return clients[agent_id]
            token = tokens.get(agent_id)
            client = AgentSlackClient(agent_id=agent_id, bot_token=token) if token else None
            if client is not None and not client.connect():
                client = None
            clients[agent_id] = client
            return client

        for row in to_post:
            client = _client_for(row.agent_id)
            permalink = _resolve_permalink(row, client, channel_id_map)
            text = _render_for(row, pi_labels, permalink)

            if not args.apply:
                logger.info(
                    "WOULD %s %s (%s): %s",
                    "STAMP" if args.stamp_only else "POST",
                    row.id, row.subject_agent_id, text,
                )
                continue

            if args.stamp_only:
                logger.info("STAMP %s (%s): %s", row.id, row.subject_agent_id, text)
                row.summary_posted_at = datetime.now(UTC)
                stamped += 1
                continue

            if client is None:
                logger.error(
                    "FAILED to post %s (%s): no Slack client available",
                    row.id, row.subject_agent_id,
                )
                failed += 1
                continue

            try:
                result = client.post_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
            except Exception:
                logger.exception(
                    "FAILED to post headline for assessment %s (%s)",
                    row.id, row.subject_agent_id,
                )
                failed += 1
                continue

            if result is None:
                # Mirrors AgentSlackClient.post_message's own "not connected"
                # contract: None means nothing was posted, so this row must
                # NOT be stamped — a later run needs to be able to retry it.
                logger.error(
                    "FAILED to post headline for assessment %s (%s): "
                    "post_message returned no result",
                    row.id, row.subject_agent_id,
                )
                failed += 1
                continue

            logger.info("POSTED %s (%s): %s", row.id, row.subject_agent_id, text)
            row.summary_posted_at = datetime.now(UTC)
            posted += 1

        if args.apply and (posted or stamped):
            await db.commit()

    await engine.dispose()

    logger.info(
        "done: %d posted, %d stamped, %d skipped, %d failed",
        posted, stamped, skipped_count, failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
