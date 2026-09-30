"""Re-post `#assessments-summary` headlines that were never posted.

Repairs the loss described in
docs/audits/2026-08-29-lost-assessment-headlines/README.md: an interview that
ended by the `max_thread_messages` timeout (or by abandonment, or by the run's
own shutdown) held a verdict nobody announced. The assessment rows are intact;
only the public headline is missing.

DRY RUN BY DEFAULT. `--apply` is required to post anything or write anything,
because a headline is a public Slack message that cannot be retracted.

    # see what is owed, post nothing
    python scripts/backfill_assessment_headlines.py --run <uuid>

    # post them
    python scripts/backfill_assessment_headlines.py --run <uuid> --apply

    # post every owed headline, open interviews included, and close the run
    # (a finalized run cannot be resumed)
    python scripts/backfill_assessment_headlines.py --run <uuid> --finalize --apply

    # claims whose post may or may not have reached Slack
    python scripts/backfill_assessment_headlines.py --run <uuid> --list-in-doubt
    # after checking the channel: make them postable again
    python scripts/backfill_assessment_headlines.py --run <uuid> --release-in-doubt <id> [<id> ...]

    # a headline IS already in Slack but the row predates 0041: record that
    # fact without posting a duplicate
    python scripts/backfill_assessment_headlines.py --run <uuid> \\
        --assessment <uuid> --stamp-only --apply

**Claims.** Every post claims its thread first
(`src/services/headline_claims.py`) — the same protocol the engine uses — so this
script and a running engine can never both post one interview. A thread any of
whose rows is already claimed or posted is skipped, and only the NEWEST owed row
of a thread is rendered. A post that raises with no Slack response keeps its
claim and is reported IN DOUBT; it is never re-posted until an operator checks
Slack and runs `--release-in-doubt`.

**Engine lock.** Every write (`--apply`, `--finalize --apply`,
`--release-in-doubt`) holds the engine's advisory lock
(`ENGINE_LOCK_KEY`) for the whole run, so no engine can start or resume while
this script posts, and it is refused (exit 2) while an engine, or another run of
this script, holds it. The lock is released on every exit path and dies with the
connection, so a SIGKILLed engine frees it at once: the status row, which a CLI
engine leaves at `stopping` and a crash leaves at `running`, is no longer
consulted. The dry run and `--list-in-doubt` take no lock. `--run-crashed` is
kept only so old invocations still parse; it overrides nothing.

**Selection follows how the run ended.** A run with `held_at`
(held open interviews) gets headlines only for interviews that ENDED (a
ThreadDecision exists), unless `--finalize`; any other run gets every owed
interview, as before. `--finalize` posts every owed interview and then sets
`simulation_runs.finalized_at` and clears `held_at`; it is allowed on an
already-finalized run, to post the headlines its finalize logged as LOST.

**The headline's band/score come from THIS ROW'S OWN stored
`weighted_score`/`band`** (`render_assessment_headline`'s `score`/`band`
override, added 2026-08-29) — never recomputed from `scores` against
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
# site-packages (the image installs src/ non-editable).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.slack_client import AgentSlackClient
from src.config import get_settings
from src.models import AgentChannel, AgentRegistry, OpportunityAssessment, SimulationRun
from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock
from src.services.assessment_headline import render_assessment_headline
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.headline_claims import (
    claim_row,
    claim_thread,
    list_in_doubt,
    mark_posted,
    release_claim,
)
from src.services.interview_state import ended_thread_ids
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
IN_DOUBT = "in_doubt"      # claimed, the post raised with no Slack response
UNCLAIMED = "unclaimed"    # another poster holds or posted this thread


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
    (``for_stamp_only=False``, the default). Stamping is exempt (2026-08-29):
    it renders nothing and posts nothing, so there is no number that
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
    dry run, an empty selection, or rows another poster holds), 1 if any row
    FAILED or is IN DOUBT."""
    return 1 if any(outcome in (FAILED, IN_DOUBT) for _, _, outcome in results) else 0


