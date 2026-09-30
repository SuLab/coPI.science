"""Turn-based simulation engine — the orchestrator.

``SimulationEngine`` wires the units in ``src/agent/engine/`` (spec §7.1) and runs
the main loop, the startup sequence and the shutdown sequence. Every member that
moved to a unit is still reachable as ``engine.<name>`` through the forwarding in
``__getattr__``/``__setattr__``/``__delattr__``, so callers, scripts and tests keep
one entry point.
"""

import asyncio
import logging
import uuid
from datetime import UTC
from typing import Any

from src.agent.agent import Agent
from src.agent.end_reasons import HOLD, TODAY, end_reason_class
from src.agent.engine import deps
from src.agent.engine.channel_directory import ChannelDirectory
from src.agent.engine.context import EngineContext, RunState, _Via
from src.agent.engine.control import Control, EngineHeartbeat
from src.agent.engine.headlines import Headlines
from src.agent.engine.llm_log import LlmLog
from src.agent.engine.memory import Memory
from src.agent.engine.panel import Panel
from src.agent.engine.persistence import Persistence
from src.agent.engine.post_lane import PostLane
from src.agent.engine.rebuild import Rebuild
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
from src.agent.message_log import MessageLog
from src.agent.specialists import (
    domain_flatness_warning,
    signal_mix_report,
)
from src.models import SimulationRun
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
    "rebuild": Rebuild,
}

#: Owner-API names two units define under the same name (``headlines.enqueue()``,
#: ``memory.enqueue()``) and owner-API names that were never engine members
#: (the verdict-ledger port, ``bind_ledger``, ``rehydrate``, ``seed_cursor``,
#: ``drop_pending``, ``channel_id_for``). Nothing reaches them as
#: engine.<name>; callers use engine.<unit>.<name>.
_NOT_FORWARDED = frozenset({
    "enqueue", "bind_ledger", "rehydrate", "is_announced", "mark_announced",
    "patch_pending_summary", "assessed_thread_ids",
    "mark_closed", "restore_prior", "activate_thread", "seed_cursor", "drop_pending",
    "channel_id_for",
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
        run_state: RunState | None = None,
        heartbeat: EngineHeartbeat | None = None,
    ):
        self.ctx = EngineContext(
            agents={a.agent_id: a for a in agents},
            slack_clients=slack_clients,
            message_log=MessageLog(),
            session_factory=session_factory,
            simulation_run_id=simulation_run_id,
            slack_enabled=slack_enabled,
        )
        self.run_state = run_state if run_state is not None else RunState()
        self.heartbeat = heartbeat
        if self.heartbeat is not None:
            self.heartbeat.set_circuit_source(
                lambda: self.ctx.circuit.is_open(deps.time.time())
            )
        self.persistence = Persistence(self.ctx)
        self.channel_directory = ChannelDirectory(self.ctx)
        self.ctx.channel_id_resolver = self.channel_directory.channel_id_for
        self.memory = Memory(self.ctx)
        self.control = Control(self.ctx, run_state=self.run_state, heartbeat=heartbeat)
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
            persistence=self.persistence,
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
        self.rebuild = Rebuild(
            self.ctx, threads=self.threads, slack_io=self.slack_io, panel=self.panel,
            channel_directory=self.channel_directory,
            reset_cursors=reset_cursors, fresh_start=fresh_start,
        )

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
        # AG-3: a signal that landed during startup was replayed into RunState
        # before this engine existed; never clear it here.
        if not self.run_state.stop_event.is_set():
            self.run_state.running = True
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
        if self.heartbeat is not None:
            self.heartbeat.mark_running()
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
                # 'Did work' for the backoff below counts reply calls that RETURNED
                # without raising (S1-04, FA1-R1) — including ones that came back
                # empty or whose post was suppressed — so on a healthy tick it
                # equals the old `api_call_count` delta. A rate-limited deferral
                # makes no call and does not count, which keeps the loop from
                # spinning; a call that raised no longer counts either, so a
                # failing API backs off instead of retrying every tick.
                successes_before_reply_lane = self.ctx.circuit.success_seq
                reply_lane_count = await self._dispatch_reply_lane()
                if reply_lane_count:
                    logger.debug(
                        "[reply-lane] serviced %d pair(s) this tick", reply_lane_count
                    )
                reply_lane_did_work = (
                    self.ctx.circuit.success_seq > successes_before_reply_lane
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
        if (
            self._pending_memory_events
            and self._running
            and not self.ctx.circuit.is_open(deps.time.time())
        ):
            await self._drain_memory_events()

        # S2-06 order: messages, then the decisions and verdicts that refer to
        # them, then the LLM call logs.
        await self.persistence._flush_persisted()
        await self.threads.flush_pending_decisions()
        if self.verdicts._pending_assessments:
            await self.verdicts._flush_pending_assessments()
        if self.llm_log._llm_log_buffer:
            await self.llm_log._flush_llm_logs()

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

        Order (spec §8.4 S1-10): flushes, the headline sweep, the bounded memory
        drain, a final flush, then the heartbeat. The sweep no longer waits for
        the drain, so a drain that outlives ``docker stop -t 420`` cannot cost
        the headlines. Only the order changed: the drain runs the same updates
        with the same count bound.

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
        # 1. Flush what the sweep reads (S2-06 order): messages, decisions,
        #    verdicts, then the LLM call logs.
        await self._flush_persisted()
        await self.threads.flush_pending_decisions()
        await self._flush_pending_assessments()
        await self._flush_llm_logs()
        # 2. The headline sweep, decided by the end-reason class (P0-04, §8.2).
        #    It no longer waits for the memory drain, so a drain that outlives
        #    the stop grace period cannot cost the headlines. AFTER the sweep,
        #    whatever it managed (SA4-04): a finalize stands even when headlines
        #    could not post — they were logged LOST with the --finalize repair
        #    command.
        await self.headlines.shutdown_sweep(self.run_state.end_reason)
        await self._record_run_end_state(end_class)
        # 3. Drain a BOUNDED number of queued memory updates BEFORE the log
        #    callback is cleared, so their llm_call_logs rows are captured by
        #    the final flush below. Bounded by count, never by wall clock (B24):
        #    the same updates run as before, only later. The remainder is
        #    dropped loudly rather than racing the SIGKILL.
        try:
            await self._drain_memory_events(limit=MEMORY_EVENTS_MAX_AT_SHUTDOWN)
        except Exception:
            logger.exception("Shutdown memory drain failed")
        if self._pending_memory_events:
            logger.warning(
                "Dropping %d queued working-memory update(s) at shutdown",
                len(self._pending_memory_events),
            )
            self.memory.drop_pending()
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
        # 4. Final flush. `final=True`: this is the LAST attempt at each buffer.
        #    Nothing drains them after `stop()` returns, so a failure here must
        #    say LOST with the row count rather than the "re-queued for retry"
        #    the per-tick path says.
        await self._flush_persisted(force_stats=True, final=True)
        await self._flush_llm_logs(final=True)
        await self.threads.flush_pending_decisions(final=True)
        await self._flush_pending_assessments(final=True)

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
        # 5. The heartbeat task, last: the page shows `stopping` until here.
        if self.heartbeat is not None:
            await self.heartbeat.stop()

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
