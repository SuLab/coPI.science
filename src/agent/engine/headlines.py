"""The #assessments-summary headline: render and post, record it durably, announce owed headlines, and the bounded shutdown sweep (spec §7.1)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from src.agent.agent import Agent
from src.agent.end_reasons import FINALIZE, HOLD, end_reason_class
from src.agent.engine import constants, deps
from src.agent.engine.constants import ASSESSMENTS_SUMMARY_CHANNEL
from src.agent.engine.context import EngineContext, RunState, VerdictLedgerPort, via
from src.agent.engine.helpers import HEADLINE_IN_DOUBT, _HeadlineInDoubt
from src.agent.state import ThreadState
from src.models import AgentChannel, AgentRegistry, OpportunityAssessment, SimulationRun
from src.services import headline_claims
from src.services.assessment_headline import render_assessment_headline
from src.services.interview_state import ended_thread_ids

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory
    from src.agent.engine.slack_io import SlackIO

logger = logging.getLogger("src.agent.simulation")

ANNOUNCE_TRIGGERS = frozenset({"capture", "thread-close", "shutdown", "finalize"})


def should_announce(
    *, trigger: str, already_announced: bool, terminal: bool = False,
    queued: bool = False, end_class: str | None = None, interview_ended: bool = False,
) -> bool:
    """The one owner of every announce decision (spec §8.2, S1-08).

    * ``capture``: a terminal verdict is announced when captured, except a
      verdict that was only queued (its row does not exist yet, P0-08);
    * ``thread-close``: the owed headline of an interview whose close wrote
      its ``ThreadDecision``;
    * ``shutdown``: the end-reason class decides. TODAY (the default Stop,
      SIGTERM) and FINALIZE announce every owed headline, HOLD only ended
      interviews (``interview_state.ended_thread_ids`` is the one predicate);
    * ``finalize``: Finalize run announces every owed headline.

    An interview already announced is never announced again (the carry-forward:
    a superseded verdict's headline is public and cannot be retracted).
    """
    if trigger not in ANNOUNCE_TRIGGERS:
        raise ValueError(f"unknown announce trigger {trigger!r}")
    if already_announced:
        return False
    if trigger == "capture":
        return terminal and not queued
    if trigger in ("thread-close", "finalize"):
        return True
    if end_class in ("TODAY", FINALIZE):
        return True
    if end_class == HOLD:
        return interview_ended
    raise ValueError(f"unknown end-reason class {end_class!r}")


@dataclass(frozen=True)
class HeadlineInputs:
    """What one ``#assessments-summary`` headline is rendered from. The capture
    path fills it from the verdict dict with ``score``/``band`` None; the owed
    path and Finalize fill it from the stored row with the row's own
    ``weighted_score``/``band``."""

    agent_id: str
    thread_id: str | None
    channel_name: str
    subject_agent_id: str | None
    slack_ts: str | None
    project: Any
    recommendation: Any
    scores: Any
    elevator_pitch: Any
    score: float | None = None
    band: str | None = None


@dataclass
class FinalizeReport:
    posted: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    in_doubt: list[str] = field(default_factory=list)


class HeadlineAnnouncer:
    """Claim, render, post and stamp one headline (spec §8.2 claim protocol).

    Engine-free, so the supervisor's Finalize run routine can post headlines
    without an engine. ``announce`` returns:

    * ``"posted"``: in Slack and stamped (``summary_posted_at``);
    * ``"lost"``: never claimed (no client, not connected, no summary channel)
      or definitely refused by Slack (claim released);
    * ``"in_doubt"``: a transport exception with no Slack response. The claim
      stays, so nothing re-posts it automatically; the operator checks Slack
      and runs ``--release-in-doubt``;
    * ``"skipped"``: the claim found the thread already claimed or posted.

    ``client_for`` and ``summary_channel_id`` may block (the Finalize build
    connects a client with ``auth.test`` and resolves the channel through
    ``conversations.list``, which sleeps on a 429), so ``announce`` runs them in
    a worker thread, off the supervisor's event loop.
    """

    def __init__(
        self, *, session_factory, run_id: uuid.UUID | None,
        client_for: Callable[[str], Any | None],
        summary_channel_id: Callable[[Any], str | None],
        source_channel_id: Callable[[str], str | None],
        pi_label_for: Callable[[str | None], str],
        cap: int | None = None,
    ) -> None:
        self._sf = session_factory
        self._run_id = run_id
        self._client_for = client_for
        self._summary_channel_id = summary_channel_id
        self._source_channel_id = source_channel_id
        self._pi_label_for = pi_label_for
        self._cap = cap

    async def announce(
        self, inputs: HeadlineInputs, *, trigger: str,
        assessment_id: uuid.UUID | None = None,
    ) -> str:
        client = await asyncio.to_thread(self._client_for, inputs.agent_id)
        channel_id = (
            await asyncio.to_thread(self._summary_channel_id, client)
            if client is not None else None
        )
        if not channel_id or not client or not client.is_connected:
            logger.warning(
                "[%s] Skipping #assessments-summary headline for thread %s: "
                "channel_id=%r, transport %s",
                inputs.agent_id, inputs.thread_id, channel_id,
                "missing" if not client else "not connected",
            )
            return "lost"
        ids: list[uuid.UUID] | None = None
        if self._sf is not None and self._run_id is not None:
            async with self._sf() as db:
                if inputs.thread_id is None:
                    ids = (
                        await headline_claims.claim_row(db, assessment_id)
                        if assessment_id else []
                    )
                else:
                    ids = await headline_claims.claim_thread(db, self._run_id, inputs.thread_id)
            if not ids:
                return "skipped"
        permalink = None
        source = self._source_channel_id(inputs.channel_name)
        if source and inputs.slack_ts:
            try:
                permalink = await client.aget_permalink(source, inputs.slack_ts)
            except Exception:
                logger.warning(
                    "[%s] Could not resolve a permalink for thread %s's verdict; "
                    "posting the headline without one",
                    inputs.agent_id, inputs.thread_id, exc_info=True,
                )
        text = render_assessment_headline(
            pi_label=self._pi_label_for(inputs.subject_agent_id),
            project=inputs.project,
            recommendation=inputs.recommendation,
            scores=inputs.scores,
            permalink=permalink,
            score=inputs.score,
            band=inputs.band,
            elevator_pitch=inputs.elevator_pitch,
        )
        try:
            posted = await client.apost_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
        except Exception:
            logger.exception(
                "[%s] IN DOUBT: the #assessments-summary headline for thread %s "
                "(trigger=%s) hit a transport error with no Slack response. Its "
                "claim is kept and it will not be re-posted automatically; check "
                "Slack, then: python scripts/backfill_assessment_headlines.py "
                "--run %s --list-in-doubt (and --release-in-doubt <id> if it is "
                "not there)",
                inputs.agent_id, inputs.thread_id, trigger, self._run_id,
            )
            return "in_doubt"
        if not posted:
            logger.error(
                "[%s] Slack refused the #assessments-summary headline for thread "
                "%s (channel %s / %s): nothing was posted, so the claim is "
                "released and the verdict stays discoverable",
                inputs.agent_id, inputs.thread_id, ASSESSMENTS_SUMMARY_CHANNEL, channel_id,
            )
            if ids:
                async with self._sf() as db:
                    await headline_claims.release_claim(db, ids)
            return "lost"
        logger.info(
            "[%s] Posted #assessments-summary headline for %s (%s)",
            inputs.agent_id, inputs.subject_agent_id or "?", inputs.recommendation,
        )
        if ids:
            async with self._sf() as db:
                await headline_claims.mark_posted(db, ids)
        return "posted"

    async def finalize(self) -> FinalizeReport:
        """Finalize run (B19): announce every owed headline of the run (the
        newest row of each owed thread, then each owed NULL-thread row) up to
        the shutdown cap, log LOST and IN DOUBT, then set ``finalized_at`` and
        clear ``held_at`` even when some headlines could not post (SA4-04)."""
        cap = self._cap if self._cap is not None else constants.HEADLINES_MAX_AT_SHUTDOWN
        report = FinalizeReport()
        async with self._sf() as db:
            rows = (await db.execute(
                select(OpportunityAssessment)
                .where(
                    OpportunityAssessment.simulation_run_id == self._run_id,
                    OpportunityAssessment.summary_posted_at.is_(None),
                    OpportunityAssessment.summary_claimed_at.is_(None),
                )
                .order_by(OpportunityAssessment.created_at.desc())
            )).scalars().all()
        seen: set[str] = set()
        owed: list[OpportunityAssessment] = []
        for row in rows:
            if row.thread_id is None:
                owed.append(row)
            elif row.thread_id not in seen:
                seen.add(row.thread_id)
                owed.append(row)
        for index, row in enumerate(owed):
            key = row.thread_id or str(row.id)
            if index >= cap:
                report.lost.append(key)
                continue
            outcome = await self.announce(
                HeadlineInputs(
                    agent_id=row.agent_id, thread_id=row.thread_id,
                    channel_name=row.channel_name, subject_agent_id=row.subject_agent_id,
                    slack_ts=row.slack_ts, project=row.company_or_project,
                    recommendation=row.recommendation, scores=row.scores or {},
                    elevator_pitch=row.elevator_pitch, score=row.weighted_score, band=row.band,
                ),
                trigger="finalize", assessment_id=row.id,
            )
            if outcome == "posted":
                report.posted.append(key)
            elif outcome == "in_doubt":
                report.in_doubt.append(key)
            elif outcome == "lost":
                report.lost.append(key)
            # "skipped": another poster already holds or posted this thread.
        if report.lost:
            logger.error(
                "LOST %d #assessments-summary headline(s) at Finalize run of %s "
                "(threads/rows: %s). The assessment rows are safe; re-post with: "
                "python scripts/backfill_assessment_headlines.py --run %s --finalize --apply",
                len(report.lost), self._run_id, ", ".join(report.lost), self._run_id,
            )
        async with self._sf() as db:
            await db.execute(
                update(SimulationRun)
                .where(SimulationRun.id == self._run_id)
                .values(finalized_at=func.now(), held_at=None)
            )
            await db.commit()
        logger.info(
            "Finalize run %s: %d posted, %d lost, %d in doubt; finalized_at set",
            self._run_id, len(report.posted), len(report.lost), len(report.in_doubt),
        )
        return report


async def build_finalize_announcer(session_factory, run_id: uuid.UUID) -> HeadlineAnnouncer:
    """The announcer the supervisor's Finalize run routine uses: the posting
    agents' own Slack clients (the hub's, since the hub writes every row),
    tokens resolved as the engine resolves them (DB token when valid, else the
    env token), pi labels from the registry regardless of status, and the run's
    own channel ids for permalinks. With Slack disabled every headline is LOST
    and ``finalized_at`` is still set (SA4-04)."""
    from src.agent.slack_client import AgentSlackClient
    from src.services.slack_tokens import env_token, is_valid_token

    async with session_factory() as db:
        registry = (await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.pi_name, AgentRegistry.slack_bot_token)
        )).all()
        channels = (await db.execute(
            select(AgentChannel.channel_name, AgentChannel.channel_id)
            .where(AgentChannel.simulation_run_id == run_id)
        )).all()
    labels = {r.agent_id: r.pi_name for r in registry}
    tokens = {
        r.agent_id: (r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(r.agent_id))
        for r in registry
    }
    channel_ids = {c.channel_name: c.channel_id for c in channels}
    slack_on = deps.get_settings().slack_enabled is not False
    clients: dict[str, Any | None] = {}

    def client_for(agent_id: str):
        if agent_id not in clients:
            token = tokens.get(agent_id)
            client = None
            if slack_on and is_valid_token(token):
                candidate = AgentSlackClient(agent_id=agent_id, bot_token=token)
                if candidate.connect():
                    client = candidate
            clients[agent_id] = client
        return clients[agent_id]

    return HeadlineAnnouncer(
        session_factory=session_factory,
        run_id=run_id,
        client_for=client_for,
        summary_channel_id=lambda client: client.get_channel_id(ASSESSMENTS_SUMMARY_CHANNEL),
        source_channel_id=channel_ids.get,
        pi_label_for=lambda subject: (labels.get(subject) if subject else None) or subject or "Unknown lab",
    )


class Headlines:
    """The headline queue and everything that posts or records a headline.

    Reaches the verdict store only through ``_ledger`` (``VerdictLedgerPort``),
    which the orchestrator binds right after constructing ``Verdicts``.
    """

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    slack_clients = via("ctx")
    _assessments_summary_channel_id = via("_channel_directory")
    _channel_id_map = via("_channel_directory")
    _end_reason = via("_run_state", "end_reason")
    _lock_lost = via("_run_state", "lock_lost")

    OWNED_STATE: tuple[str, ...] = (
        "_announced",
        "_pending_headlines",
        "_in_doubt_headlines",
        "_unclaimed_headlines",
    )

    def __init__(
        self,
        ctx: EngineContext,
        *,
        slack_io: SlackIO,
        channel_directory: ChannelDirectory,
        run_state: RunState,
    ) -> None:
        self.ctx = ctx
        self._slack_io = slack_io
        self._channel_directory = channel_directory
        self._run_state = run_state
        self._ledger: VerdictLedgerPort | None = None
        # The announce ledger (spec §8.2): interviews whose headline is public
        # (posted, or claimed and so never re-posted automatically). Carries a
        # superseded verdict's announcement forward: the channel keeps the
        # first word and cannot retract it.
        self._announced: set[str] = set()
        # Interviews that ENDED holding a verdict nobody announced. TWO things
        # put a thread here and they are not the same failure: the interview
        # ended without a concluding reply at all, or a concluding reply landed
        # and its own headline post FAILED (`_capture_hub_assessment` leaves
        # `announced` False for exactly that). Queued rather than posted at the
        # close, because `_close_thread` runs holding the thread lock, both
        # agent locks and a reply-lane semaphore slot, and a headline is two
        # Slack round-trips — the same reason the memory events beside this are
        # queued rather than synthesised there (audit finding 1). Drained by
        # `_drain_and_flush` and by `stop()`.
        self._pending_headlines: list[str] = []
        # Threads whose headline post hit a transport error this process
        # (spec P0-08): claimed, not posted, never re-posted automatically.
        # stop() reports the ones its own sweep produced as IN DOUBT.
        self._in_doubt_headlines: list[str] = []
        # Owed headlines whose claim another holder has (an earlier process's
        # in-doubt claim, or the repair script): not posted here, and not LOST
        # either, since `--apply` would skip them too.
        self._unclaimed_headlines: list[str] = []

    def bind_ledger(self, ledger: VerdictLedgerPort) -> None:
        """Wire the verdict store (spec §7.2 rule 2). The orchestrator calls this once,
        right after constructing Verdicts, which itself needs this unit."""
        self._ledger = ledger

    def is_announced(self, thread_id: str) -> bool:
        """Whether ``thread_id``'s interview already produced its headline."""
        return thread_id in self._announced

    def mark_announced(self, thread_id: str) -> None:
        """Record that ``thread_id``'s headline is public (or claimed and so
        never re-posted automatically)."""
        self._announced.add(thread_id)

    def enqueue(self, thread_id: str) -> None:
        """Queue ``thread_id``'s owed headline for the next drain, once (spec §7.2 rule 1).

        QUEUE only: the post happens in ``_drain_pending_headlines``, off the
        caller's locks.
        """
        if thread_id not in self._pending_headlines:
            self._pending_headlines.append(thread_id)

    async def _post_assessment_summary(
        self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None,
        *, score: float | None = None, band: str | None = None,
    ) -> bool | _HeadlineInDoubt:
        """Post a headline-only summary of a concluded interview to the
        assessments-summary channel (design D12/D13/D14/D16). Returns True when
        a headline actually reached Slack — the caller stamps
        `opportunity_assessments.summary_posted_at` on that answer — False when
        nothing was posted, and the falsy ``HEADLINE_IN_DOUBT`` when the post
        raised with no Slack response (it may or may not have landed). That is why the transport's RETURN
        VALUE is checked and not just its exceptions: `post_message` swallows a
        refusal and answers `None` rather than raising (see the check below).
        Called from _capture_hub_assessment right after a verdict is HELD —
        covers both the immediate fail (closes_thread) path and the pass path
        symmetrically, since both funnel through that one call site — and from
        _announce_owed_headline for an interview that ended un-announced.

        Rendering itself is delegated to `render_assessment_headline`
        (`src/services/assessment_headline.py`) so the engine and
        `scripts/backfill_assessment_headlines.py` cannot render differently.

        ``score``/``band``: the STORED values, passed only by
        `_announce_owed_headline`; never read from ``verdict`` — a sidecar dict
        can carry a model-written ``weighted_score``.

        Never raises: a Slack failure here must not affect anything the
        caller already did (the assessment row's persistence, or the reply
        already posted to Slack). Two levels of that, deliberately: the
        permalink lookup has its own inner guard so a link failure DEGRADES
        (design D16 — "(link unavailable)", never a dropped post), while the
        outer one is the last resort for the post itself.

        Only the headline's six fields are ever rendered — PI/lab name,
        project, recommendation, band/score, permalink, and — since
        2026-09-09 — the sidecar's `elevator_pitch` on a second line (design
        D12, widened once). The pitch widening rests on the operator's
        assertion that PIs cannot join the Slack workspace, which no code
        enforces; it is a real risk accepted deliberately, not a free
        extension of the existing policy. The verdict's `rationale`,
        `red_flags`, `gating` and `raw_verdict` are never read here at all,
        which is what keeps this post from saying more than the manager
        read-only detail view already shows staff. Pinned by
        `tests/unit/test_assessments_summary_post.py`'s sentinel test —
        widening this further to interpolate `verdict` wholesale, or to add a
        "why" line, is a content-policy change, not a formatting one.
        """
        try:
            channel_id = self._assessments_summary_channel_id
            client = self.slack_clients.get(agent.agent_id)
            # ``is_connected`` is not redundant with ``channel_id``: with Slack
            # off, ``_ensure_assessments_summary_channel`` still fills that id in
            # with a ``local:`` placeholder, and the transport is then a
            # ``NullTransport`` — which implements the SYNC Transport protocol
            # only and has no ``apost_message``/``aget_permalink`` at all. Without
            # this the DB-only mode would log an AttributeError traceback for
            # every held verdict (swallowed below, but pure noise). Same guard
            # every other outbound call site uses — see ``_post_message``'s
            # ``if client and client.is_connected``.
            if not channel_id or not client or not client.is_connected:
                # Say so. This return used to be silent, which is why nobody
                # noticed that #assessments-summary has exactly one member (the
                # hub bot itself) and that every headline it has ever posted went
                # into an empty room. A skip and a successful post were equally
                # invisible, so neither could be audited.
                logger.warning(
                    "[%s] Skipping #assessments-summary headline for thread %s: "
                    "channel_id=%r, transport %s",
                    agent.agent_id, thread.thread_id, channel_id,
                    "missing" if not client else "not connected",
                )
                return False

            subject_agent_id = thread.other_agent_id
            pi = self.agents.get(subject_agent_id) if subject_agent_id else None
            pi_label = pi.pi_name if pi else (subject_agent_id or "Unknown lab")

            source_channel_id = self._channel_id_map.get(thread.channel)
            permalink = None
            if source_channel_id and slack_ts:
                # Its OWN try, narrower than the whole-method one below: design
                # D16 says a missing permalink degrades to "(link unavailable)"
                # and is "not a dropped post", and a RAISE has to degrade the
                # same way a None does. `get_permalink` only catches
                # `SlackApiError` itself (src/agent/slack_client.py), so a
                # transport-level error — or anything `_call_with_retry` gives
                # up on that is not a rate limit — comes straight out of it.
                # Left in the method-wide try, such a raise would skip the
                # `apost_message` below entirely and lose a verdict's headline
                # over a cosmetic link.
                try:
                    permalink = await client.aget_permalink(source_channel_id, slack_ts)
                except Exception:
                    logger.warning(
                        "[%s] Could not resolve a permalink for thread %s's "
                        "verdict; posting the headline without one",
                        agent.agent_id, thread.thread_id, exc_info=True,
                    )

            text = render_assessment_headline(
                pi_label=pi_label,
                project=verdict.get("company_or_project"),
                recommendation=verdict.get("recommendation"),
                scores=verdict.get("scores"),
                permalink=permalink,
                score=score,
                band=band,
                elevator_pitch=verdict.get("elevator_pitch"),
            )
            try:
                posted = await client.apost_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
            except Exception:
                # No Slack response at all — a timeout, a reset connection. The
                # post may or may not have landed, so this is NOT a definite
                # failure: the claim-aware caller keeps its claim and nothing
                # re-posts it automatically (spec P0-08). A refusal Slack
                # answered arrives as a falsy return instead, below.
                logger.exception(
                    "[%s] The #assessments-summary headline for thread %s hit a "
                    "transport error with no Slack response — IN DOUBT",
                    agent.agent_id, thread.thread_id,
                )
                return HEADLINE_IN_DOUBT
            if not posted:
                # A REFUSED post is not an exception here. `post_message` ends
                # `if not posted: return None` (src/agent/slack_client.py), and
                # `apost_message` is a thin `to_thread` wrapper that forwards
                # it — so `not_in_channel` after a failed autojoin, an archived
                # channel, `invalid_auth` and a chunk failure ALL arrive as a
                # falsy return and nothing else. Discarding that return made
                # this method answer True for a headline that never existed,
                # and the caller then stamped `summary_posted_at` on it —
                # which hides the verdict from the close path, the shutdown
                # sweep AND `scripts/backfill_assessment_headlines.py` (whose
                # `select_rows_needing_headline` skips an already-stamped row),
                # turning the column from "it posted" into "we tried". The
                # script has always honoured this contract (`if not result:` ->
                # no stamp); the engine must agree with it about the same fact.
                #
                # ERROR, not the caller's WARNING: that one says a retry is
                # coming, this one says Slack refused a public post outright.
                logger.error(
                    "[%s] Slack refused the #assessments-summary headline for "
                    "thread %s (channel %s / %s) — nothing was posted, so "
                    "`summary_posted_at` stays NULL and the verdict remains "
                    "discoverable by the shutdown sweep and by "
                    "scripts/backfill_assessment_headlines.py",
                    agent.agent_id, thread.thread_id,
                    ASSESSMENTS_SUMMARY_CHANNEL, channel_id,
                )
                return False
            logger.info(
                "[%s] Posted #assessments-summary headline for %s (%s)",
                agent.agent_id, subject_agent_id or "?",
                verdict.get("recommendation"),
            )
            return True
        except Exception:
            logger.exception(
                "[%s] Failed to post assessments-summary headline for thread %s",
                agent.agent_id, thread.thread_id,
            )
            return False

    async def _claim_headline(self, thread_id: str) -> list[uuid.UUID] | None:
        """Claim this interview's owed rows immediately before posting (P0-08).

        ``None``: there is no database, so nothing to claim — the post goes
        ahead unclaimed, as a DB-less engine always has. ``[]``: another poster
        holds or posted it — do not post. A database error propagates to
        `_post_claimed_headline`, which treats it as a definite failure.
        """
        if not self.session_factory or not self.simulation_run_id:
            return None
        from src.services.headline_claims import claim_thread

        async with self.session_factory() as db:
            return await claim_thread(db, self.simulation_run_id, thread_id)

    async def _release_headline_claim(
        self, thread_id: str, ids: list[uuid.UUID] | None,
    ) -> None:
        """Clear the claim after a DEFINITE failure. Never raises."""
        if not ids:
            return
        from src.services.headline_claims import release_claim

        try:
            async with self.session_factory() as db:
                await release_claim(db, ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not release the headline claim %s for thread %s: %s — it "
                "reads as IN DOUBT after 10 minutes; release it with "
                "scripts/backfill_assessment_headlines.py --release-in-doubt once "
                "you have checked Slack", ids, thread_id, exc,
            )

    async def _post_claimed_headline(
        self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None,
        *, score: float | None = None, band: str | None = None,
    ) -> str:
        """Claim, post and settle one interview's headline (spec P0-08).

        Returns ``"posted"``; ``"failed"`` (definitely not posted, claim
        released so a later path may post it); ``"in_doubt"`` (a transport
        error with no Slack response: the claim is KEPT and nothing re-posts
        it); or ``"unclaimed"`` (another poster already holds or posted it).
        """
        try:
            ids = await self._claim_headline(thread.thread_id)
        except Exception as exc:  # noqa: BLE001 — a failed claim must not post
            # Nothing was claimed and nothing posted: a definite failure, so the
            # close path, the stop sweep or the repair script can still post it.
            logger.warning(
                "[%s] Could not claim the #assessments-summary headline for thread "
                "%s: %s — not posting it now", agent.agent_id, thread.thread_id, exc,
            )
            return "failed"
        if ids == []:
            logger.info(
                "[%s] The #assessments-summary headline for thread %s is already "
                "claimed or posted elsewhere; not posting it",
                agent.agent_id, thread.thread_id,
            )
            return "unclaimed"
        outcome = await self._post_assessment_summary(
            agent, thread, verdict, slack_ts, score=score, band=band,
        )
        if outcome is True:
            await self._mark_summary_posted(thread.thread_id, ids)
            return "posted"
        if outcome is HEADLINE_IN_DOUBT:
            self._in_doubt_headlines.append(thread.thread_id)
            logger.error(
                "[%s] IN DOUBT: the #assessments-summary headline for thread %s "
                "(rows %s) may or may not be in the channel. Its claim is kept and "
                "nothing re-posts it automatically. Check Slack, then: python "
                "scripts/backfill_assessment_headlines.py --run %s --list-in-doubt, "
                "and --release-in-doubt <id>... if it did not post",
                agent.agent_id, thread.thread_id, ids, self.simulation_run_id,
            )
            return "in_doubt"
        await self._release_headline_claim(thread.thread_id, ids)
        return "failed"

    async def announce_captured(
        self, agent: Agent, thread: ThreadState, verdict: dict, slack_ts: str | None,
    ) -> bool:
        """Post a just-captured terminal verdict's headline (``should_announce``
        already said yes) and record it in the ledger. ``in_doubt`` and
        ``unclaimed`` also count as announced: re-posting either could
        duplicate a public post. Only a definite failure (claim released) stays
        owed, for the close path, the shutdown sweep and the repair script."""
        outcome = await self._post_claimed_headline(agent, thread, verdict, slack_ts)
        if outcome == "failed":
            logger.warning(
                "[%s] The #assessments-summary headline for %s did not post; the "
                "verdict is stored and will be retried when the interview ends",
                agent.agent_id, thread.other_agent_id or "?",
            )
            return False
        self.mark_announced(thread.thread_id)
        return outcome == "posted"

    async def _mark_summary_posted(
        self, thread_id: str | None, ids: list[uuid.UUID] | None = None,
    ) -> None:
        """Record durably that this interview's headline is in Slack.

        ``ids`` are the rows this poster claimed (`_claim_headline`, spec
        P0-08): exactly those are stamped. Without ids — no claim was taken,
        which only a DB-less engine does — the thread's owed rows are stamped,
        keyed by THREAD as before: the thread is the stable identity of an
        interview, and the interview is what gets announced once.

        Also patches any copy still queued in `_pending_assessments`, so a row
        that has not landed yet carries the stamp when it does — otherwise the
        flush writes NULL over a headline that is already public.

        Best-effort and never raises: the headline is already in Slack by the
        time this runs. A failure here costs at-most-once across a restart, not
        the post.
        """
        if not thread_id:
            return
        now = deps.datetime.now(UTC)
        self._ledger.patch_pending_summary(thread_id, now)
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import update as sa_update

        from src.services.headline_claims import mark_posted

        try:
            async with self.session_factory() as db:
                if ids:
                    await mark_posted(db, ids)
                else:
                    await db.execute(
                        sa_update(OpportunityAssessment)
                        .where(
                            OpportunityAssessment.simulation_run_id
                            == self.simulation_run_id,
                            OpportunityAssessment.thread_id == thread_id,
                            OpportunityAssessment.summary_posted_at.is_(None),
                        )
                        .values(summary_posted_at=now)
                    )
                    await db.commit()
        except Exception as exc:  # noqa: BLE001 — the headline already posted
            logger.warning(
                "Posted the #assessments-summary headline for thread %s but "
                "could not record it on the row: %s. A restart of this run may "
                "post a second headline for the same interview.",
                thread_id, exc,
            )

    async def _announce_owed_headline(self, thread_id: str, *, trigger: str) -> bool:
        """Post the `#assessments-summary` headline an ENDED interview still owes.

        The completeness half of the invariant in
        docs/audits/2026-08-29-lost-assessment-headlines/README.md §5:
        announcement used to be a side effect of one particular REPLY (terminal
        = ⏸️, or the CONCLUDE ordinal), and an interview that ended any other
        way — the `max_thread_messages` timeout, abandonment, the run's own
        shutdown — dropped its verdict silently.

        TWO different failures arrive here, and this method cannot tell them
        apart — so it deliberately does not try, and neither should its log:

        * the interview ended without a concluding reply at all (the
          message-count parity break of the RCA's §2.2, which this path makes
          non-destructive but does not fix);
        * a concluding reply DID land and was fine, but its own headline post
          failed — `_capture_hub_assessment` records that by leaving
          the ledger unannounced and `summary_posted_at` NULL, precisely so this
          path can pick the verdict up.

        Everything is resolved from the STORED ROW, never from live state, and
        both halves of that matter:

        * the agent that closed the thread is often the PI, not the hub
          (production run 61ccad6d logged the close under `[rothstein]`), so
          `row.agent_id` is the only correct source for whose client posts;
        * a closed thread has already been popped from every agent's
          `active_threads`, and after a restart there is no `ThreadState` at
          all — but `channel_name`, `subject_agent_id` and `slack_ts` are all
          columns, so a faithful one can be rebuilt.

        `summary_posted_at IS NULL` in the predicate picks the row to render;
        the at-most-once guard is the claim (`_claim_headline`, spec §8.2): the
        close path and the shutdown sweep can both name the same thread, and a
        headline is a public post that cannot be retracted.

        Returns True when a headline actually reached Slack. Never raises.
        """
        if self.is_announced(thread_id):
            return False
        if not self.session_factory or not self.simulation_run_id:
            return False

        try:
            async with self.session_factory() as db:
                row = (await db.execute(
                    select(OpportunityAssessment)
                    .where(
                        OpportunityAssessment.simulation_run_id
                        == self.simulation_run_id,
                        OpportunityAssessment.thread_id == thread_id,
                        OpportunityAssessment.summary_posted_at.is_(None),
                    )
                    .order_by(OpportunityAssessment.created_at.desc())
                    .limit(1)
                )).scalars().first()
        except Exception as exc:  # noqa: BLE001 — never cost the caller
            logger.warning(
                "Could not read the verdict owed a headline for thread %s: %s",
                thread_id, exc,
            )
            return False
        if row is None:
            # Not silent. `_drain_pending_headlines` has ALREADY popped this
            # thread id, so a `return False` here is a queue entry that vanishes
            # without a trace — the exact shape of loss this whole path exists
            # to end, and the last instance of it left in the method.
            #
            # The commonest cause is benign-but-worth-seeing: the verdict's
            # first DB write failed and the row is still on
            # `_pending_assessments`, invisible to this SELECT. That one still
            # gets another chance — `stop()` re-derives the queue from the
            # database (`summary_posted_at IS NULL`, falling back to
            # the verdict ledger) AFTER the final `_flush_pending_assessments`.
            # The rest (a row a cleanup removed, a thread that never got one) are
            # for `scripts/backfill_assessment_headlines.py`.
            logger.warning(
                "No un-announced verdict row found for thread %s (trigger=%s), "
                "so no #assessments-summary headline was posted for it. If its "
                "first write failed the row may still be on the retry queue, in "
                "which case the shutdown sweep will re-derive this thread and "
                "try again.",
                thread_id, trigger,
            )
            return False

        agent = self.agents.get(row.agent_id)
        if agent is None:
            logger.warning(
                "Interview %s ended owing a #assessments-summary headline, but "
                "its author %r is not on this roster — re-post with "
                "scripts/backfill_assessment_headlines.py --run %s --apply",
                thread_id, row.agent_id, self.simulation_run_id,
            )
            return False

        thread = ThreadState(
            thread_id=thread_id,
            channel=row.channel_name,
            other_agent_id=row.subject_agent_id or "",
        )
        verdict = {
            "company_or_project": row.company_or_project,
            "recommendation": row.recommendation,
            "scores": row.scores or {},
            "elevator_pitch": row.elevator_pitch,
        }
        # Stored score/band, not a live recomputation: a weights change since
        # the row was written must not re-band a public headline. Claimed
        # immediately before the post (spec P0-08), so the repair script and
        # this path can never both post it.
        outcome = await self._post_claimed_headline(
            agent, thread, verdict, row.slack_ts,
            score=row.weighted_score, band=row.band,
        )
        if outcome != "posted":
            if outcome == "unclaimed":
                self._unclaimed_headlines.append(thread_id)
            if outcome in ("in_doubt", "unclaimed"):
                self.mark_announced(thread_id)
            return False

        self.mark_announced(thread_id)
        # WARNING, not INFO: every rescue means something upstream failed, and
        # both causes are worth an operator's attention. But it reports what it
        # OBSERVED, not a diagnosis. It used to assert the parity break as the
        # single cause ("the hub was locked out of its own CONCLUDE turn"),
        # which is simply false for the other half — a Slack outage that fails
        # the in-turn post lands here too, and an operator reading that line
        # would go hunting a message-count bug that never happened. Naming both
        # costs one clause; naming the wrong one costs an afternoon.
        logger.warning(
            "[%s] RESCUED the #assessments-summary headline for %s (thread %s, "
            "trigger=%s): the interview ended holding a verdict that had never "
            "been announced — either it ended without a concluding reply, or an "
            "earlier headline post for it failed. Announced on the way out. See "
            "docs/audits/2026-08-29-lost-assessment-headlines/.",
            row.agent_id, row.subject_agent_id or "?", thread_id, trigger,
        )
        return True

    async def _drain_pending_headlines(
        self, *, limit: int | None = None, trigger: str = "thread-close",
    ) -> int:
        """Post the headlines `_close_thread` and `stop()` queued.

        Pops BEFORE posting, so a thread that fails cannot spin the queue — the
        durable retry is `scripts/backfill_assessment_headlines.py`, not this
        loop, and `summary_posted_at` makes a re-queue harmless anyway. Never
        raises: this runs from the main loop's `finally` and from `stop()`.

        Returns how many drained thread ids did NOT result in a post. Popping
        without re-queueing means a Slack outage at shutdown empties the queue
        completely, so `stop()`'s LOST tally — which is the only line carrying
        the repair command — would read ZERO for the very failure it exists to
        report if it counted the leftover queue alone. Every falsy answer is
        counted, including `_announce_owed_headline`'s "no un-announced verdict
        row found": that thread was queued, was drained, and got no headline,
        which is exactly what an operator needs told. Each one has already
        logged its own specific cause; this count is the aggregate.
        """
        drained = 0
        unposted = 0
        while self._pending_headlines and (limit is None or drained < limit):
            thread_id = self._pending_headlines.pop(0)
            drained += 1
            try:
                if not await self._announce_owed_headline(thread_id, trigger=trigger):
                    unposted += 1
            except Exception:
                unposted += 1
                logger.exception(
                    "Failed to announce the owed headline for thread %s", thread_id,
                )
        return unposted

    async def _owed_thread_ids_from_db(
        self, end_class: str,
    ) -> tuple[list[str] | None, list[str]]:
        """``(owed, held_open)`` read from the database for the shutdown sweep.

        ``owed`` is None when there is no database or the read failed (the caller
        falls back to the ledger). Under HOLD, ``should_announce`` splits the owed
        threads into those to announce and those held open."""
        owed_thread_ids: list[str] | None = None
        held_open: list[str] = []
        if not (self.session_factory and self.simulation_run_id):
            return None, held_open
        try:
            async with self.session_factory() as db:
                owed_thread_ids = list((await db.execute(
                    select(OpportunityAssessment.thread_id)
                    .where(
                        OpportunityAssessment.simulation_run_id
                        == self.simulation_run_id,
                        OpportunityAssessment.thread_id.is_not(None),
                        OpportunityAssessment.summary_posted_at.is_(None),
                    )
                    .distinct()
                )).scalars().all())
                if end_class == HOLD and owed_thread_ids:
                    ended = await ended_thread_ids(
                        db, self.simulation_run_id, owed_thread_ids,
                    )
                    announce_now = [
                        t for t in owed_thread_ids
                        if should_announce(
                            trigger="shutdown", already_announced=False,
                            end_class=end_class, interview_ended=t in ended,
                        )
                    ]
                    held_open = [t for t in owed_thread_ids if t not in announce_now]
                    owed_thread_ids = announce_now
        except Exception:
            logger.exception(
                "Could not read which interviews owe a "
                "#assessments-summary headline at shutdown; falling "
                "back to the in-memory _assessed_threads map, which "
                "cannot tell a genuinely owed verdict from a "
                "rehydrated one on a resumed run"
            )
            owed_thread_ids = None
            held_open = []
        return owed_thread_ids, held_open

    def _unannounced_thread_ids(self) -> list[str]:
        """Threads holding a verdict whose headline is not public, in insertion order."""
        return [t for t in self._ledger.assessed_thread_ids() if not self.is_announced(t)]

    async def shutdown_sweep(
        self, end_reason: str | None, *, only: set[str] | None = None,
    ) -> None:
        """Announce every owed headline at a stop, decided by the end-reason
        class (``should_announce``, trigger ``shutdown``): ``end_reason`` None
        is the default Stop (TODAY). Never raises.

        ``only`` limits the sweep to those threads: ``stop()``'s second sweep,
        for verdicts and decisions that landed only in its final flush (a
        failure path). Headlines the first sweep left queued past its bound stay
        queued and are not posted by it. After a lost engine lock the sweep posts
        nothing: another writer may hold the lock, so the owed headlines are
        left for the repair script."""
        if self._lock_lost:
            logger.error(
                "The engine lock was lost: the shutdown sweep posts no "
                "#assessments-summary headline, since another engine or the repair "
                "script may hold the lock now. The owed headlines are left for: "
                "python scripts/backfill_assessment_headlines.py --run %s --apply "
                "(ended interviews), or --finalize --apply to release them all",
                self.simulation_run_id,
            )
            return
        end_class = end_reason_class(end_reason or "operator")
        if only is None:
            await self._sweep_owed_headlines_at_shutdown(end_class)
            return
        stashed, self._pending_headlines = self._pending_headlines, []
        try:
            await self._sweep_owed_headlines_at_shutdown(end_class, only=only)
        finally:
            self._pending_headlines.extend(
                t for t in stashed if t not in self._pending_headlines
            )

    async def _sweep_owed_headlines_at_shutdown(
        self, end_class: str, *, only: set[str] | None = None,
    ) -> None:
        """Announce, at shutdown, the headlines the run still owes (spec §7.1: the
        shutdown sweep block of ``stop()``). Runs after the step-1 flushes of
        ``stop()``; never raises (the block's own except logs), so ``stop()``
        always reaches its "Simulation stopping..." line. ``end_class`` is the
        end-reason class ``stop()`` derived (``src/agent/end_reasons.py``);
        ``only``, when given, restricts it to those threads."""
        # Every interview still holding an unannounced verdict is over: the run
        # is ending, so no later turn will ever conclude or supersede it. This
        # is the last chance to honour D12, and it must run AFTER the assessment
        # flush in `stop()` — `_announce_owed_headline` reads the row back from the
        # database, so a verdict still sitting on `_pending_assessments` would
        # be invisible to it. No drain lock is needed here: `start()` awaits
        # `_run_main_loop()` directly in the same coroutine and `stop()` is only
        # awaited (from src/agent/main.py's finally-block) after `start()` has
        # returned, so this sweep can never run concurrently with the main
        # loop's own `_drain_and_flush`. Wrapped like the memory drain in `stop()`:
        # anything escaping here must not skip the "Simulation stopping..."
        # line at the end of `stop()`, which docs/operations/host-and-simulation.md documents as the
        # operator's proof the buffers reached disk.
        try:
            # Seed from a DB query for exactly which interviews still owe a
            # headline, rather than from the verdict ledger alone.
            # `summary_posted_at IS NULL` is the exact, durable definition of
            # "still owed" — the same predicate `_announce_owed_headline`
            # itself reads before posting, so this seed and that guard can
            # never disagree. The in-memory map can disagree with both, in
            # either direction, and each direction costs something real:
            #
            # * an un-announced ledger entry over a row that IS stamped (a repair-script
            #   `--stamp-only` pass against a live run, say) queues an
            #   already-public headline, and on a resume with 25+ prior
            #   verdicts the rehydrated entries sit at the FRONT of the
            #   insertion-ordered dict — so a memory-derived seed can spend the
            #   whole `HEADLINES_MAX_AT_SHUTDOWN` budget on no-op reads and then
            #   report this session's genuinely owed verdicts as LOST;
            # * a thread with no entry at all is invisible to a memory walk,
            #   which is exactly the interview-ending paths that never call
            #   `_close_thread` — the case this sweep exists for.
            #
            # `_rehydrate_assessed_threads` marks a thread announced in the
            # ledger when `summary_posted_at is not None`, which narrows the first bullet but does not close it:
            # rows written before migration `0041` read NULL whether or not a
            # headline ever posted, so a resumed pre-`0041` run rehydrates them
            # all as owed and this query returns them all too. No seed can fix
            # that — the column is the only record there is — which is why
            # the `0041` box in docs/operations/migration-deploy-notes.md makes running the repair procedure a
            # precondition for resuming such a run.
            # Owed interviews a HOLD end keeps back (spec P0-04): no
            # ThreadDecision, so still open. Announced on a resume or a finalize.
            owed_thread_ids, held_open = await self._owed_thread_ids_from_db(end_class)
            if owed_thread_ids is None and end_class == HOLD:
                # Without the database a HOLD cannot tell an ended interview
                # from an open one, and it must never announce an open one: it
                # holds every un-announced verdict instead of falling back.
                held_open = self._unannounced_thread_ids()
                owed_thread_ids = []
                logger.error(
                    "HOLD end with an unreadable owed-headline query: holding all "
                    "%d un-announced verdict(s) rather than risk announcing an "
                    "open interview", len(held_open),
                )
            if owed_thread_ids is None:
                owed_thread_ids = self._unannounced_thread_ids()
            if only is not None:
                owed_thread_ids = [t for t in owed_thread_ids if t in only]
                held_open = [t for t in held_open if t in only]
            for thread_id in owed_thread_ids:
                if self.is_announced(thread_id):
                    continue
                if thread_id not in self._pending_headlines:
                    self._pending_headlines.append(thread_id)
            if self._pending_headlines:
                logger.info(
                    "Announcing %d interview verdict(s) that ended holding an "
                    "un-announced verdict — either the interview ended "
                    "without a concluding reply, or an earlier headline post "
                    "for it failed", len(self._pending_headlines),
                )
            in_doubt_before = len(self._in_doubt_headlines)
            unclaimed_before = len(self._unclaimed_headlines)
            unposted = await self._drain_pending_headlines(
                limit=constants.HEADLINES_MAX_AT_SHUTDOWN, trigger="shutdown",
            )
            in_doubt = self._in_doubt_headlines[in_doubt_before:]
            unclaimed = self._unclaimed_headlines[unclaimed_before:]
            over_budget = list(self._pending_headlines)
            lost_attempted = max(unposted - len(in_doubt) - len(unclaimed), 0)
            repair = (
                f"python scripts/backfill_assessment_headlines.py --run "
                f"{self.simulation_run_id} "
                + ("--finalize --apply" if end_class == FINALIZE else "--apply")
            )
            if over_budget or lost_attempted:
                # Say LOST with the count and the ids, the same way the buffer
                # flushes do: the assessment rows are safe, so this is
                # recoverable, but only by someone who knows it happened. BOTH
                # causes are counted — a thread whose post FAILS is popped and
                # never re-queued, so the overflow alone would read zero for a
                # Slack outage. In-doubt threads are reported separately below.
                logger.error(
                    "LOST %d #assessments-summary headline(s) at shutdown "
                    "(%d attempted and not posted, %d never attempted past the "
                    "%d-headline shutdown bound; threads still queued: %s). The "
                    "assessment rows are safe — re-post with: %s",
                    lost_attempted + len(over_budget),
                    lost_attempted,
                    len(over_budget),
                    constants.HEADLINES_MAX_AT_SHUTDOWN,
                    ", ".join(over_budget) or "none",
                    repair,
                )
            if in_doubt:
                logger.error(
                    "IN DOUBT %d #assessments-summary headline(s) at shutdown "
                    "(threads: %s): a transport error left no Slack response, so "
                    "each may or may not be in the channel. Their claims are kept "
                    "and nothing re-posts them. Check the channel, then: python "
                    "scripts/backfill_assessment_headlines.py --run %s "
                    "--list-in-doubt, and --release-in-doubt <id>... for any that "
                    "did not post",
                    len(in_doubt), ", ".join(in_doubt), self.simulation_run_id,
                )
            if unclaimed:
                logger.error(
                    "CLAIMED ELSEWHERE %d #assessments-summary headline(s) at "
                    "shutdown (threads: %s): another holder's claim (an earlier "
                    "process's in-doubt post, or the repair script) kept this sweep "
                    "from posting them. Check the channel, then: python "
                    "scripts/backfill_assessment_headlines.py --run %s "
                    "--list-in-doubt, and --release-in-doubt <id>... for any that "
                    "did not post",
                    len(unclaimed), ", ".join(unclaimed), self.simulation_run_id,
                )
            if held_open:
                logger.warning(
                    "HELD %d open interview(s)' #assessments-summary headline(s) "
                    "(end reason %s; threads: %s). They post on a resume, or "
                    "release them with: python "
                    "scripts/backfill_assessment_headlines.py --run %s --finalize "
                    "--apply",
                    len(held_open), self._end_reason, ", ".join(held_open),
                    self.simulation_run_id,
                )
        except Exception:
            logger.exception(
                "Shutdown headline sweep failed; any un-announced verdict for "
                "this run can still be repaired with "
                "scripts/backfill_assessment_headlines.py"
            )