def newest_owed_per_thread(rows):
    """Split ``rows`` into (candidates, [(row, why_skipped), ...]) by interview.

    A thread any of whose rows is already posted or claimed is skipped whole —
    the claim would refuse it anyway, and saying so here makes the dry run
    honest. Otherwise only the thread's NEWEST row is a candidate:
    the newest verdict is the interview's verdict. NULL-thread rows cannot be
    grouped and stand alone.
    """
    by_thread: dict[str, list] = {}
    candidates, skipped = [], []
    for row in rows:
        if row.thread_id is None:
            if row.summary_posted_at is None and row.summary_claimed_at is None:
                candidates.append(row)
            else:
                skipped.append((row, "already announced or claimed"))
            continue
        by_thread.setdefault(row.thread_id, []).append(row)
    for thread_rows in by_thread.values():
        if any(r.summary_posted_at is not None or r.summary_claimed_at is not None
               for r in thread_rows):
            skipped.extend(
                (r, "its interview is already announced, or claimed by another poster")
                for r in thread_rows
            )
            continue
        newest = max(thread_rows, key=lambda r: r.created_at)
        candidates.append(newest)
        skipped.extend((r, "an older verdict of the same interview") for r in thread_rows
                       if r is not newest)
    return candidates, skipped


def filter_by_hold(rows, *, held: bool, finalize: bool, ended: set[str]):
    """A held run posts only ENDED interviews unless ``finalize``.
    A row with no thread counts as ended: no interview can still change it, and
    the Phase 2 held count classes it the same way."""
    if not held or finalize:
        return list(rows), []
    kept, skipped = [], []
    for row in rows:
        if row.thread_id is None or row.thread_id in ended:
            kept.append(row)
        else:
            skipped.append((row, "held run: the interview is still open (--finalize releases it)"))
    return kept, skipped


async def acquire_apply_lock(database_url: str) -> SessionAdvisoryLock | None:
    """Hold the engine lock for a whole write run (spec P0-08, Phase 2), so no
    engine can start or resume while this script posts, finalizes or releases
    claims. Returns None when an engine (or another run of this script) holds it."""
    lock = SessionAdvisoryLock(database_url, ENGINE_LOCK_KEY)
    try:
        if await lock.acquire():
            return lock
    except BaseException:
        await lock.release()
        raise
    await lock.release()
    return None


async def post_with_claims(rows_and_texts, *, factory, client_for):
    """Claim, post and settle each ``(row, text)`` in order, each write in its own
    short transaction. Returns ``(row, text, outcome)`` triples."""
    results = []
    for row, text in rows_and_texts:
        async with factory() as db:
            ids = (
                await claim_thread(db, row.simulation_run_id, row.thread_id)
                if row.thread_id is not None
                else await claim_row(db, row.id)
            )
        if not ids:
            results.append((row, text, UNCLAIMED))
            continue
        client = client_for(getattr(row, "agent_id", None))
        if client is None:
            async with factory() as db:
                await release_claim(db, ids)
            results.append((row, text, FAILED))
            continue
        try:
            result = client.post_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
        except Exception:
            # Every row the thread claim took, not only the newest: releasing a
            # subset leaves the thread claimed, and --apply then skips it.
            logger.exception(
                "IN DOUBT: the headline for assessment %s (%s) raised with no Slack "
                "response; its claim is kept — check the channel, then "
                "--release-in-doubt %s if it did not post",
                row.id, getattr(row, "subject_agent_id", "?"),
                " ".join(str(i) for i in ids),
            )
            results.append((row, text, IN_DOUBT))
            continue
        async with factory() as db:
            if result:
                await mark_posted(db, ids)
            else:
                await release_claim(db, ids)
        results.append((row, text, POSTED if result else FAILED))
    return results


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
    in ``src/agent/main.py``:
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
    own ``self._channel_id_map`` (``SimulationEngine._persist_seeded_channels``
    and ``_rebuild_state_from_db``) — used only to resolve a permalink
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
        # Replay the row's OWN stored band/score rather than
        # recomputing from `scores` against whatever rubric is live today —
        # see the module docstring and assessment_headline.py's.
        score=row.weighted_score,
        band=row.band,
        elevator_pitch=row.elevator_pitch,
    )


