"""Record, and optionally announce, a finished run's hub declines that carried no assessment.

Before migration 0071 a hub ⏸️ decline with no ``<assessment_json>`` sidecar left no
``opportunity_assessments`` row, so the interview appeared neither on the Assessments
page nor in ``#assessments-summary``. Owner decision 2026-10-16: every such close gets a
``hub_declines`` row and a headline. This script writes the rows the engine did not
(declines from runs before the release) and can post their headlines.

PREVIEW BY DEFAULT: nothing is written or posted without a flag.

    # list the run's unrecorded declines, write nothing
    python scripts/backfill_hub_declines.py --run <uuid>

    # write them (one transaction, ON CONFLICT DO NOTHING)
    python scripts/backfill_hub_declines.py --run <uuid> --apply

    # write them, then post every owed decline headline of the run
    python scripts/backfill_hub_declines.py --run <uuid> --apply --post

    # a headline claim left by a transport error: list it, check Slack, then release it
    python scripts/backfill_hub_declines.py --run <uuid> --list-in-doubt
    python scripts/backfill_hub_declines.py --run <uuid> --release-in-doubt <decline id>

**What counts as a decline.** A ``thread_decisions`` row of the run with
``outcome = 'no_proposal'`` and ``closed_by_role = 'scout_hub'``, one of whose two agents
has ``agents.role = 'scout_hub'`` (the hub), with no ``opportunity_assessments`` row and
no ``hub_declines`` row for (run, thread). A lab's own ⏸️ close is not a hub decline.
``reason_text`` is the hub's newest ``agent_messages`` row in that thread, clipped to
``REASON_MAX_CHARS``; a thread with no recorded hub message is skipped, since the
reason is required.

**Posting.** ``--post`` announces each owed row (``decline_claims.owed``: neither claimed
nor posted) through ``build_finalize_announcer(...).announce_decline``, the claim
protocol Finalize uses, so a headline posts at most once. Without ``--apply`` it posts
only rows already in the table.

**In doubt.** A post that hit a transport error with no Slack response keeps its claim, so
nothing re-posts it. ``--list-in-doubt`` (read-only) lists the run's declines claimed more
than ten minutes ago with no post; after checking ``#assessments-summary`` by hand,
``--release-in-doubt <id>`` clears that claim (only an in-doubt row of this run), so the
next ``--post`` announces it. Release only a headline that is not in Slack.

**Engine lock.** ``--apply``, ``--post`` and ``--release-in-doubt`` hold the engine's advisory lock for the whole
run (``acquire_apply_lock`` from ``scripts/backfill_assessment_headlines.py``) and are
refused, exit 2, while an engine or another lock holder has it. The preview takes no
lock.

Exit codes: 0 success; 1 a headline was ``lost`` or ``in_doubt``; 2 refused (the engine
lock is held, or the run is unknown). Re-running is safe: a recorded decline is not
selected again, and a posted headline is not owed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

# Prefer the mounted project root over any baked-in copy of `src` in
# site-packages (the image installs src/ non-editable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.backfill_assessment_headlines import acquire_apply_lock
from src.agent.engine.headlines import build_finalize_announcer
from src.agent.engine.sidecar import _reply_closes_thread
from src.config import get_settings
from src.models import (
    AgentMessage,
    AgentRegistry,
    AssessmentDrop,
    HubDecline,
    OpportunityAssessment,
    SimulationRun,
    ThreadDecision,
)
from src.models.hub_decline import REASON_MAX_CHARS
from src.services import decline_claims
from src.services.assessment_headline import decline_reason

logger = logging.getLogger("backfill_hub_declines")

HUB_ROLE = "scout_hub"
LAB_ROLE = "pi_lab"
#: ``announce_decline`` outcomes that make the exit code 1.
FAILING_OUTCOMES = ("lost", "in_doubt")


async def find_declines(db, run_id: uuid.UUID) -> tuple[list[dict], list[tuple[str, str]]]:
    """The run's unrecorded hub declines as ``hub_declines`` insert values, plus
    ``(thread_id, why)`` for each candidate thread that was skipped.

    One entry per thread: when a thread has several qualifying decisions, the newest
    (``decided_at``) supplies the channel and the agents."""
    decisions = (await db.execute(
        select(ThreadDecision)
        .where(
            ThreadDecision.simulation_run_id == run_id,
            ThreadDecision.outcome == "no_proposal",
            ThreadDecision.closed_by_role == HUB_ROLE,
            ~exists().where(
                OpportunityAssessment.simulation_run_id == run_id,
                OpportunityAssessment.thread_id == ThreadDecision.thread_id,
            ),
            ~exists().where(
                HubDecline.simulation_run_id == run_id,
                HubDecline.thread_id == ThreadDecision.thread_id,
            ),
            # A dropped verdict (a sidecar that was refused or unusable) means the
            # closing reply carried one: that interview is not a decline.
            ~exists().where(
                AssessmentDrop.simulation_run_id == run_id,
                AssessmentDrop.thread_id == ThreadDecision.thread_id,
            ),
        )
        .order_by(ThreadDecision.decided_at.desc(), ThreadDecision.id)
    )).scalars().all()
    agent_ids = {d.agent_a for d in decisions} | {d.agent_b for d in decisions}
    roles = dict((await db.execute(
        select(AgentRegistry.agent_id, AgentRegistry.role)
        .where(AgentRegistry.agent_id.in_(agent_ids))
    )).all()) if agent_ids else {}

    values: list[dict] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for d in decisions:
        if d.thread_id in seen:
            continue
        seen.add(d.thread_id)
        hubs = [a for a in (d.agent_a, d.agent_b) if roles.get(a) == HUB_ROLE]
        if len(hubs) != 1:
            skipped.append((d.thread_id, f"{len(hubs)} of its two agents are a scout_hub"))
            continue
        hub = hubs[0]
        subject = d.agent_b if hub == d.agent_a else d.agent_a
        if roles.get(subject) != LAB_ROLE:
            skipped.append((d.thread_id, f"the other party {subject!r} is not a pi_lab"))
            continue
        # The hub's closing reply: its newest thread reply that opens with ⏸️. A reply
        # over Slack's limit is several rows, and only the first opens with the marker,
        # which is the chunk the engine keeps too.
        replies = (await db.execute(
            select(AgentMessage.content, AgentMessage.slack_ts)
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.agent_id == hub,
                AgentMessage.thread_ts == d.thread_id,
                AgentMessage.phase == "thread_reply",
            )
            .order_by(AgentMessage.posted_at.desc(), AgentMessage.created_at.desc())
        )).all()
        message = next((r for r in replies if _reply_closes_thread(r.content or "")), None)
        if message is None:
            skipped.append((d.thread_id, "no ⏸️ hub reply recorded in the thread"))
            continue
        values.append({
            "id": uuid.uuid4(),
            "simulation_run_id": run_id,
            "agent_id": hub,
            "subject_agent_id": subject,
            "channel_name": d.channel,
            "thread_id": d.thread_id,
            "slack_ts": message.slack_ts,
            "reason_text": (message.content or "")[:REASON_MAX_CHARS],
        })
    return values, skipped


async def insert_declines(db, values: list[dict]) -> int:
    """Insert ``values`` in one transaction; a (run, thread) already recorded is left
    alone. Returns the number of rows inserted."""
    if not values:
        return 0
    inserted = (await db.execute(
        insert(HubDecline)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_hub_declines_run_thread")
        .returning(HubDecline.id)
    )).scalars().all()
    await db.commit()
    return len(inserted)


async def post_owed(factory, run_id: uuid.UUID, *, make_announcer) -> list[tuple[HubDecline, str]]:
    """Announce every owed decline of the run, oldest first; ``(row, outcome)`` each."""
    async with factory() as db:
        rows = await decline_claims.owed(db, run_id)
    if not rows:
        return []
    announcer = await make_announcer(factory, run_id)
    return [(row, await announcer.announce_decline(row)) for row in rows]


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Record a run's hub declines that carried no assessment as hub_declines rows, "
            "and optionally post their #assessments-summary headlines. Preview by default."
        ),
    )
    ap.add_argument("--run", required=True, help="simulation_run_id to backfill")
    ap.add_argument(
        "--apply", action="store_true",
        help="Insert the previewed rows (one transaction, ON CONFLICT DO NOTHING).",
    )
    ap.add_argument(
        "--list-in-doubt", action="store_true",
        help="List the run's declines whose headline claim is in doubt (read-only).",
    )
    ap.add_argument(
        "--release-in-doubt", metavar="DECLINE_ID",
        help="Clear the in-doubt claim of one decline of the run, after checking Slack.",
    )
    ap.add_argument(
        "--post", action="store_true",
        help="Post the headline of every owed decline of the run (not yet claimed or "
             "posted). A headline is public and cannot be retracted.",
    )
    return ap


async def run_backfill(
    args, factory, *, make_announcer=None, lock_url: str | None = None,
) -> int:
    """The whole script, given parsed ``args`` and a session factory.

    ``make_announcer(session_factory, run_id)`` returns the announcer (default
    ``build_finalize_announcer``; tests pass a fake). ``lock_url`` is the database URL
    the engine lock connects to (default: the settings URL). Exit codes as in the module
    docstring."""
    if (args.list_in_doubt or args.release_in_doubt) and (args.apply or args.post):
        logger.error("--list-in-doubt and --release-in-doubt run alone, not with --apply or --post")
        return 2
    if args.list_in_doubt and args.release_in_doubt:
        logger.error("pass one of --list-in-doubt and --release-in-doubt")
        return 2
    try:
        uuid.UUID(args.run)
    except ValueError:
        logger.error("--run must be a full simulation run UUID, got %r", args.run)
        return 2
    lock = None
    if args.apply or args.post or args.release_in_doubt:
        lock = await acquire_apply_lock(lock_url or get_settings().database_url)
        if lock is None:
            logger.error(
                "refusing to write: a simulation engine holds the engine lock "
                "(stop the engine first)"
            )
            return 2
    try:
        return await _backfill_body(
            args, factory, make_announcer=make_announcer or build_finalize_announcer,
        )
    finally:
        if lock is not None:
            await lock.release()


async def _in_doubt(args, factory, run_id: uuid.UUID) -> int:
    """``--list-in-doubt`` / ``--release-in-doubt``; exit 2 for an id that is not an
    in-doubt decline of the run."""
    async with factory() as db:
        rows = await decline_claims.list_in_doubt(db, run_id)
    if args.list_in_doubt:
        for row in rows:
            logger.info("IN DOUBT decline %s (thread %s, %s), claimed %s", row.id,
                        row.thread_id, row.subject_agent_id, row.summary_claimed_at)
        logger.info("%d in-doubt decline headline(s) for run %s", len(rows), run_id)
        return 0
    target = next((r for r in rows if str(r.id) == args.release_in_doubt), None)
    if target is None:
        logger.error("decline %s is not an in-doubt decline of run %s",
                     args.release_in_doubt, run_id)
        return 2
    async with factory() as db:
        await decline_claims.release_claim(db, target.id)
    logger.info("released the headline claim of decline %s (thread %s)", target.id,
                target.thread_id)
    return 0


async def _backfill_body(args, factory, *, make_announcer) -> int:
    run_id = uuid.UUID(args.run)
    async with factory() as db:
        if await db.get(SimulationRun, run_id) is None:
            logger.error("no simulation run %s", run_id)
            return 2
    if args.list_in_doubt or args.release_in_doubt:
        return await _in_doubt(args, factory, run_id)
    async with factory() as db:
        # Before migration 0036 an assessment row had no thread_id, so "this
        # interview was assessed" cannot be told for such a run.
        unthreaded = await db.scalar(select(exists().where(
            OpportunityAssessment.simulation_run_id == run_id,
            OpportunityAssessment.thread_id.is_(None),
        )))
    if unthreaded:
        logger.error(
            "run %s has assessments with no thread_id (written before migration 0036); "
            "its declines cannot be told from its assessed interviews, so nothing is "
            "recorded", run_id,
        )
        return 2
    async with factory() as db:
        values, skipped = await find_declines(db, run_id)
        labels = dict((await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.pi_name)
        )).all())

    for thread_id, why in skipped:
        logger.info("SKIP thread %s: %s", thread_id, why)
    for v in values:
        lab = labels.get(v["subject_agent_id"]) or v["subject_agent_id"] or "Unknown lab"
        logger.info(
            "%s thread %s  lab %s  reason: %s",
            "INSERT" if args.apply else "WOULD INSERT",
            v["thread_id"], lab, decline_reason(v["reason_text"]) or "(none)",
        )

    if args.apply:
        async with factory() as db:
            inserted = await insert_declines(db, values)
        logger.info("inserted %d hub_declines row(s) for run %s", inserted, run_id)
    else:
        logger.info("%d decline(s) to record for run %s (preview)", len(values), run_id)

    if not args.post:
        return 0
    results = await post_owed(factory, run_id, make_announcer=make_announcer)
    for row, outcome in results:
        log = logger.error if outcome in FAILING_OUTCOMES else logger.info
        log("%s decline %s (thread %s, %s)", outcome.upper(), row.id, row.thread_id,
            row.subject_agent_id)
    logger.info("%d owed decline headline(s) announced for run %s", len(results), run_id)
    return 1 if any(outcome in FAILING_OUTCOMES for _, outcome in results) else 0


async def main() -> int:
    args = _build_arg_parser().parse_args()
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return await run_backfill(args, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
