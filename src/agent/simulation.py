"""Turn-based simulation engine — coordinates all agents across all channels."""

import asyncio
import logging
import uuid
from datetime import UTC, timedelta
from typing import Any

from src.agent.agent import Agent
from src.agent.end_reasons import HOLD, TODAY, end_reason_class
from src.agent.engine import deps
from src.agent.engine.channel_directory import ChannelDirectory
from src.agent.engine.context import EngineContext, RunState, _Via
from src.agent.engine.control import Control
from src.agent.engine.headlines import Headlines
from src.agent.engine.llm_log import LlmLog
from src.agent.engine.memory import Memory
from src.agent.engine.panel import Panel
from src.agent.engine.persistence import Persistence
from src.agent.engine.post_lane import PostLane
from src.agent.engine.reply_lane import ReplyLane
from src.agent.engine.roster import Roster
from src.agent.engine.run_announcer import RunAnnouncer
from src.agent.engine.scheduler import Scheduler
from src.agent.engine.slack_io import SlackIO
from src.agent.engine.threads import Threads
from src.agent.engine.verdicts import Verdicts

# isort: off
from src.agent.engine.constants import (  # re-exported: tests and scripts import these from here
    ASSESSMENTS_SUMMARY_CHANNEL as ASSESSMENTS_SUMMARY_CHANNEL,
    CHANNEL_POLL_INTERVAL as CHANNEL_POLL_INTERVAL,
    CONTROL_POLL_INTERVAL as CONTROL_POLL_INTERVAL,
    HEADLINES_MAX_AT_SHUTDOWN as HEADLINES_MAX_AT_SHUTDOWN,
    MEMORY_EVENTS_MAX_AT_SHUTDOWN as MEMORY_EVENTS_MAX_AT_SHUTDOWN,
    PER_ROW_RECOVERY_DEADLINE_S as PER_ROW_RECOVERY_DEADLINE_S,
    PERSIST_UPSERT_CHUNK_ROWS as PERSIST_UPSERT_CHUNK_ROWS,
    POLL_ERROR_LOG_INTERVAL as POLL_ERROR_LOG_INTERVAL,
    PRIOR_THREADS_KEPT_PER_PAIR as PRIOR_THREADS_KEPT_PER_PAIR,
    PROFILES_DIR as PROFILES_DIR,
    PROPOSAL_DRAIN_SETTLE_TICKS as PROPOSAL_DRAIN_SETTLE_TICKS,
    REBUILD_WINDOW_S as REBUILD_WINDOW_S,
    ROSTER_POLL_INTERVAL as ROSTER_POLL_INTERVAL,
    RUN_STATS_UPDATE_INTERVAL as RUN_STATS_UPDATE_INTERVAL,
    SEEDED_CHANNELS as SEEDED_CHANNELS,
    TRUNCATION_NOTICE as TRUNCATION_NOTICE,
    _CALLS_PER_LOG_ROW as _CALLS_PER_LOG_ROW,
    _CHANNEL_KEYWORDS as _CHANNEL_KEYWORDS,
    _ROW_LEVEL_DB_ERRORS as _ROW_LEVEL_DB_ERRORS,
    _UNIVERSAL_CHANNELS as _UNIVERSAL_CHANNELS,
    _UNSET as _UNSET,
)
from src.agent.engine.helpers import (  # re-exported
    HEADLINE_IN_DOUBT as HEADLINE_IN_DOUBT,
    _HeadlineInDoubt as _HeadlineInDoubt,
    _HeldVerdict as _HeldVerdict,
    _restored_slack_ts as _restored_slack_ts,
    _thread_phase_label as _thread_phase_label,
    _visibility_permits as _visibility_permits,
    _was_truncated as _was_truncated,
)
from src.agent.engine.sidecar import (  # re-exported
    _ASSESSMENT_ORPHAN_TAG_RE as _ASSESSMENT_ORPHAN_TAG_RE,
    _ASSESSMENT_RE as _ASSESSMENT_RE,
    _ASSESSMENT_UNCLOSED_RE as _ASSESSMENT_UNCLOSED_RE,
    _DIMENSION_RATIONALE_CHARS as _DIMENSION_RATIONALE_CHARS,
    _HEADLINE_SOFT_LIMIT as _HEADLINE_SOFT_LIMIT,
    _HUB_BULLET_CHARS as _HUB_BULLET_CHARS,
    _HUB_BULLETS_MAX as _HUB_BULLETS_MAX,
    _HUB_BULLETS_MIN as _HUB_BULLETS_MIN,
    _KEY_POINT_BULLET_CHARS as _KEY_POINT_BULLET_CHARS,
    _KEY_POINT_GROUP_BULLETS as _KEY_POINT_GROUP_BULLETS,
    _KEY_POINTS_MAX as _KEY_POINTS_MAX,
    _KEY_POINTS_MIN as _KEY_POINTS_MIN,
    _PITCH_CITATION_RE as _PITCH_CITATION_RE,
    _PITCH_WORD_LIMIT as _PITCH_WORD_LIMIT,
    _PROJECT_SOFT_LIMIT as _PROJECT_SOFT_LIMIT,
    _SIDECAR_FENCE_RE as _SIDECAR_FENCE_RE,
    _VALID_GATING_STATES as _VALID_GATING_STATES,
    _bounded_str as _bounded_str,
    _extract_assessment_json as _extract_assessment_json,
    _extract_json as _extract_json,
    _extract_slack_message as _extract_slack_message,
    _normalize_gating as _normalize_gating,
    _reply_closes_thread as _reply_closes_thread,
    _reply_opens_with_pause as _reply_opens_with_pause,
    _sidecar_has_valid_json_block as _sidecar_has_valid_json_block,
    _str_or_none as _str_or_none,
    _strip_assessment_sidecar as _strip_assessment_sidecar,
    _strip_llm_preamble as _strip_llm_preamble,
    _unfence_sidecar as _unfence_sidecar,
)
# isort: on
from src.agent.message_log import LogEntry, MessageLog, is_panel_note
from src.agent.specialists import (
    domain_flatness_warning,
    signal_mix_report,
)
from src.agent.state import ThreadState
from src.models import (
    AgentMessage,
    LlmCallLog,
    SimulationRun,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE
from src.services.llm import set_call_log_callback

logger = logging.getLogger(__name__)

#: Engine members held by the context objects: name -> (engine attribute, name there).
_CONTEXT_FORWARD: dict[str, tuple[str, str]] = {
    "agents": ("ctx", "agents"),
    "slack_clients": ("ctx", "slack_clients"),
    "message_log": ("ctx", "message_log"),
    "session_factory": ("ctx", "session_factory"),
    "simulation_run_id": ("ctx", "simulation_run_id"),
    "slack_enabled": ("ctx", "slack_enabled"),
    "_agent_locks": ("ctx", "agent_locks"),
    "_thread_locks": ("ctx", "thread_locks"),
    "_running": ("run_state", "running"),
    "_stop_event": ("run_state", "stop_event"),
    "_end_reason": ("run_state", "end_reason"),
    "request_stop": ("run_state", "request_stop"),
}

#: Engine attribute -> unit class, one entry per unit. Each unit that moves out of
#: SimulationEngine is added here and constructed in SimulationEngine.__init__.
_UNIT_CLASSES: dict[str, type] = {
    "persistence": Persistence,
    "channel_directory": ChannelDirectory,
    "memory": Memory,
    "control": Control,
    "llm_log": LlmLog,
    "slack_io": SlackIO,
    "scheduler": Scheduler,
    "panel": Panel,
    "headlines": Headlines,
    "verdicts": Verdicts,
    "roster": Roster,
    "run_announcer": RunAnnouncer,
    "threads": Threads,
    "post_lane": PostLane,
    "reply_lane": ReplyLane,
}

#: Owner-API names two units define under the same name (``headlines.enqueue()``,
#: ``memory.enqueue()``) and owner-API names that were never engine members
#: (the verdict-ledger port, ``bind_ledger``, ``rehydrate``). Nothing reaches them as
#: engine.<name>; callers use engine.<unit>.<name>.
_NOT_FORWARDED = frozenset({
    "enqueue", "bind_ledger", "rehydrate", "held_for", "is_announced", "mark_announced",
    "patch_pending_summary", "unannounced_thread_ids",
    "mark_closed", "restore_prior", "activate_thread",
})


class SimulationEngine:
    """
    Turn-based simulation engine.

    Main loop: poll Slack for PI messages, select agent, run 5-phase turn.
    """

    def __init__(
        self,
        agents: list[Agent],
        slack_clients: dict,  # agent_id -> AgentSlackClient
        max_runtime_minutes: int = 60,
        # 0 = off, matching the --budget CLI default and _turn_eligible's
        # docstring. A nonzero default silently armed the DEPRECATED cumulative
        # cap for every caller that omitted the kwarg (tests, backfill scripts),
        # i.e. it re-created the permanent bench this branch exists to remove.
        # The live throttle is the sliding window (_within_rate_limit).
        budget_cap: int = 0,
        session_factory=None,
        simulation_run_id: uuid.UUID | None = None,
        reset_cursors: bool = False,
        slack_enabled: bool = True,
        fresh_start: bool = False,
        max_proposals: int = 0,
    ):
        self.ctx = EngineContext(
            agents={a.agent_id: a for a in agents},
            slack_clients=slack_clients,
            message_log=MessageLog(),
            session_factory=session_factory,
            simulation_run_id=simulation_run_id,
            slack_enabled=slack_enabled,
        )
        self.run_state = RunState()
        self.persistence = Persistence(self.ctx)
        self.channel_directory = ChannelDirectory(self.ctx)
        self.ctx.channel_id_resolver = self.channel_directory.channel_id_for
        self.memory = Memory(self.ctx)
        self.control = Control(self.ctx, run_state=self.run_state)
        self.llm_log = LlmLog(self.ctx, persistence=self.persistence)
        self.slack_io = SlackIO(
            self.ctx,
            channel_directory=self.channel_directory,
            persistence=self.persistence,
            # Ports (spec §7.2 rule 2), resolved through the facade at call time so
            # a patched engine member is the one called.
            on_thread_gone=lambda thread_id: self._evict_dead_thread(thread_id),
            tag_filter=lambda text, agent: self._strip_disallowed_tags(text, agent),
        )
        self.scheduler = Scheduler(
            self.ctx,
            channel_directory=self.channel_directory,
            max_runtime_minutes=max_runtime_minutes,
            budget_cap=budget_cap,
        )
        self.panel = Panel(self.ctx, slack_io=self.slack_io)
        self.headlines = Headlines(
            self.ctx, slack_io=self.slack_io, channel_directory=self.channel_directory,
            run_state=self.run_state,
        )
        self.verdicts = Verdicts(
            self.ctx, persistence=self.persistence, panel=self.panel, headlines=self.headlines,
        )
        self.headlines.bind_ledger(self.verdicts)
        self.roster = Roster(
            self.ctx, agents=agents, channel_directory=self.channel_directory,
            rejection_counts=lambda: self._post_type_rejections,
        )
        self.message_log.set_bot_name_map(self._bot_name_to_id)
        self.run_announcer = RunAnnouncer(
            self.ctx, slack_io=self.slack_io, channel_directory=self.channel_directory,
            scheduler=self.scheduler,
        )
        self.threads = Threads(
            self.ctx, headlines=self.headlines, memory=self.memory, verdicts=self.verdicts,
        )
        self.post_lane = PostLane(
            self.ctx, scheduler=self.scheduler, roster=self.roster, threads=self.threads,
            slack_io=self.slack_io, llm_log=self.llm_log,
            channel_directory=self.channel_directory, panel=self.panel,
            max_proposals=max_proposals,
        )
        self.reply_lane = ReplyLane(
            self.ctx, run_state=self.run_state, scheduler=self.scheduler,
            post_lane=self.post_lane, threads=self.threads, verdicts=self.verdicts,
            panel=self.panel, slack_io=self.slack_io, channel_directory=self.channel_directory,
        )
        self._reset_cursors = reset_cursors
        # True only for `--fresh`, which has just minted a new run with no
        # agent_messages/agent_channels rows of its own (it deletes nothing —
        # see main._open_fresh_run). Read by `start()`: only a resume runs the
        # hub's reply-less-pitch recovery, and only a fresh run announces
        # itself. Both kinds of start seed the Slack poll cursors past the
        # history already on the transport (see _restore_slack_state).
        self._fresh_start = fresh_start

    # ------------------------------------------------------------------
    # Facade: a member that moved to a unit is still engine.<name>
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward a member that lives on a unit or on ctx/run_state.

        Called only when normal lookup fails, so orchestrator members and
        instance attributes are unaffected.
        """
        target = _FORWARD.get(name)
        if target is None:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        return getattr(object.__getattribute__(self, target[0]), target[1])

    def __setattr__(self, name: str, value: Any) -> None:
        target = _FORWARD.get(name)
        if target is None:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, target[0]), target[1], value)

    def __delattr__(self, name: str) -> None:
        target = _FORWARD.get(name)
        if target is None:
            object.__delattr__(self, name)
        else:
            delattr(object.__getattribute__(self, target[0]), target[1])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the full simulation."""
        self._start_time = deps.datetime.now(UTC)
        self._running = True
        settings = deps.get_settings()

        logger.info(
            "Simulation started. Max runtime: %dm, Budget: %d calls/agent",
            self.max_runtime_minutes, self.budget_cap,
        )

        # Setup
        self._ensure_seeded_channels()
        self._ensure_assessments_summary_channel()
        await self._persist_seeded_channels()
        # The DB is the primary conversation store and the only one a restart
        # restores. Register the persist hook, hydrate the log from the DB, park
        # the Slack poll cursors past the history already on the transport, then
        # reconstruct per-agent state from the log. This whole sequence runs with
        # Slack fully off.
        self.message_log.set_persist_callback(self._enqueue_persist)
        await self._rebuild_state_from_db()
        await self._restore_slack_state()
        await self._rebuild_agent_state()
        # AFTER _rebuild_agent_state, not before: `_HeldVerdict.final` is derived
        # from `_closed_thread_ids`, which the rebuild above populates from
        # `thread_decisions`. Rehydrating first would mark every restored verdict
        # non-final — including the ones whose interview is already over. See
        # `_rehydrate_assessed_threads`.
        await self._rehydrate_assessed_threads()
        await self._rehydrate_proposal_count()
        set_call_log_callback(self._on_llm_call)

        # Compute the cohort gate BEFORE the first turn. The rebuild above is
        # deliberately gate-blind (it populates the log and state that every agent
        # shares), so on a resumed run this is where cross-cohort threads inherited
        # from the previous process get grandfathered and stale banked posts get
        # pruned. The loop's roster sync would also reach it (_last_roster_poll
        # starts at 0.0), but doing it here means no turn can ever run with an
        # unset gate while isolation is on. See specs/cohort-system-v2.md §8.
        await self._recompute_allowed_sender_ids()
        # Fail fast: a cohort layout that isn't star-shaped ({lab, hub} per lab,
        # no lab-to-lab cohort) makes the hub-and-spoke design unrunnable — a lab
        # that can reach another lab directly, or can't reach the hub at all, has
        # no way to land a pitch. Only the startup path raises; a mid-run
        # recompute (roster sync) logs instead — see
        # _recompute_allowed_sender_ids's call sites.
        violations = self._validate_star_topology()
        if violations:
            raise RuntimeError(
                "Star-topology validation failed: " + "; ".join(violations)
            )
        # AFTER the gate, never before: the filter inside reads
        # agent.allowed_sender_ids, which is None until the line above runs.
        self.refresh_lab_directories()
        # Record which topology this run actually started with, so the run's output
        # stays attributable to its configuration (v2 §13.1).
        await self._record_topology_snapshot()

        # Resume only: recover lab pitches the previous process left without a
        # reply because it stopped in the tick they were posted. Placed AFTER the
        # cohort gate and the star-topology validation above, so the one-shot
        # applies the same gates Phase 3 does. A fresh start has nothing to
        # recover, and its hub keeps subscribing at its first post-lane turn.
        if not self._fresh_start:
            await self._recover_reply_less_pitches()

        # Announce the run boundary in Slack — fresh runs only (a resume is
        # not a new experiment), and only after validation so a run that
        # fails startup is never announced. Best-effort: see
        # _announce_run_start.
        if self._fresh_start:
            await self._announce_run_start()

        # NOTE: every agent's staleness clock is anchored at CONSTRUCTION, by
        # `AgentState` itself — see the comment on that field. This used to be a
        # one-shot loop here, which covered the startup roster and nothing else:
        # `_sync_roster_from_db`'s add path builds an `Agent(...)` mid-run and
        # never reached it, so a mid-run addition monopolised the scheduler.
        # Do not re-add the loop; it would imply the anchor is a startup concern,
        # which is the belief that let the roster-add path ship without one.

        await self._run_main_loop()

    async def _recover_reply_less_pitches(self) -> None:
        """Resume-only one-shot: open interviews on lab pitches a stop orphaned.

        A lab can still pitch in the tick a Stop lands, and on the resume the
        hub's Phase-3 scan starts from the rebuilt ``last_seen_cursor`` — already
        past that pitch — so it would never be interviewed. For each hub this
        runs Phase 1 (its ``subscribed_channels`` is empty at startup) and then
        the Phase-3 auto-activation over the run's last ``REBUILD_WINDOW_S``,
        with every Phase-3 gate.

        ``last_seen_cursor`` is left at its rebuilt value on purpose:
        ``_pending_reply_pairs`` reads it, and moving it back would make the hub
        reply again to threads it has already answered. Never raises: a failure
        here must not abort startup.
        """
        since = deps.time.time() - REBUILD_WINDOW_S
        for hub in [a for a in self.agents.values() if a.role == "scout_hub"]:
            try:
                await self._phase1_channel_discovery(hub)
                activated = self._auto_activate_lab_posts(hub, since=since)
                if activated:
                    logger.info(
                        "[%s] Resume: activated %d reply-less lab pitch(es) from the "
                        "run's last %d days",
                        hub.agent_id, activated, REBUILD_WINDOW_S // 86400,
                    )
            except Exception:
                logger.exception(
                    "[%s] Resume: recovering reply-less lab pitches failed; startup "
                    "continues", hub.agent_id,
                )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    @staticmethod
    def _idle_backoff(streak: int) -> int:
        """Seconds to wait after ``streak`` consecutive unproductive ticks.

        One definition shared by the three places that back off — the
        no-eligible-agent stall, the back-to-back-caller skip, and the idle turn
        — so a tick that does no work always costs the same wall time whichever
        way it came up empty.
        """
        if streak <= 3:
            return 5
        if streak <= 10:
            return 15
        return 30

    def _terminal_stall_reason(self) -> str | None:
        """Why an empty selection should END the run — or None when it is transient.

        ``_select_agent()`` returning None used to break the loop unconditionally.
        Under the sliding-window limiter that is wrong and actively dangerous:
        both remaining live gates LAPSE WITH TIME. ``_within_rate_limit`` expires
        entries as the window slides, and the per-agent ``turn_delay_seconds``
        cooldown expires by the clock. Breaking on either turns "one agent is
        benched for a while" into "the container exits and stays exited" — a
        strictly worse failure than the one this branch was written to fix, and
        one that bites every roster small enough for aggregate demand to reach
        aggregate allowance (e.g. 7 token-holding agents at 8 calls/600s).

        Only two conditions can never recover on their own:

        - an EMPTY ROSTER — nothing will ever become eligible;
        - the LEGACY cumulative ``--budget`` cap, armed (> 0) and blown by EVERY
          agent. ``api_call_count`` only ever increases within a process, and
          ``_rebuild_state_from_db`` restores it across restarts, so this one
          really is permanent. It is also opt-in and deprecated (design §6).

        ``max_runtime`` and SIGTERM still end the run through the loop condition;
        this predicate is only about the selection stall.
        """
        if not self.agents:
            return "the roster is empty"
        if self.budget_cap > 0 and all(
            not self._agent_within_budget(a) for a in self.agents.values()
        ):
            return (
                f"every agent is over the legacy --budget cap ({self.budget_cap})"
            )
        return None

    async def _run_main_loop(self) -> None:
        """Poll inbound sources, select an agent, run its turn — until stopped.

        Split out of ``start()`` so the scheduling contract (in particular
        ``_terminal_stall_reason``) is reachable from a unit test without
        standing up the whole startup sequence.
        """
        turn_count = 0
        stalled = False
        consecutive_idle = 0
        while self._running and self.is_within_time_limit and not self._proposal_target_drained():
            # EVERY exit from this iteration runs `_drain_and_flush`, which is
            # why the whole body sits in a try/finally rather than ending with
            # the four calls. The drain and the three flushes used to be the
            # last statements in the body, and the two `continue`s in the
            # no-eligible-agent branch below jumped straight over all four —
            # the common case for a throttled roster and for a reply-only hub.
            # Harness result over 5 productive ticks:
            # {'flush_persisted': 0, 'flush_llm': 0, 'flush_assess': 0,
            # 'drain': 0}, with 5 rows stranded in each buffer and the
            # documented exit path a `docker stop` that can end in SIGKILL.
            # `finally` also covers the terminal-stall `break` and an
            # exception escaping the body, neither of which the old bottom-of-
            # loop placement reached either.
            try:
                # Poll Slack for other bots' channel messages, mirroring them into
                # the log. No-ops when Slack is off (NullTransport / no connected
                # clients).
                await self._poll_slack_for_bot_messages()

                # Pick up active/inactive flips (and newly-provisioned tokens) from
                # the DB so the roster changes live, without a process restart.
                await self._sync_roster_from_db()

                # Operator control plane: claim a pending stop command and
                # refresh the heartbeat row, so /admin/simulation can see the
                # run is alive and stop it without touching the container.
                # Own cadence gate inside (_last_control_poll), same pattern
                # as _sync_roster_from_db above.
                await self._poll_control_plane(deps.time.time())

                # Pick up profile edits made from the web app (separate process).
                self._sync_profiles_from_disk()

                # Reply lane: service every (agent, thread) pair owing a reply,
                # every tick, with no pacing at all — before the post lane's
                # weighted draw runs. See docs/specs/2026-08-14-two-lane-
                # concurrent-scheduler-design.md §2.1.
                #
                # The blanket try/except that used to wrap this whole call is
                # gone — `_dispatch_reply_lane` now isolates both one pair's
                # servicing failure AND one agent's Phase-3-activation failure
                # from their siblings; what is left unguarded is a genuine failure in
                # pair *selection* itself, which is a real bug that should
                # surface rather than repeat one swallowed ERROR per tick forever
                # while no interview progresses.
                #
                # "Did work" for the backoff below must reflect actual SPEND, not
                # attempts: `_dispatch_reply_lane`'s
                # return counts pairs ATTEMPTED, including ones the reservation
                # limiter deferred with zero LLM calls and `has_pending_reply`
                # left True — so the identical pair recurs every tick. Driving
                # the backoff off the attempt count alone spun the main loop at
                # native tick speed (measured ~2,800 iterations/s) whenever a
                # pending pair was rate-limited: never sleeping, never yielding
                # to _flush_persisted/_flush_llm_logs/_flush_pending_assessments,
                # and hammering the per-tick Slack poll and roster sync every
                # iteration. Comparing the
                # roster's total `api_call_count` across the call — mirroring
                # what `_run_post_turn` already does with `api_calls_before` —
                # answers "did anything actually get spent", not "was anything
                # attempted".
                calls_before_reply_lane = sum(
                    a.api_call_count for a in self.agents.values()
                )
                reply_lane_count = await self._dispatch_reply_lane()
                if reply_lane_count:
                    logger.debug(
                        "[reply-lane] serviced %d pair(s) this tick", reply_lane_count
                    )
                reply_lane_did_work = (
                    sum(a.api_call_count for a in self.agents.values())
                    > calls_before_reply_lane
                )

                # Select agent (post lane — paced, one at a time)
                agent = self._select_agent()
                if agent is None:
                    # No agent is currently eligible. Throttling and the per-agent
                    # cooldown both lapse with time, so this is normally TRANSIENT:
                    # back off and retry rather than ending the run. Only
                    # _terminal_stall_reason's two permanent cases stop the loop.
                    reason = self._terminal_stall_reason()
                    if reason is not None:
                        logger.info("No eligible agent: %s. Stopping.", reason)
                        # Recorded after the loop, not here: request_stop() clears
                        # _running, which would skip this tick's memory drain in
                        # the finally below.
                        stalled = True
                        break
                    if reply_lane_did_work:
                        # The reply lane made a real LLM call this tick even
                        # though no post-lane agent was eligible right now. This
                        # is not an idle tick — sleeping the idle backoff here
                        # would pace the "unpaced" lane, delaying the next reply
                        # sweep by up to 30s.
                        consecutive_idle = 0
                        continue
                    consecutive_idle += 1
                    delay = self._idle_backoff(consecutive_idle)
                    logger.info(
                        "No eligible agent (all throttled or cooling down) — "
                        "retrying in %ds. Transient: the rate window slides and "
                        "per-agent cooldowns expire. (stall streak: %d)",
                        delay, consecutive_idle,
                    )
                    # The sleep is what keeps this a backoff rather than a hot spin,
                    # and it returns early on SIGTERM so shutdown stays prompt.
                    await self._sleep(delay)
                    continue

                logger.info("=== Turn %d: %s ===", turn_count + 1, agent.agent_id)

                # Run the post-lane turn (Phase 1 + Phase 5)
                did_work = False
                try:
                    did_work = await self._run_post_turn(agent)
                except Exception:
                    logger.exception("Error during turn for %s", agent.agent_id)

                agent.state.last_selected = deps.time.time()
                turn_count += 1

                # Idle backoff: if no LLM calls were made in EITHER lane this
                # tick, delay before next turn. Reply-lane SPEND counts too —
                # the hub in particular
                # has no post_types at all, so `did_work` alone is false on
                # nearly every one of its ticks, and gating solely on it would
                # pace the reply lane behind a 30s idle-backoff ceiling on every
                # tick it only replied. Gating on ATTEMPTS rather than spend spun
                # the loop instead — see the comment above `reply_lane_did_work`.
                if did_work or reply_lane_did_work:
                    consecutive_idle = 0
                else:
                    consecutive_idle += 1

                if consecutive_idle > 0:
                    delay = self._idle_backoff(consecutive_idle)
                    logger.debug("Idle backoff: %ds (idle streak: %d)", delay, consecutive_idle)
                    await self._sleep(delay)
                # turn_delay_seconds is NOT slept on here. It is a *per-agent* tempo
                # throttle, enforced at selection time in _turn_eligible: the agent that
                # just ran becomes ineligible for the delay while every other agent
                # stays selectable. Sleeping the loop instead stalled Slack polling, DB
                # ingestion and every other agent for one agent's cooldown.
                # See specs/cohort-system-v2.md §10.3.
            except Exception:
                # An exception escaping the body ends the run as a HOLD (spec
                # P0-04): its open interviews' headlines wait for a resume.
                self.request_stop("exception")
                raise
            finally:
                try:
                    await self._drain_and_flush()
                except Exception:
                    # Name the real cause: without this, main records the
                    # escaping error as `start_failed`.
                    self.request_stop("exception")
                    raise

        if self._running:
            # Nothing asked the loop to stop. A terminal stall is a failure end
            # (a HOLD). Otherwise the loop condition ended the run: max runtime
            # or a drained proposal target — a natural end, which finalizes it
            # (spec P0-04, B19). `_proposal_target_drained()` mutates its streak,
            # so it is not called again to tell them apart. An operator Stop that
            # landed in the same tick as a stall keeps the operator's choice.
            if stalled:
                self.request_stop("stall")
            else:
                self.request_stop(
                    "time_limit" if not self.is_within_time_limit else "target_drained"
                )
        logger.info(
            "Main loop exited after %d turns (end reason: %s)", turn_count, self._end_reason,
        )

    async def _drain_and_flush(self) -> None:
        """The per-tick durability step: drain queued memory work, then flush.

        Hoisted out of the bottom of ``_run_main_loop``'s body so that every
        exit from an iteration reaches it — see the comment at the top of that
        loop for what the two ``continue`` statements used to skip.

        Ordering is the pre-existing one and is load-bearing: the memory drain
        makes real LLM calls, so it runs BEFORE the flushes and this tick's
        ``llm_call_logs`` rows land in this tick's flush rather than the next
        one's.

        One deliberate behaviour change comes with the hoist: the drain now also
        fires on ticks where the selector returned None because everything was
        throttled. That is defensible rather than accidental — the events are
        already queued (``_close_thread`` put them there), each is a real billed
        call that is booked through ``Agent.record_api_call`` like any other,
        and the alternative is a queue that only advances on ticks the scheduler
        happened to find work for. It does mean a fully-throttled roster still
        spends on memory synthesis; that spend is bounded by the queue, not by
        the tick rate.

        ``and self._running`` on the drain is NOT defensive tidiness. This drain
        is UNBOUNDED, while ``stop()`` deliberately bounds its own with
        ``MEMORY_EVENTS_MAX_AT_SHUTDOWN`` because "each is a real LLM call and
        the stop grace period is finite". Hoisting into the ``finally`` put the
        unbounded one on a path a SIGTERM reaches: ``docker stop -t 420`` lands
        while the loop sits in the idle backoff, ``request_stop()`` clears
        ``_running`` and wakes ``_sleep`` early, the branch ``continue``s — and
        without this guard the ``finally`` then spends N x 10-40 s of real LLM
        calls before the ``while`` condition is even re-tested. Nothing is lost
        by skipping it: ``stop()``'s bounded drain runs moments later, on the
        same queue. The FLUSHES still run — they are cheap DB writes, and
        skipping them is the data loss this method exists to prevent.
        """
        if self._pending_memory_events and self._running:
            await self._drain_memory_events()

        # Flush buffered message-log entries + LLM logs + any assessment
        # rows that failed their first write.
        await self._flush_persisted()
        if self._llm_log_buffer:
            await self._flush_llm_logs()
        if self._pending_assessments:
            await self._flush_pending_assessments()

        # AFTER the flushes, never before: a verdict that failed its first write
        # is on `_pending_assessments`, and `_announce_owed_headline` reads the
        # row back from the database. Draining first would find nothing and
        # silently skip the interview this whole path exists for.
        #
        # Deliberately NOT gated on `self._running`, unlike the memory drain
        # above: a Slack post is not an LLM call, the queue is short, and the
        # tick where `_running` has just been cleared is exactly when a closing
        # interview most needs it.
        if self._pending_headlines:
            await self._drain_pending_headlines()

    async def _sleep(self, delay: float) -> None:
        """Sleep for ``delay`` seconds, returning early once a stop is requested.

        The idle backoff sleeps up to 30 s; a plain ``asyncio.sleep`` there would
        burn most of the container's (default 10 s) stop grace period before the
        loop noticed SIGTERM, and the final flush would never run (R2).
        """
        if self._stop_event.is_set():
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def stop(self) -> None:
        """Stop the simulation and durably flush everything still buffered.

        Awaited from the entry point's finally-block so a graceful shutdown
        cannot lose the in-flight turn's messages. Idempotent.

        The headline sweep follows the recorded end-reason class
        (`src/agent/end_reasons.py`): TODAY and FINALIZE announce every owed
        headline (capped at ``HEADLINES_MAX_AT_SHUTDOWN``), HOLD only those of
        ended interviews; HOLD sets ``held_at`` and FINALIZE ``finalized_at``.
        """
        self._running = False
        self._stop_event.set()
        # Which shutdown sweep this is (spec P0-04, src/agent/end_reasons.py).
        # Nothing recorded — a direct stop() — behaves exactly as TODAY.
        end_class = end_reason_class(self._end_reason) if self._end_reason else TODAY
        # Drain a BOUNDED number of queued memory updates BEFORE the log
        # callback is cleared, so their llm_call_logs rows are captured by
        # the flush below. Bounded: each is a real LLM call and the stop
        # grace period is finite — the remainder is dropped loudly rather
        # than racing the SIGKILL.
        try:
            await self._drain_memory_events(limit=MEMORY_EVENTS_MAX_AT_SHUTDOWN)
        except Exception:
            logger.exception("Shutdown memory drain failed")
        if self._pending_memory_events:
            logger.warning(
                "Dropping %d queued working-memory update(s) at shutdown",
                len(self._pending_memory_events),
            )
            self._pending_memory_events.clear()
        set_call_log_callback(None)
        # Await every flush task `_on_llm_call` spawned. Placement is exact:
        #
        # - AFTER `set_call_log_callback(None)` and after the memory drain
        #   above, because the drain makes real LLM calls and so can spawn a
        #   NEW flush task; gathering at the top of `stop()` would leave that
        #   one orphaned, which is the bug this fixes.
        # - BEFORE the final `_flush_llm_logs()`, so a batch that a gathered
        #   task failed on and re-queued gets one more attempt here rather than
        #   sitting in the buffer while the process exits.
        # - `return_exceptions=True` because a CANCELLED task re-raises out of
        #   `gather`, which would abort `stop()` before
        #   `_flush_pending_assessments` — trading one lost buffer for two.
        #   Failures are already reported by `_on_flush_done`.
        pending = [t for t in self._flush_tasks if not t.done()]
        if pending:
            logger.info("Awaiting %d in-flight LLM log flush(es)", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
        # `final=True`: this is the LAST attempt at each buffer. Nothing drains
        # them after `stop()` returns, so a failure here must say LOST with the
        # row count rather than the "re-queued for retry" the per-tick path says.
        await self._flush_persisted(force_stats=True, final=True)
        await self._flush_llm_logs(final=True)
        await self._flush_pending_assessments(final=True)

        # Every interview still holding an unannounced verdict is over; announce it
        # now, after the final assessment flush above (Headlines owns the sweep).
        await self._sweep_owed_headlines_at_shutdown(end_class)
        # AFTER the sweep, whatever it managed (spec P0-04, SA4-04): a finalize
        # stands even when headlines could not post — they were logged LOST with
        # the --finalize repair command above.
        await self._record_run_end_state(end_class)

        # The clear-rate FLOOR was retired 2026-08-28. It asserted that a low
        # `clear` share meant the panel could not discriminate; a 48-consult
        # positive control falsified that (blocking 87.5% -> 0% across a
        # quality ladder, p = 5.1e-07), and the floor sat ABOVE the rate a
        # correct panel produces on this population. There is no replacement
        # threshold on the run-level mix: the optimal operating point for a
        # screen is a likelihood ratio, which depends on the population's base
        # rate, so a fixed floor on the output rate is the wrong shape of
        # constraint. `signal_mix_report` REPORTS the mix (INFO — it is not a
        # problem) rather than diagnosing one; `domain_flatness_warning` is the
        # one thing that still stays quiet for a healthy panel, and is worded
        # as a prompt to measure, never a verdict. See
        # docs/audits/2026-08-27-consult-persona-calibration/.
        #
        # `specialist_consults` can hold MORE rows for the run than either
        # tally: a TRUNCATED consult is recorded durably but never tallied
        # (tools.py — an unread specialist has cleared nothing, and the floor
        # refuses the row too). So a report-vs-table mismatch like ee419dd3's
        # 228-vs-229 is by design, not a lost count — the mix message says
        # "counted consults" for exactly this reason.
        #
        # A DEFAULTED consult is tallied, but under
        # `specialists.DEFAULTED_TALLY_LABEL` rather than under the label it
        # defaulted to, and both functions below exclude that bucket from the
        # mix and report it separately. Otherwise a parse failure is
        # indistinguishable from a specialist saying `gap`, which let the
        # flatness alarm accuse a persona for a broken output shape and go quiet
        # for a domain that had stopped being readable.
        mix = signal_mix_report(self._consult_signal_counts)
        if mix:
            logger.info("%s", mix)
        for line in domain_flatness_warning(self._consult_signal_counts_by_domain):
            logger.warning("%s", line)

        logger.info("Simulation stopping...")

    async def _rehydrate_assessed_threads(self) -> None:
        """Rehydrate the verdict ledger on a resume (``Verdicts.rehydrate``).

        Here rather than on ``Verdicts`` because that unit may not read the thread
        unit's closed set (spec §7.2 rule 2); ``start()`` calls this after
        ``_rebuild_agent_state`` has loaded the set.
        """
        await self.verdicts.rehydrate(self._closed_thread_ids)

    async def _record_run_end_state(self, end_class: str) -> None:
        """Stamp how the run ended on its row (spec P0-04): HOLD sets
        ``held_at``; FINALIZE sets ``finalized_at`` and clears ``held_at`` (a
        finalized run cannot be resumed); TODAY writes nothing. Never raises."""
        if end_class == TODAY or not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import func as sa_func
        from sqlalchemy import update as sa_update

        values = (
            {"held_at": sa_func.now()} if end_class == HOLD
            else {"finalized_at": sa_func.now(), "held_at": None}
        )
        try:
            async with self.session_factory() as db:
                await db.execute(
                    sa_update(SimulationRun)
                    .where(SimulationRun.id == self.simulation_run_id)
                    .values(**values)
                )
                await db.commit()
            logger.info(
                "Run %s ended %s (end reason %s)",
                self.simulation_run_id, end_class, self._end_reason,
            )
        except Exception:
            logger.exception(
                "Could not record run %s's end state (%s, end reason %s)",
                self.simulation_run_id, end_class, self._end_reason,
            )

    # ------------------------------------------------------------------
    # Agent selection (weighted random)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Slack Polling (PI messages)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Message posting
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    async def _rebuild_state_from_db(self) -> None:
        """Hydrate the MessageLog from agent_messages — the primary store.

        Loads message bodies (available since migration 0019) via the
        callback-bypassing path so restored rows aren't re-persisted. Seeds the
        mint_ts high-water mark and, for rows that were mirrored to Slack, the
        Slack poll cursors.
        See specs/local-db-conversations.md.
        """
        if not self.session_factory or not self.simulation_run_id:
            logger.info("No DB session — skipping DB rebuild")
            return
        from sqlalchemy import func as sa_func
        from sqlalchemy import or_
        from sqlalchemy import select as sa_select
        # Bound the load (B2): recent messages, plus the full history of any
        # thread that has no ThreadDecision (still undecided/active). Active-thread
        # reconstruction only needs undecided threads; old closed-thread bodies
        # would just bloat RAM and startup. Nothing in the engine reopens an old
        # closed thread since 23da58d.
        recent_floor = deps.time.time() - REBUILD_WINDOW_S
        closed_thread_ids_subq = sa_select(ThreadDecision.thread_id).where(
            ThreadDecision.simulation_run_id == self.simulation_run_id
        )
        try:
            async with self.session_factory() as db:
                result = await db.execute(
                    sa_select(AgentMessage)
                    .where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        or_(
                            AgentMessage.posted_at > recent_floor,
                            sa_func.coalesce(
                                AgentMessage.thread_ts, AgentMessage.message_ts
                            ).notin_(closed_thread_ids_subq),
                        ),
                    )
                    .order_by(AgentMessage.posted_at.asc(), AgentMessage.created_at.asc())
                )
                rows = result.scalars().all()
        except Exception as exc:
            logger.warning("DB rebuild failed: %s", exc)
            return

        loaded = 0
        max_posted = 0.0
        for r in rows:
            # Pre-0019 rows carry only metadata (empty body): skip them. They
            # hold no conversational signal, and if Slack is on the reconcile
            # pass re-adds them with content. Stage 7 backfills legacy content.
            if not r.content or not r.message_ts:
                continue
            entry = LogEntry(
                ts=r.message_ts,
                channel=r.channel_name,
                sender_agent_id=r.agent_id,
                sender_name=r.sender_name or "",
                content=r.content,
                thread_ts=r.thread_ts,
                posted_at=r.posted_at or 0.0,
                is_bot=r.is_bot,
                visibility=r.visibility,
                # Restore the Slack mirror mapping, not just the content: a reply
                # posted after this restart needs the root's Slack ts to thread
                # on, and its absence is how a DB-origin thread is recognised.
                slack_ts=_restored_slack_ts(r),
                slack_channel_id=r.slack_channel_id,
                slack_thread_ts=r.slack_thread_ts,
                # Restore the KIND too, or a panel note comes back from the DB
                # as an ordinary reply and re-enters every agent-facing read
                # the moment the process restarts — thread histories, message
                # counts, the other party's reply trigger. The exclusions are
                # only as durable as this line.
                phase=r.phase,
            )
            self.message_log.load_entry(entry)
            loaded += 1
            if entry.posted_at > max_posted:
                max_posted = entry.posted_at
            # Advance the Slack poll cursor past every stored mirrored message.
            if r.slack_ts and r.slack_channel_id:
                cur = self._poll_cursors.get(r.slack_channel_id, "0")
                if r.slack_ts > cur:
                    self._poll_cursors[r.slack_channel_id] = r.slack_ts
        self._ts_minter.seed_floor(max_posted)
        logger.info("Rebuilt MessageLog from DB: %d messages", loaded)

    async def _restore_slack_state(self) -> None:
        """Park the Slack poll cursors past the history already on the transport.

        Every start does this — fresh and resumed alike. A resume used to
        reconcile Slack history into the log instead; that pass re-imported
        other runs' conversations and every other bot's messages into this run,
        so a restart now restores from the database only, and the engine's own
        posts reach it write-through (`_post_message`). What a restart can no
        longer recover — Slack-native messages posted while the engine was down
        — is accepted.

        The cursor seed is still needed on a resume: `_rebuild_state_from_db`
        only advances cursors for channels with stored mirrored rows, and the
        live poller defaults every other channel to "0".
        """
        await self._seed_slack_cursors_without_ingest()

    async def _rebuild_agent_state(self) -> None:
        """Reconstruct per-agent state from the message log + DB.

        Runs after the DB rebuild, so it
        behaves identically with Slack on or off. Reads only self.message_log,
        thread_decisions and llm_call_logs — no Slack calls.
        """
        # Rebuild active_threads per agent.
        # Get all closed thread IDs and prior thread summaries from thread_decisions
        closed_thread_ids: set[str] = set()
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.simulation_run_id == self.simulation_run_id
                        )
                    )
                    all_decisions = result.scalars().all()
                    for td in all_decisions:
                        closed_thread_ids.add(td.thread_id)
                        # _prior_threads is a list per pair, so appending here
                        # unconditionally is not idempotent: a second rebuild —
                        # or a rebuild after _close_thread already recorded this
                        # thread in-process — feeds Phase 5 the same prior
                        # discussion twice, as "you already tried this N times".
                        # _closed_thread_ids is the shared already-accounted-for
                        # marker (_close_thread sets it before its own append),
                        # and it is only updated after this loop, so a thread with
                        # several decision rows from repeated propose/reopen cycles
                        # still contributes each of them on the first pass.
                        if td.thread_id in self._closed_thread_ids:
                            continue
                        pair_key = tuple(sorted([td.agent_a, td.agent_b]))
                        self._prior_threads.setdefault(pair_key, []).append({
                            "channel": td.channel,
                            "outcome": td.outcome,
                            "summary": (td.summary_text or "")[:400] or None,
                            # Carried for G3 dedup-context visibility filtering.
                            "origin_visibility": td.origin_visibility,
                        })
                        pair_list = self._prior_threads[pair_key]
                        if len(pair_list) > PRIOR_THREADS_KEPT_PER_PAIR:
                            del pair_list[: len(pair_list) - PRIOR_THREADS_KEPT_PER_PAIR]
                    self._closed_thread_ids.update(closed_thread_ids)
            except Exception as exc:
                logger.warning("Failed to load thread decisions: %s", exc)

        for agent in self.agents.values():
            aid = agent.agent_id
            # Find threads where this agent participated
            for entry in self.message_log._entries:
                if entry.sender_agent_id != aid:
                    continue
                if is_panel_note(entry):
                    # Posting a note is not participating. Restoring a thread
                    # off one would resurrect, for the hub, an interview it had
                    # not yet said anything in — and would do it from an entry
                    # that `get_thread_history` (used further down for the
                    # participant and pending-reply decisions) cannot see, so
                    # the two halves of this reconstruction would disagree.
                    continue
                thread_id = entry.thread_ts or entry.ts
                # Skip if already closed or already tracked
                if thread_id in closed_thread_ids:
                    continue
                if thread_id in agent.state.active_threads:
                    continue
                # Skip thread reconstruction for collab_private channels —
                # discussion in those channels is flat, not threaded, so
                # there shouldn't be an active_thread at all. Any threaded
                # replies pre-dating this rule are left alone in Slack but
                # not reactivated in-memory.
                if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                    continue
                # Skip a root post that has no replies — nothing to restore.
                # (Root posts that DO have replies must be restored: a reply
                # arriving while we were at the active-thread cap could have
                # been dropped from Phase 3 and would otherwise be ghosted.)
                if entry.thread_ts is None:
                    history = self.message_log.get_thread_history(thread_id)
                    if len(history) <= 1:
                        continue
                # Find the other agent in this thread
                root = self.message_log.get_entry(thread_id)
                if not root:
                    continue
                other_id = root.sender_agent_id if root.sender_agent_id != aid else None
                if not other_id:
                    # Check other replies for the other agent
                    history = self.message_log.get_thread_history(thread_id)
                    for h in history:
                        if h.sender_agent_id and h.sender_agent_id != aid:
                            other_id = h.sender_agent_id
                            break
                if not other_id:
                    continue

                msg_count = self.message_log.get_thread_message_count(thread_id)
                # Check if the last message was from the other agent (pending reply)
                history = self.message_log.get_thread_history(thread_id)
                last_sender = history[-1].sender_agent_id if history else None
                has_pending = last_sender is not None and last_sender != aid
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=msg_count,
                    has_pending_reply=has_pending,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed. This rebuild runs at process
                    # startup, before this fresh process's _specialist_consults
                    # could hold anything, so this always starts False here —
                    # correctly matching the post-restart fail-open case
                    # _specialist_floor_gap's docstring describes. It is not
                    # stuck there: the per-turn latch at the top of
                    # _reply_to_thread re-checks the global map on every later
                    # turn this restored thread takes, and arms once the
                    # restarted process itself records any consult.
                    floor_armed=bool(self._specialist_consults),
                )

        # 4. Rebuild api_call_count per agent from DB.
        #
        # Per CALL, not per ROW. One `llm_call_logs` row is one TURN and a turn
        # can be several real billed API calls — 78.6% of stored `thread_reply`
        # rows are 2+. Live booking is per-call (`_on_llm_call` books the tool
        # rounds; `record_api_call` books everything else), so a per-row rebuild
        # would silently reset every restarted agent's lifetime count and window
        # to a fraction of its real spend.
        #
        # `COALESCE(jsonb_array_length(call_stats), 1)`, never a bare
        # `jsonb_array_length`: 4,650 of the 5,771 stored rows have `call_stats
        # IS NULL` (the column arrived in migration 0032), and NULL propagates
        # through SUM — collapsing the lifetime rebuild and loosening the
        # throttle in the opposite direction. A row that recorded nothing is
        # worth exactly the one call we know it made.
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import func as sa_func
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(
                            LlmCallLog.agent_id,
                            sa_func.sum(_CALLS_PER_LOG_ROW).label("count"),
                        )
                        .where(LlmCallLog.simulation_run_id == self.simulation_run_id)
                        .group_by(LlmCallLog.agent_id)
                    )
                    for r in result:
                        agent = self.agents.get(r.agent_id)
                        if agent:
                            agent.api_call_count = int(r.count or 0)
            except Exception as exc:
                logger.warning("Failed to rebuild api_call_count: %s", exc)

        # 4b. Rebuild the sliding-window call ledger from the same table.
        #
        # Deliberately SEPARATE from step 4. Both now sum `_CALLS_PER_LOG_ROW`
        # (this comment said step 4 "stays an all-time COUNT(*)" until the
        # per-call change landed), but they differ in WINDOW and in purpose:
        # step 4 is all-time lifetime accounting (run summary,
        # SimulationRun.total_api_calls) while call_times is the live throttle
        # and reads only back to the rate window's cutoff.
        # Folding these together is the bug — it is what made an over-budget
        # agent over-budget again on every restart, forever. See design §4.2.
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select

                # datetime, UTC and timedelta are already module-level imports
                # (simulation.py:10) — do not re-import them here.
                cutoff = deps.datetime.now(UTC) - timedelta(
                    seconds=deps.get_settings().llm_rate_window_seconds
                )
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(
                            LlmCallLog.agent_id,
                            LlmCallLog.created_at,
                            _CALLS_PER_LOG_ROW.label("calls"),
                        )
                        .where(
                            LlmCallLog.simulation_run_id == self.simulation_run_id,
                            LlmCallLog.created_at >= cutoff,
                        )
                        .order_by(LlmCallLog.created_at)
                    )
                    rows = result.all()
                # call_times is a deque that try_reserve appends to
                # AND that record_api_call's default (already_reserved=False)
                # path also appends to (see that method's
                # docstring; the seven call sites that rely on this default are
                # never separately reserved, so record_api_call is the only
                # place they are booked into the window at all), same
                # shape as the _prior_threads rebuild above — so a plain append here is
                # not idempotent either: a second rebuild call would duplicate
                # every in-window entry and could throttle an agent that isn't
                # actually over its allowance. Unlike _prior_threads, this
                # query is a full window snapshot (not one row per agent), so
                # the fix is a clear-then-repopulate rather than a replace-by-key.
                # Clear ALL agents, not just the ones with rows in `rows`: the
                # window query is authoritative for every agent, and an agent
                # with zero in-window calls must end up with an EMPTY ledger,
                # not whatever stale entries it had before this rebuild. The
                # clear is sequenced after the query succeeds (not before) so a
                # DB failure below is caught and logged without first wiping a
                # ledger it then fails to repopulate.
                for agent in self.agents.values():
                    agent.state.call_times.clear()
                for r in rows:
                    agent = self.agents.get(r.agent_id)
                    if agent:
                        # One ENTRY PER CALL, not per row — the same change as
                        # step 4, and it has to move with it. Live booking is
                        # per-call, so a per-row ledger would let a restarted
                        # agent spend the ratio of calls-to-turns more than its
                        # allowance. All of a turn's calls share the row's
                        # timestamp; the window only cares about the boundary,
                        # and the individual calls are seconds apart at most.
                        stamp = r.created_at.timestamp()
                        for _ in range(int(r.calls or 1)):
                            agent.state.call_times.append(stamp)
            except Exception as exc:
                logger.warning("Failed to rebuild call_times: %s", exc)

        # 5. Set last_seen_cursor per agent to latest message time
        if self._reset_cursors:
            logger.info("--reset-cursors: agents will re-scan all posts")
            for agent in self.agents.values():
                agent.state.last_seen_cursor = 0
        elif self.message_log._entries:
            # Panel notes are deliberately NOT excluded from this max. It is a
            # "don't rescan what is already stored" high-water mark, not a
            # per-entry decision, and a note is a real message on the transport
            # — stopping the cursor short of one would leave every agent
            # rescanning up to it forever, and the entries a note could hide
            # behind it are older than it by construction.
            latest_ts = max(e.posted_at for e in self.message_log._entries)
            for agent in self.agents.values():
                agent.state.last_seen_cursor = latest_ts

        # Log rebuild summary
        for agent in self.agents.values():
            at = len(agent.state.active_threads)
            if at:
                logger.info(
                    "[%s] Restored: %d active threads, %d API calls",
                    agent.agent_id, at, agent.api_call_count,
                )

    # ------------------------------------------------------------------
    # LLM call logging
    # ------------------------------------------------------------------


def _build_forward_map() -> dict[str, tuple[str, str]]:
    """Every moved member name -> (engine attribute of its owner, name there).

    Built from the unit classes: each unit's ``OWNED_STATE`` plus every method,
    property and staticmethod it defines. A name two owners define, or one the
    orchestrator also defines, is a wiring bug and fails at import: every member
    has exactly one owner.
    """
    forward = dict(_CONTEXT_FORWARD)
    orchestrator = set(vars(SimulationEngine))
    for holder, cls in _UNIT_CLASSES.items():
        members = set(cls.OWNED_STATE)
        members.update(
            name for name, value in vars(cls).items()
            if not name.startswith("__") and name != "OWNED_STATE" and not isinstance(value, _Via)
        )
        for name in sorted(members - _NOT_FORWARDED):
            if name in forward or name in orchestrator:
                other = forward[name][0] if name in forward else "the orchestrator"
                raise RuntimeError(f"engine member {name!r} is defined by {holder} and {other}")
            forward[name] = (holder, name)
    return forward


_FORWARD = _build_forward_map()