def _rubric_note(row: OpportunityAssessment, live_version: str, live_hash: str) -> str:
    """A one-line, human-legible statement of which rubric revision this
    row's band/score came from — preview-only. It plays no role in the
    post/stamp decision: the drift gate became advisory rather than
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
    ap.add_argument(
        "--finalize", action="store_true",
        help="Post EVERY owed interview's headline, open interviews of a held run "
             "included, then set simulation_runs.finalized_at and clear held_at: the "
             "run can no longer be resumed. Allowed on an already-finalized run. "
             "Needs --apply to write anything.",
    )
    ap.add_argument(
        "--list-in-doubt", action="store_true",
        help="List claims older than 10 minutes whose post never recorded — the "
             "headline may or may not be in Slack. Writes nothing.",
    )
    ap.add_argument(
        "--release-in-doubt", nargs="+", default=None, metavar="ID",
        help="Clear these in-doubt claims after you have checked the channel and "
             "the headline is NOT there, so a later --apply posts it.",
    )
    ap.add_argument(
        "--run-crashed", action="store_true",
        help="Deprecated no-op, accepted so old invocations still parse. Writes are "
             "gated on the engine lock, which a dead engine releases at once and "
             "which nothing overrides.",
    )
    return ap


async def run_repair(
    args, factory, *, make_client=None, lock_url: str | None = None,
) -> int:
    """The whole script, given parsed ``args`` and a session factory.

    ``make_client(agent_id)`` builds the posting client (tests pass a fake);
    by default a real AgentSlackClient from the agent's stored or env token.
    ``lock_url`` is the database URL the engine lock connects to (default: the
    settings URL). A write mode holds the engine lock for the whole run and
    releases it on every exit path.
    Exit codes: 0 success, 1 a post FAILED or is IN DOUBT, 2 refused (an engine
    holds the lock, or the run is unknown).
    """
    apply_lock = None
    if (args.apply or args.release_in_doubt) and not args.list_in_doubt:
        apply_lock = await acquire_apply_lock(lock_url or get_settings().database_url)
        if apply_lock is None:
            logger.error(
                "refusing to write: a simulation engine holds the engine lock "
                "(stop the engine first; --run-crashed does not override this)"
            )
            return 2
    try:
        return await _repair_body(args, factory, make_client=make_client)
    finally:
        if apply_lock is not None:
            await apply_lock.release()


async def _repair_body(args, factory, *, make_client) -> int:
    run_id = uuid.UUID(args.run)
    assessment_ids = (
        [uuid.UUID(a) for a in args.assessment_ids] if args.assessment_ids else None
    )

    async with factory() as db:
        if args.list_in_doubt:
            rows = await list_in_doubt(db, run_id)
            for row in rows:
                logger.info(
                    "IN DOUBT %s (thread %s, %s): claimed %s, never recorded as posted",
                    row.id, row.thread_id, row.subject_agent_id, row.summary_claimed_at,
                )
            logger.info("%d in-doubt claim(s) for run %s", len(rows), run_id)
            return 0
        run = await db.get(SimulationRun, run_id)
        if run is None:
            logger.error("no simulation run %s", run_id)
            return 2
        if args.release_in_doubt:
            wanted = {uuid.UUID(a) for a in args.release_in_doubt}
            in_doubt = {row.id for row in await list_in_doubt(db, run_id)}
            for rid in sorted(wanted - in_doubt, key=str):
                logger.error("NOT IN DOUBT %s: no claim older than 10 minutes to release", rid)
            releasable = sorted(wanted & in_doubt, key=str)
            await release_claim(db, releasable)
            logger.info("released %d in-doubt claim(s)", len(releasable))
            return 1 if wanted - in_doubt else 0

        rows = await _load_rows(db, run_id, assessment_ids)
        logger.info("found %d assessment row(s) for run %s", len(rows), run_id)
        held = run.held_at is not None
        ended = (
            await ended_thread_ids(db, run_id)
            if held and not args.finalize and not args.stamp_only else set()
        )
        pi_labels = await _load_pi_labels(db)
        channel_id_map = await _load_channel_id_map(db, run_id)
        tokens = await _load_posting_tokens(db) if make_client is None else {}

    clients: dict[str, object | None] = {}

    def _client_for(agent_id: str | None):
        """Lazily build (and cache) one client per AUTHORING agent_id — the
        verdict's author (normally "blackbird"), never the PI it assessed.
        Connecting is read-only (auth.test), so a dry run can show permalinks."""
        if not agent_id:
            return None
        if agent_id in clients:
            return clients[agent_id]
        if make_client is not None:
            client = make_client(agent_id)
        else:
            token = tokens.get(agent_id)
            client = AgentSlackClient(agent_id=agent_id, bot_token=token) if token else None
            if client is not None and not client.connect():
                client = None
        clients[agent_id] = client
        return client

    if args.stamp_only:
        to_stamp, skip_pairs = select_rows_needing_headline(
            rows, live_rubric_hash=RUBRIC_CONTENT_HASH,
            allow_rubric_drift=args.allow_rubric_drift, for_stamp_only=True,
        )
        rows_and_texts = [(row, _render_for(row, pi_labels, None)) for row in to_stamp]
        results = apply_headline_repairs(
            rows_and_texts, client_for=_client_for, apply=args.apply, stamp_only=True,
        )
        if args.apply and any(outcome == STAMPED for _, _, outcome in results):
            async with factory() as db:
                for row, _text, outcome in results:
                    if outcome == STAMPED:
                        await db.execute(
                            update(OpportunityAssessment)
                            .where(OpportunityAssessment.id == row.id)
                            .values(summary_posted_at=row.summary_posted_at)
                        )
                await db.commit()
    else:
        candidates, thread_skips = newest_owed_per_thread(rows)
        candidates, hold_skips = filter_by_hold(
            candidates, held=held, finalize=args.finalize, ended=ended,
        )
        to_post, drift_skips = select_rows_needing_headline(
            candidates, live_rubric_hash=RUBRIC_CONTENT_HASH,
            allow_rubric_drift=args.allow_rubric_drift,
        )
        skip_pairs = [*thread_skips, *hold_skips, *drift_skips]
        rows_and_texts = [
            (row, _render_for(row, pi_labels, _resolve_permalink(
                row, _client_for(row.agent_id), channel_id_map,
            )))
            for row in to_post
        ]
        if args.apply:
            results = await post_with_claims(
                rows_and_texts, factory=factory, client_for=_client_for,
            )
        else:
            results = [(row, text, WOULD_POST) for row, text in rows_and_texts]

    for row, why in skip_pairs:
        logger.info("SKIP %s (%s): %s", row.id, row.subject_agent_id, why)
    tally = {POSTED: 0, STAMPED: 0, FAILED: 0, IN_DOUBT: 0, UNCLAIMED: 0}
    for row, text, outcome in results:
        note = _rubric_note(row, RUBRIC_VERSION, RUBRIC_CONTENT_HASH)
        if outcome in (WOULD_POST, WOULD_STAMP):
            logger.info(
                "%s %s (%s) [%s]: %s",
                "WOULD POST" if outcome == WOULD_POST else "WOULD STAMP",
                row.id, row.subject_agent_id, note, text,
            )
            continue
        tally[outcome] += 1
        log = logger.error if outcome in (FAILED, IN_DOUBT) else logger.info
        log("%s %s (%s): %s", outcome.upper(), row.id, row.subject_agent_id, text)

    if args.finalize and args.apply:
        async with factory() as db:
            await db.execute(
                update(SimulationRun)
                .where(SimulationRun.id == run_id)
                .values(finalized_at=func.coalesce(SimulationRun.finalized_at, func.now()),
                        held_at=None)
            )
            await db.commit()
        logger.info("run %s finalized: it can no longer be resumed", run_id)

    logger.info(
        "done: %d posted, %d stamped, %d skipped, %d unclaimed, %d in doubt, %d failed",
        tally[POSTED], tally[STAMPED], len(skip_pairs), tally[UNCLAIMED],
        tally[IN_DOUBT], tally[FAILED],
    )
    return exit_code_for(results)


async def main() -> int:
    args = _build_arg_parser().parse_args()
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return await run_repair(args, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
