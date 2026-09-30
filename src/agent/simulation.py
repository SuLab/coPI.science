"""Turn-based simulation engine — coordinates all agents across all channels."""

import asyncio
import json
import logging
import random
import re
import uuid
from datetime import UTC, timedelta
from typing import Any

from src.agent.agent import Agent
from src.agent.end_reasons import HOLD, TODAY, end_reason_class
from src.agent.engine import constants, deps
from src.agent.engine.channel_directory import ChannelDirectory
from src.agent.engine.context import EngineContext, RunState, _Via
from src.agent.engine.control import Control
from src.agent.engine.headlines import Headlines
from src.agent.engine.llm_log import LlmLog
from src.agent.engine.memory import Memory
from src.agent.engine.panel import Panel
from src.agent.engine.persistence import Persistence
from src.agent.engine.roster import Roster
from src.agent.engine.run_announcer import RunAnnouncer
from src.agent.engine.scheduler import Scheduler
from src.agent.engine.slack_io import SlackIO
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
from src.agent.post_types import (
    PostTypeSpec,
    available_for,
    eligible_targets,
    render_menu,
)
from src.agent.prompt_safety import delimit
from src.agent.specialists import (
    domain_flatness_warning,
    signal_mix_report,
)
from src.agent.state import ThreadState
from src.agent.thread_guidance import phase4_guidance
from src.agent.tools import execute_tool, tools_for_role
from src.models import (
    AgentMessage,
    LlmCallLog,
    SimulationRun,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
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
}

#: Owner-API names two units define under the same name (``headlines.enqueue()``,
#: ``memory.enqueue()``) and owner-API names that were never engine members
#: (the verdict-ledger port, ``bind_ledger``, ``rehydrate``). Nothing reaches them as
#: engine.<name>; callers use engine.<unit>.<name>.
_NOT_FORWARDED = frozenset({
    "enqueue", "bind_ledger", "rehydrate", "held_for", "is_announced", "mark_announced",
    "patch_pending_summary", "unannounced_thread_ids",
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
        # 0 = off. When >0, the engine stops opening NEW pitches once this many
        # top-level posts exist this run, then ends the run when every opened
        # interview has drained (see _proposal_target_drained). Rehydrated on
        # resume from agent_messages (phase='new_post', see
        # _rehydrate_proposal_count) — but rehydration only restores the
        # COUNT; the cap itself is whatever the caller passes in here. main.py's
        # _run_simulation inherits max_proposals from the resumed run's stored
        # config when the CLI value is 0, which is what actually keeps a resume
        # from silently disabling the gate. The gate only enforces when
        # max_proposals > 0.
        self.max_proposals = max_proposals
        self._proposals_posted = 0
        self._proposal_drain_streak = 0
        self._reset_cursors = reset_cursors
        # True only for `--fresh`, which has just minted a new run with no
        # agent_messages/agent_channels rows of its own (it deletes nothing —
        # see main._open_fresh_run). Read by `start()`: only a resume runs the
        # hub's reply-less-pitch recovery, and only a fresh run announces
        # itself. Both kinds of start seed the Slack poll cursors past the
        # history already on the transport (see _restore_slack_state).
        self._fresh_start = fresh_start

        # role name -> declared post_types. Same reason as _role_rate_cache
        # above: load_role() hits the disk on every call.
        self._role_post_types_cache: dict[str, tuple[PostTypeSpec, ...]] = {}

        # Closed thread IDs — prevents Phase 3 from re-activating decided threads
        self._closed_thread_ids: set[str] = set()


        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}

        # Per-agent count of new-post rejections from _post_type_rejection —
        # unavailable post_type, missing/unreachable tagged_agent, or a
        # mutilated-mention reject. Mirrors the roster's _cohort_tags_stripped: a
        # deployment where every pitch is rejected on (e.g.) a tagged_agent
        # spelling slip is otherwise only visible by grepping logs.
        self._post_type_rejections: dict[str, int] = {}

        # Bounds concurrent reply-lane tasks PROCESS-WIDE. Constructed once,
        # here, and never re-constructed per call/per turn — the whole reason
        # the OLD Phase-4 fan-out semaphore (`_llm_fanout_sem`, deleted here)
        # failed at this: it bounded one turn's own fan-out, so N concurrent
        # turns gave N x cap concurrent requests (spec §6.3, ported test
        # `test_the_fanout_bound_is_global_not_per_turn` in
        # tests/unit/test_reply_lane.py).
        self._reply_sem = asyncio.Semaphore(
            max(1, deps.get_settings().reply_lane_max_in_flight)
        )
        # Dispatcher-level in-flight dedup (spec §4.3): a (agent, thread) pair
        # already being serviced — by this dispatch call's own sub-tasks, or
        # by an earlier dispatch call that is still draining when the next
        # tick's call starts — must not be spawned a second time.
        self._reply_in_flight: set[tuple[str, str]] = set()

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
    # Reply lane — every (agent, thread) pair owing a reply, unpaced
    # ------------------------------------------------------------------

    def _pending_reply_pairs(self) -> list[tuple[Agent, ThreadState]]:
        """Every (agent, thread) owing a reply. The reply lane's work queue.

        Unpaced by construction: no staleness weighting, no streak cap, no
        cooldown. A thread that is ready is serviced.

        Ungated (``allowed_sender_ids=None``): this thread is already open, so
        it is entitled to conclude even if the partner has since dropped out
        of the cohort — abandoning it mid-flight would waste every call
        already spent on it. What a grandfathered thread does NOT get is
        reactive *priority*, but that concept no longer exists now that every
        owed reply is serviced every pass regardless of staleness — the only
        thing left to preserve is that it still gets answered at all. See
        v2 §8.

        A genuine new reply resets ``empty_response_count`` so the thread's
        2-strike empty-response backoff gets a fresh attempt at the new
        content, mirroring the old `_phase4_reply_threads` selection half.
        """
        pairs: list[tuple[Agent, ThreadState]] = []
        for agent in self.agents.values():
            for thread in list(agent.state.active_threads.values()):
                if thread.status != "active":
                    continue
                if self._channel_visibility.get(thread.channel) == VISIBILITY_COLLAB_PRIVATE:
                    continue
                has_new = self.message_log.has_new_reply_from_other(
                    thread.thread_id, agent.agent_id, agent.state.last_seen_cursor,
                    allowed_sender_ids=None,
                )
                if has_new:
                    thread.empty_response_count = 0
                if has_new or thread.has_pending_reply:
                    pairs.append((agent, thread))
        return pairs

    async def _service_reply(self, agent: Agent, thread: ThreadState) -> None:
        """Phase 4 for one (agent, thread) pair.

        Guards against a thread that closed *mid-sweep* — `pairs` is snapshotted once by
        `_dispatch_reply_lane` before any of its (possibly many, possibly
        slow) LLM calls run, and `_close_thread` can pop this exact thread out
        from under a sibling pair's call in the same sweep. Without this, the
        agent spends a real Opus call composing a reply into a thread that is
        already closed.

        Otherwise promotes ``has_pending_reply`` to durable True before the
        (possibly failing) reply attempt — mirrors the old
        `_phase4_reply_threads` promotion, so a failed/empty/exception reply
        is retried on the next dispatch (this is also done, for the WHOLE
        batch, by `_dispatch_reply_lane` itself before servicing starts — see
        its docstring; the repeat here is a defensive no-op for anything that
        calls `_service_reply` directly without going through dispatch, e.g.
        tests).

        Does NOT touch ``last_phase5_action_time`` — only a real Phase 5
        action (inside `_phase5_new_post`) may stamp that; conflating
        replying with posting is exactly the cross-lane coupling Task 10 of
        docs/plans/2026-08-14-two-lane-concurrent-scheduler.md removed. Does
        NOT touch ``consecutive_phase5_skips`` either: the reply lane briefly
        owned a once-per-tick reset here, but the reset is idempotent, so
        per-pair and per-tick produce identical final state — any agent
        holding an open pending thread was zeroed every tick either way,
        permanently disabling `_select_agent`'s skip de-weighting. The
        counter is now wholly post-lane-owned: `_phase5_new_post` increments
        it on a skip/rejection and resets it on a genuinely successful post.
        """
        if thread.status != "active" or agent.state.active_threads.get(
            thread.thread_id
        ) is not thread:
            logger.debug(
                "[%s] Reply lane: skipping thread %s — closed or evicted "
                "mid-sweep",
                agent.agent_id, thread.thread_id,
            )
            return
        thread.has_pending_reply = True
        await self._reply_to_thread(agent, thread)

    async def _dispatch_reply_lane(self) -> int:
        """Service every pending pair, concurrently, bounded by
        ``reply_lane_max_in_flight`` (Task 13 of
        docs/plans/2026-08-14-two-lane-concurrent-scheduler.md). At 1 this is
        behaviourally sequential — one pair fully serviced (thread lock
        released, semaphore released) before the next one's own
        lock/semaphore acquisition can succeed. Task 14 of the same plan
        raised the default to 4 once the adversarial
        concurrency tests (tests/integration/test_concurrent_thread_safety.py)
        were green.

        Phase 3 (thread activation) runs first for every agent, each guarded
        by its own try/except (see below): nothing else calls
        it now that `_run_post_turn` is Phase 1 + 5 only, so without this a
        brand-new @-mention or reply-to-a-post would never open a thread at
        all.

        Cursor advancement mirrors the snapshot-then-assign idempotent
        pattern of that plan's Task 6, applied per agent:
        `_phase3_activate_threads` and `_pending_reply_pairs`'s
        `has_new_reply_from_other` check both read `last_seen_cursor` to
        bound their "since cursor" scans (linear scans over the whole log —
        see message_log.py), and nothing else advances
        it for an agent the post lane does not happen to pick this tick.
        Without this, such an agent would rescan the entire message log from
        turn zero on every single main-loop tick, forever. The snapshot is
        taken BEFORE Phase 3 runs and only assigned AFTER `_pending_reply_
        pairs` has read the (still old) cursor — advancing early would hide
        the very replies this pass exists to find, exactly as it would in
        `_run_post_turn`. Nothing in `_run_post_turn` writes this cursor any
        more — this function is its sole owner.

        Retry signal and per-pair isolation:
        - **Batch-wide retry promotion**: every pair's `has_pending_reply` is
          promoted for the WHOLE
          batch, immediately after `_pending_reply_pairs()` returns and
          before the cursor advances or any servicing `await` runs. A pair
          found only via `has_new` (not yet durable) whose SIBLING later in
          this same sequential loop raises must still carry a retry signal —
          the cursor is about to move past whatever made it "new", and
          `has_pending_reply` is the only thing that survives that (see
          `_pending_reply_pairs`'s docstring). Each pair is then serviced
          inside its own try/except so one failing reply can never abort the
          rest of the sweep (spec §8) or propagate out of this call.
        - **Early stop on shutdown**: checks `self._running` so a shutdown
          request is honoured
          within roughly one reply's worth of latency instead of first
          draining the whole sweep — worst case today is ~12 sequential Opus
          calls in a 12-interview star, well past the documented `docker stop
          -t 420` grace period.

        Lane ownership and prologue guarding:
        - **The skip streak is post-lane-owned**: this function no longer
          touches `consecutive_phase5_skips` at all. An earlier version moved
          the reset here
          (once per engaged agent per tick, instead of once per pending pair)
          on the theory that batching would fix the coupling — but the reset
          is idempotent, so per-pair and per-tick produce IDENTICAL final
          state: any agent holding an open pending thread was still zeroed
          every single tick regardless of which of the two shapes did it,
          pinning `stretch` at 1 and permanently disabling `_select_agent`'s
          `skips >= 3` de-weighting. A reply-lane write of a post-lane pacing
          variable is itself the cross-lane coupling this feature exists to
          remove. The counter is now wholly post-lane-owned:
          `_phase5_new_post` increments it on a skip/rejection and resets it
          to 0 on a genuinely successful post (verified — see that method's
          `previous_skips` handling).
        - **Only Phase 3 is guarded**: the blanket try/except that
          used to wrap the ENTIRE call to this function (in `_run_main_loop`)
          is gone — it swallowed a deterministic failure in
          `_pending_reply_pairs` or the cursor-advance step just as silently
          and permanently (one ERROR per tick, forever, no interview ever progressing, while
          the post lane kept posting as if nothing were wrong). What's left
          guarded, narrowly, is `_phase3_activate_threads`: it runs once per
          agent, so one agent's activation bug must not stop every OTHER
          agent's from running too. `_pending_reply_pairs()` itself, and
          anything below it, is now genuinely unguarded — a bug there
          surfaces (crashes the run) rather than being swallowed wholesale.

        Catching that per-agent Phase 3 failure is not
        enough on its own — the cursor-advance loop below used to run
        unconditionally for every agent, so a caught-and-logged exception
        for agent X still marked X's own unprocessed messages "seen" for
        good, the exact permanent, silent loss this whole guard exists to
        avoid. `failed_agent_ids` is collected in the Phase 3 loop and
        consulted in the advance loop so a failed agent's cursor holds where
        it was; its unactivated messages are retried on the next dispatch
        rather than lost.

        Shutdown re-check inside the semaphore (the concurrent rewrite
        regressed the early stop above): `_run` originally
        checked `self._running` only once, at the top, before acquiring
        anything. `asyncio.gather` schedules every pair's `_run` task up
        front, and an uncontended `Semaphore.acquire`/`Lock.acquire` never
        actually suspends — so with only that one check, pair 0 reaches
        its first genuine `await` (the Opus call inside `_service_reply`)
        before pair 1 even LOOKS at `self._running`, which is still True at
        that instant. Every other pair then passes the same stale check and
        parks on the semaphore; a stop requested while pair 0 is in flight
        is invisible to all of them, and the WHOLE sweep drains instead of
        stopping after pair 0 — measured, at cap=1, with a real `await`
        inside the mocked `_service_reply`: 6 pending pairs, stop requested
        during pair 0, served 6 instead of 1. `_run` now re-checks
        `self._running` a second time, inside the semaphore, immediately
        before the thread lock and the real service call — that is the
        check that actually matters, since it fires at the moment a pair
        is genuinely about to be serviced rather than at task-creation
        time. `test_dispatch_stops_early_when_the_engine_stops_mid_sweep`'s
        mock now does a real `await asyncio.sleep(0)` inside
        `_service_reply` — without it, the test cannot fail even against
        the pre-fix single-check code, because a mock with no `await` never
        exercises the task-scheduling gap the bug lived in.
        """
        cursor_snapshots = {
            agent.agent_id: self.message_log.latest_timestamp
            for agent in self.agents.values()
        }
        # An agent whose Phase 3 pass raises must NOT have
        # its cursor advanced below — that would mark this exact agent's
        # unprocessed messages "seen" on the strength of a pass that never
        # actually ran, a permanent silent loss of the same shape the
        # batch-wide retry promotion (see the docstring) exists to prevent (see
        # test_dispatch_isolates_one_agents_phase3_failure_from_the_others in
        # tests/unit/test_reply_lane.py, which only
        # pins that OTHER agents keep working — it says nothing about the
        # failed agent's own cursor). Collected here, consulted in the advance
        # loop below.
        failed_agent_ids: set[str] = set()
        for agent in self.agents.values():
            try:
                self._phase3_activate_threads(agent)
            except Exception:
                failed_agent_ids.add(agent.agent_id)
                logger.exception(
                    "[reply-lane] %s: error activating threads (Phase 3)",
                    agent.agent_id,
                )

        pairs = self._pending_reply_pairs()

        # Promote the whole batch's retry flag BEFORE the cursor advances
        # and before any servicing await runs. See "Batch-wide retry
        # promotion" in the docstring.
        for _agent, thread in pairs:
            thread.has_pending_reply = True

        for agent in self.agents.values():
            if agent.agent_id in failed_agent_ids:
                continue
            agent.state.last_seen_cursor = max(
                agent.state.last_seen_cursor, cursor_snapshots[agent.agent_id]
            )

        serviced = 0

        async def _run(agent: Agent, thread: ThreadState) -> None:
            nonlocal serviced
            # I5, cheap early exit: catches a stop requested before this
            # pair's task got a look-in at all. NOT sufficient on its own —
            # see the second check below, which is the one that actually
            # matters.
            if not self._running:
                return
            key = (agent.agent_id, thread.thread_id)
            # Dispatcher-level in-flight dedup (spec §4.3): a pair already
            # being serviced by another still-draining `_dispatch_reply_lane`
            # call must not be spawned again. Check-then-add with NO `await`
            # between them — the same check-then-act shape every other
            # invariant in this file is careful about — so two concurrent
            # dispatch calls racing this exact pair can't both pass.
            if key in self._reply_in_flight:
                return
            self._reply_in_flight.add(key)
            try:
                async with self._reply_sem:
                    # I5, THE check that matters (task review, Critical):
                    # `asyncio.gather` schedules every pair's task up front,
                    # and an uncontended `Semaphore`/`Lock.acquire` never
                    # actually suspends — so at cap=1, pair 0 reaches its
                    # first genuine `await` (the Opus call inside
                    # `_service_reply`) before pair 1 even looks at
                    # `self._running` above. A stop requested WHILE pair 0 is
                    # in flight is invisible to every pair still queued
                    # behind the semaphore unless it is re-checked here, at
                    # the moment a pair is actually about to be serviced
                    # (immediately after acquiring the semaphore slot,
                    # immediately before the thread lock and the real
                    # service call). Without this second check, a stop
                    # requested during pair 0 drains the ENTIRE sweep instead
                    # of stopping after pair 0 — measured: a 6-pair sweep
                    # with a real `await` inside `_service_reply`, stopped
                    # during pair 0, served all 6 instead of 1. See
                    # test_dispatch_stops_early_when_the_engine_stops_mid_sweep,
                    # whose mock now awaits for exactly this reason.
                    if not self._running:
                        return
                    # Thread lock held ACROSS the LLM call: the stale-history
                    # (§4.1) and CONCLUDE-ordinal (§4.2) races both happen
                    # between the read and the act, not inside either — a
                    # lock released before `_service_reply` returns would not
                    # fix them.
                    #
                    # LOCK ORDER (see the note on `_thread_locks` in
                    # __init__): this acquires the THREAD lock. Everything
                    # nested inside `_service_reply` that also needs an AGENT
                    # lock (`_close_thread` via `_check_thread_outcome` or the
                    # system-enforced-close branch of `_reply_to_thread`;
                    # `_evict_dead_thread` via `_post_message`'s
                    # ThreadNotFound handling) acquires it while this thread
                    # lock is already held — thread-lock-outer, agent-lock-
                    # inner, never the reverse.
                    async with self._thread_locks.acquire_all(thread.thread_id):
                        try:
                            await self._service_reply(agent, thread)
                        except Exception:
                            logger.exception(
                                "[reply-lane] %s: error servicing thread %s",
                                agent.agent_id, thread.thread_id,
                            )
                        finally:
                            serviced += 1
            finally:
                self._reply_in_flight.discard(key)

        await asyncio.gather(
            *(_run(agent, thread) for agent, thread in pairs),
            return_exceptions=True,
        )
        return serviced

    # ------------------------------------------------------------------
    # Turn execution — post lane (Phase 1 + Phase 5)
    # ------------------------------------------------------------------

    async def _run_post_turn(self, agent: Agent) -> bool:
        """Run Phase 1 + Phase 5 for one agent. Returns True if work was done.

        The paced lane: Phase 3 (thread activation) and Phase 4 (thread
        reply) moved to the reply lane (`_dispatch_reply_lane` /
        `_service_reply`) entirely — see docs/specs/2026-08-14-two-lane-
        concurrent-scheduler-design.md §2.

        This function does NOT touch
        `last_seen_cursor` at all — neither Phase 1 nor Phase 5 reads it, and
        `_dispatch_reply_lane` already owns cursor advancement for every
        agent, every tick (that is the only reader: `_phase3_activate_threads`
        / `_pending_reply_pairs`'s `has_new_reply_from_other` check). A
        second writer here, taking its OWN snapshot after `_dispatch_reply_
        lane` has already run (and possibly spent many seconds/minutes making
        LLM calls that posted new messages), would capture a `latest_
        timestamp` far ahead of what this agent's Phase 3 actually saw this
        tick — and assigning it would silently mark a message Phase 3 never
        processed as "seen", permanently stalling that thread with no
        backstop (`has_pending_reply` was already cleared by the reply that
        made the thread's status update). This is exactly the Task-6 bug,
        reintroduced by a second cursor writer with no corresponding reader.
        See tests/unit/test_cursor_advance.py.
        """
        settings = deps.get_settings()
        api_calls_before = agent.api_call_count
        agent.state.in_flight = True
        try:
            # Phase 1: Channel discovery
            await self._phase1_channel_discovery(agent)

            # Spontaneous post timer — allow one Phase 5 call after enough
            # idle time so agents can organically start new conversations. This
            # is the ONLY Phase 5 gate here: the paced post lane must not be
            # driven by reply volume from the unpaced reply lane — see
            # tests/unit/test_post_lane.py. The daily cap and the other Phase 5
            # guards are unchanged and live inside `_phase5_new_post` itself.
            base_interval = settings.phase5_spontaneous_interval * 60  # to seconds
            skips = agent.state.consecutive_phase5_skips
            stretch = min(
                max(skips, 1), settings.phase5_spontaneous_interval_max_multiplier
            )
            spontaneous_interval = base_interval * stretch
            since_last_action = deps.time.time() - agent.state.last_phase5_action_time
            spontaneous_ready = since_last_action >= spontaneous_interval

            if spontaneous_ready:
                await self._phase5_new_post(agent)
            else:
                logger.debug(
                    "[%s] Phase 5: Skipped (spontaneous timer not due for %ds)",
                    agent.agent_id,
                    int(spontaneous_interval - since_last_action),
                )

            return agent.api_call_count > api_calls_before
        finally:
            agent.state.in_flight = False

    # ------------------------------------------------------------------
    # Phase 1: Channel Discovery
    # ------------------------------------------------------------------

    async def _phase1_channel_discovery(self, agent: Agent) -> None:
        """Join new channels based on profile keyword matching."""
        profile_text = agent.public_profile.lower()
        channels_to_join = set(constants._UNIVERSAL_CHANNELS)

        for channel_name, keywords in constants._CHANNEL_KEYWORDS.items():
            if any(kw in profile_text for kw in keywords):
                channels_to_join.add(channel_name)

        new_channels = channels_to_join - agent.state.subscribed_channels
        if new_channels:
            for ch_name in new_channels:
                ch_id = self._channel_id_map.get(ch_name)
                if ch_id:
                    client = self.slack_clients.get(agent.agent_id)
                    if client:
                        await client.ajoin_channel(ch_id)
            agent.state.subscribed_channels.update(new_channels)
            logger.info("[%s] Phase 1: Joined channels: %s", agent.agent_id, new_channels)

    # ------------------------------------------------------------------
    # Phase 3: Activate Threads from Tags
    # ------------------------------------------------------------------

    def _phase3_activate_threads(self, agent: Agent) -> None:
        """
        Auto-activate threads where this agent was tagged or
        where someone replied to this agent's top-level posts.

        Skipped entirely for entries in collab_private channels: those channels
        are flat discussions (no threading), so tags and replies there are
        just conversation content for later phases to read directly, not
        thread-activation signals.

        Human-authored (``is_bot=False``) entries are skipped in all three loops
        below (tags, replies, hub auto-activation) — the bot-behavior half of
        decision 5 (2026-08-12 PI-interaction removal cycle): there is no
        PI-bot interaction surface left for a human post to activate a thread,
        including the substring-match trap ``_infer_agent_id`` could otherwise
        walk into (e.g. a human sender name like "Andrew Su (PI)" contains the
        real agent_id "su"). The GATED ``MessageLog`` reads these loops consume
        (``get_tags_for_agent``/``get_replies_to_agent_posts``/
        ``get_new_top_level_posts``) deliberately still return human rows —
        they are general-purpose per-agent reads whose history/observability
        half of decision 5 is kept — so the filter belongs here, at the actual
        point of activation, not in those shared methods.
        """
        cursor = agent.state.last_seen_cursor

        # Check for tags
        tagged_entries = self.message_log.get_tags_for_agent(
            agent.bot_name, cursor, allowed_sender_ids=agent.allowed_sender_ids
        )
        for entry in tagged_entries:
            if not entry.is_bot:
                continue
            # Private channels are flat — no thread activation.
            if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            thread_id = entry.thread_ts or entry.ts
            if thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            # Threshold gates Phase 5 (starting new threads), not Phase 3.
            # Ignoring an explicit @-mention is worse than running over the cap.
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and agent.agent_id not in allowed:
                logger.info(
                    "[%s] Phase 3: Skipping tagged thread %s — not in allowed set %s",
                    agent.agent_id, thread_id, allowed,
                )
                continue
            # Determine the other agent
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed and the latch at the top of
                    # _reply_to_thread.
                    floor_armed=bool(self._specialist_consults),
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (tagged by %s)",
                    agent.agent_id, thread_id, other_id,
                )

        # Check for replies to agent's own top-level posts
        reply_entries = self.message_log.get_replies_to_agent_posts(
            agent.agent_id, cursor, allowed_sender_ids=agent.allowed_sender_ids
        )
        for entry in reply_entries:
            if not entry.is_bot:
                continue
            # Private channels are flat — no thread activation.
            if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            thread_id = entry.thread_ts
            if not thread_id or thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            # Threshold gates Phase 5 (starting new threads), not Phase 3.
            # Ghosting a reply to our own post is worse than running over the cap.
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed and the latch at the top of
                    # _reply_to_thread.
                    floor_armed=bool(self._specialist_consults),
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (reply from %s)",
                    agent.agent_id, thread_id, other_id,
                )

        # Hub auto-activation: the scout hub opens an interview thread on
        # every new lab top-level post, no @-mention required. Gated on the
        # plain `agent.role` attribute (NOT `self._roles_by_agent()` — see
        # INV-E structural note 4, a separate, separately-recomputed
        # consumer of role knowledge).
        if agent.role == "scout_hub":
            self._auto_activate_lab_posts(agent, since=cursor)

    def _auto_activate_lab_posts(self, agent: Agent, since: float) -> int:
        """Open an interview thread on every new lab top-level post after ``since``.

        The hub's half of Phase 3, extracted so the resume one-shot
        (`_recover_reply_less_pitches`) applies exactly the same gates: human
        rows, the agent's cohort gate (`allowed_sender_ids`), collab_private
        channels, closed threads, already-active threads and
        `get_thread_allowed_agents`. Returns how many threads it activated.
        The caller decides which agents are hubs.
        """
        activated = 0
        new_posts = self.message_log.get_new_top_level_posts(
            since=since,
            channels=agent.state.subscribed_channels,
            exclude_agent_id=agent.agent_id,
            allowed_sender_ids=agent.allowed_sender_ids,
        )
        for entry in new_posts:
            if not entry.is_bot:
                continue
            # Private channels are flat — no thread activation.
            if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            thread_id = entry.thread_ts or entry.ts
            if thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed and the latch at the top of
                    # _reply_to_thread.
                    floor_armed=bool(self._specialist_consults),
                )
                activated += 1
                logger.info(
                    "[%s] Phase 3: Auto-activated interview thread %s (lab post by %s)",
                    agent.agent_id, thread_id, other_id,
                )
        return activated

    # ------------------------------------------------------------------
    # Phase 4: Reply to a single thread
    # ------------------------------------------------------------------

    async def _reply_to_thread(self, agent: Agent, thread: ThreadState) -> None:
        """Compose and post a reply to a single thread.

        Runs under `_dispatch_reply_lane`'s THREAD lock for
        `thread.thread_id`, held for this call's entire duration (across the
        LLM call) by the caller — see `_run` there. That is deliberate, not
        incidental: the stale-history read below and the CONCLUDE-ordinal
        computed from it (spec §4.1, §4.2) are a check-then-act pair that
        only holds if nothing else can touch this same thread between the
        read and the reply landing. Both `_close_thread` (via
        `_check_thread_outcome`, and the system-enforced-close branch below)
        and `_evict_dead_thread` (via `_post_message`'s ThreadNotFound
        handling) may additionally take an AGENT lock while this thread lock
        is held — that nesting order (thread-lock-outer, agent-lock-inner) is
        the one documented on `_thread_locks` in __init__ and must never
        invert.
        """
        # Monotonic latch for the specialist floor's fail-open snapshot
        # (ThreadState.floor_armed), re-evaluated at the START of every turn on
        # this thread, before any `await` in this method.
        #
        # `floor_armed` is set once at activation (see the four ThreadState(...)
        # construction sites), but activation happens long before this thread's
        # own interview does its own specialist consulting — freezing it there
        # and never touching it again meant a thread activated while
        # `_specialist_consults` was still globally empty could never arm,
        # even once ITS OWN later consult calls (via `on_consult` in the tool
        # executor below) made the global map non-empty. That silently exempted
        # an under-vetted "advance"/"conditional" verdict from the floor
        # entirely — worse than the concurrency race this field was built to
        # fix. The same staleness made a restart-rebuilt thread
        # (`_rebuild_agent_state`, always constructed with floor_armed=False)
        # permanently unenforceable even after the process had recorded many
        # consults.
        #
        # The `or` makes this monotonic — once armed, always armed for this
        # thread — and it re-reads the GLOBAL map, never this thread's own
        # subject's consults, on purpose: an earlier version of the floor
        # failed open whenever the SUBJECT had no consults, which quietly
        # excused the commonest failure of all, a hub that never convenes a
        # panel at all (see _specialist_floor_gap's docstring). Latching on
        # "this PI has a consult" would walk straight back into that hole.
        #
        # Doing this BEFORE any await, rather than reading the global map live
        # at persist time, is what keeps the original race fixed: this turn's
        # `floor_armed` value is captured once, here, and is not re-read from
        # the live global again before `_persist_assessment` consults it later
        # in this same turn — so a DIFFERENT interview's consult landing in
        # some other task's turn, mid-await, cannot flip this verdict's fate.
        #
        # Accepted residual: if the process's very first-ever consult happens
        # DURING this thread's own concluding turn (recorded by a tool call
        # inside this same call, after this latch already ran), this turn
        # still reads fail-open. That is deliberate, though the reason has
        # changed: enforcement no longer discards anything, so a false
        # positive here costs a wrong `panel_incomplete=True` on a verdict
        # whose panel really was convened — a false accusation in the one
        # number spec §10 exists to report. A false negative costs a verdict
        # recorded as UNVERIFIED (`missing_domains=[]`, see
        # `_floor_verifiable`), which is visible for what it is and which a
        # human can still review at /admin/assessments. Bias to fail-open.
        thread.floor_armed = thread.floor_armed or bool(self._specialist_consults)

        settings = deps.get_settings()

        # Get thread history from message log
        history_entries = self.message_log.get_thread_history(thread.thread_id)

        # A thread whose ROOT is absent from this run's log is not ours to
        # continue. The only way to get here is a reply ingested into a thread
        # this run never saw — e.g. another workspace bot's thread_broadcast
        # reply landing in a PREVIOUS run's interview (conversations.history
        # returns broadcasts, and the poller mirrors every bot message,
        # thread_ts included). Replying would restart that interview at
        # ordinal 2 with a one-message "history"; get_thread_allowed_agents
        # cannot block it because a missing root reads as "open to anyone".
        # Evict, and pin the id closed so Phase 3's tag/reply scans (which all
        # check _closed_thread_ids) cannot re-activate it on the next tick.
        if self.message_log.get_entry(thread.thread_id) is None:
            logger.warning(
                "[%s] Phase 4: evicting thread %s in #%s — no root in this "
                "run's log (reply into a previous run's thread?)",
                agent.agent_id, thread.thread_id, thread.channel,
            )
            agent.state.active_threads.pop(thread.thread_id, None)
            self._closed_thread_ids.add(thread.thread_id)
            return

        thread_history = [
            {"sender": e.sender_name, "content": e.content}
            for e in history_entries
        ]

        # Update message count.
        thread.message_count = len(history_entries)

        # Final participation check before composing a reply
        allowed = self.message_log.get_thread_allowed_agents(thread.thread_id)
        if allowed and agent.agent_id not in allowed:
            logger.info(
                "[%s] Phase 4: Aborting reply to thread %s — not in allowed set %s",
                agent.agent_id, thread.thread_id, allowed,
            )
            agent.state.active_threads.pop(thread.thread_id, None)
            return

        # Check for system-enforced close. Correct on its own terms: a thread
        # with `max_thread_messages` messages already in it is genuinely full,
        # and this must stay a check on the PRIOR count, not the ordinal —
        # closing here is "there is no room left to reply", a different
        # question from "what phase is the reply I'm about to write in".
        #
        # Latent coupling worth knowing about: thread_guidance.py's CONCLUDE
        # boundary is a hardcoded literal (12), independent of
        # `settings.max_thread_messages`. They agree today only because both
        # happen to be 12. Below (build_phase4_prompt's ordinal fix), a reply
        # generated at prior-count 11 gets ordinal 12 -> CONCLUDE, then THIS
        # check closes the thread as full on the very next turn (prior-count
        # 12). If `max_thread_messages` is ever configured to something other
        # than 12, that "CONCLUDE, then close next turn" handoff drifts: e.g.
        # max_thread_messages=20 lets ordinals 12-19 all render as CONCLUDE
        # (thread_guidance doesn't know the cap moved), and max_thread_messages
        # < 12 closes the thread as a timeout before CONCLUDE guidance is ever
        # reachable at all — exactly the failure mode the ordinal fix removed
        # for the default value. `_warn_if_hub_conclude_missing_assessment`
        # reads thread_guidance directly (not this setting) for exactly this
        # reason.
        if thread.message_count >= settings.max_thread_messages:
            logger.info(
                "[%s] Thread %s reached max messages, closing",
                agent.agent_id, thread.thread_id,
            )
            await self._close_thread(agent, thread, "timeout")
            return

        # Get other agent info
        other_agent = self.agents.get(thread.other_agent_id)
        other_name = other_agent.bot_name if other_agent else thread.other_agent_id
        other_lab = other_agent.pi_name if other_agent else "Unknown"

        # Resolve the thread's channel visibility for G1 prompt scoping. In v1
        # all threads live in public channels, so this is effectively always
        # VISIBILITY_PUBLIC; the lookup hook is in place for when migrations
        # start producing collab_private channels.
        thread_visibility = self._resolve_channel_visibility(thread.channel)
        thread_channel_id = self._channel_id_map.get(thread.channel)

        # Same computation `build_phase4_prompt` makes internally (message
        # count read BEFORE this reply exists, +1 for the reply's own
        # ordinal) — recomputed here rather than threaded back out of that
        # call, purely to stamp the llm_call_logs row below with what phase
        # and ordinal this turn was composed under.
        reply_message_ordinal = thread.message_count + 1
        reply_thread_phase, _, _ = phase4_guidance(agent.role, reply_message_ordinal)

        # Build prompt
        system_prompt, messages = agent.build_phase4_prompt(
            thread=thread,
            thread_history=thread_history,
            other_agent_name=other_name,
            other_agent_lab=other_lab,
            visibility=thread_visibility,
            channel_id=thread_channel_id,
        )

        # The durable twin of `on_consult` below, plus the workspace-visible
        # one. In-memory stays authoritative in-process (the floor reads it, and
        # a failed write must never un-count a consult that happened) — the row
        # is what survives the restart that clears the map, and is the only
        # place a human can see WHO was consulted about an interview and what
        # they said. Same `_pi`/`_t`/`_ch` default binding as `on_consult`, for
        # the same reason.
        #
        # A nested `async def` rather than the lambda this used to be, because
        # there are now two awaits and their ORDER matters: the durable record
        # first, the Slack note second. The record is the artifact a verdict is
        # later audited against; the note is a courtesy to whoever is watching
        # the thread. If only one of them can happen, it must be the record.
        # Both are individually best-effort and neither can raise into the tool
        # (see `_record_specialist_consult` / `_post_panel_note`), so this
        # cannot fail the consult, the turn or the reply.
        async def record_consult(
            _pi=thread.other_agent_id,
            _t=thread.thread_id,
            _ch=thread.channel,
            **fields,
        ) -> None:
            await self._record_specialist_consult(
                agent.agent_id,
                subject_agent_id=_pi,
                thread_id=_t,
                channel_name=_ch,
                **fields,
            )
            await self._post_panel_note(
                agent.agent_id, channel=_ch, thread_ts=_t, **fields,
            )

        # Create tool executor bound to this thread's state
        async def tool_executor(tool_name: str, tool_input: dict) -> str:
            return await execute_tool(
                tool_name, tool_input, agent.agent_id, thread, role=agent.role,
                on_consult=lambda domain, signal, _pi=thread.other_agent_id, _t=thread.thread_id: (
                    self._note_consult(_pi, domain, signal, _t)
                ),
                on_consult_record=record_consult,
                # A specialist consult is a real, separately billed API call.
                # Without this it was invisible to the sliding-window limiter and
                # to SimulationRun.total_api_calls, so a concluding reply that
                # convened the panel booked 1 call while making up to 9.
                # (This comment used to say "a real Opus call". That was wrong
                # for as long as it existed: the consult passed no `model` and
                # inherited the Sonnet default. It is pinned to the Opus setting
                # at its call site as of the Opus 5 / Sonnet 5 migration —
                # src/agent/tools.py::_execute_consult_specialist.)
                on_api_call=agent.record_api_call,
                own_dois=agent.own_publication_dois,
            )

        if not agent.try_reserve(
            self._allowance_for(agent), deps.get_settings().llm_rate_window_seconds
        ):
            logger.warning(
                "[%s] rate-limited; deferring this reply", agent.agent_id,
            )
            return
        # already_reserved=True: try_reserve just appended this exact call to
        # call_times — appending again here would double-book it.
        agent.record_api_call(already_reserved=True)
        # Filled by `on_stop_reason` below, then read once the reply is
        # extracted. A list rather than a scalar for the same reason
        # src/agent/tools.py uses one: the callback is a plain `append`, so the
        # collection needs no closure and no nonlocal.
        stop_reasons: list[str] = []
        try:
            raw_response = await deps.generate_with_tools(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools_for_role(agent.role),
                tool_executor=tool_executor,
                model=settings.llm_agent_model_opus,
                # 2500, not 1500: a scout_hub CONCLUDE reply carries the
                # `<assessment_json>` sidecar, emitted LAST. Truncation there
                # drops the closing tag, so _extract_assessment_json returns
                # None and the verdict is lost permanently — the reply has
                # already been posted and no path re-attempts the artifact.
                # _phase5_new_post carries the sizing history for this exact
                # artifact (1000 truncated it "while leaving the Slack post
                # looking complete") and sat at 2500; when Option A moved the
                # sidecar here, this call was not raised to match. A ceiling is
                # not a spend — a short pi_lab reply costs the same as before.
                #
                # 4000, up from 2500, for the Opus 5 / Sonnet 5 migration. Two
                # compounding reasons, both of which attack the sidecar this
                # ceiling exists to protect: this is the one call site running
                # ADAPTIVE thinking (see llm.py's tools call), and max_tokens
                # caps thinking + text TOGETHER; and the 4.7-generation tokenizer
                # yields ~30% more tokens for the same text. 2500 was already
                # truncating here on Sonnet 4.6 (observed in run 2026-08-19
                # 13:35), and a truncated CONCLUDE reply is exactly how a verdict
                # gets lost.
                #
                # 16000, up from 4000. Measured on the run started 2026-08-21
                # 12:01 (Opus 5, rubric v2): the container log shows 9 of 108
                # thread_reply turns hit "Response truncated" at 4000 (~8%).
                # That count has to come from the log, not `llm_call_logs`:
                # this phase's stored `output_tokens` is CUMULATIVE across
                # every tool round AND any retry, so `output_tokens > 4000`
                # matches a 12-row candidate set with no way to pick out which
                # 9 actually truncated. The largest sidecar reply's final
                # text was 18,553 characters.
                #
                # Adaptive thinking, not the sidecar text, turned out to be
                # the dominant consumer of this budget: this is the one call
                # site running ADAPTIVE thinking, and max_tokens caps
                # thinking + text TOGETHER. chars(response_text)/output_tokens
                # on single-call rows measured 1.41 for claude-opus-5 (1.46
                # restricted to 1-3900 tokens, where a retry is impossible)
                # against 4.08 for claude-opus-4-6 — a ~30% denser tokenizer
                # would predict ~3.1, so roughly 55-65% of output tokens at
                # this site are invisible thinking, not text (six opus-5 rows
                # even logged `length(response_text)=0` with up to 1600 output
                # tokens, so response_text length is not a proxy for tokens
                # consumed). Applying that share to the 18,553-character
                # (~4.5-5k token) largest final text implies that call wanted
                # something like 11-13k tokens total. Every 2x retry (8000,
                # thinking DISABLED, tools dropped) succeeded, so 8000 is only
                # the proven floor for text with thinking off, not a safe
                # ceiling with thinking on; 16000 covers the measured text
                # maximum plus the measured thinking share with headroom.
                # This is not a spend increase: a ceiling is not a spend, and
                # on the ~8% of turns that truncated it REMOVES a second
                # billed call. It also closes a hazard: the retry path passes
                # no `tools`, so a retried concluding turn cannot consult a
                # specialist and regenerates the sidecar in a call that never
                # saw the tool results.
                #
                # Per-call truncation IS attributable from the DB now: 42fc0b2
                # (migration 0032) added `llm_call_logs.call_stats`, one entry
                # per real API call carrying `stop_reason`, the requested
                # `max_tokens` and the thinking/text split. The next resizing of
                # this ceiling is a `jsonb_array_elements(call_stats)` query, not
                # another pass over container logs.
                #
                # 16000 is also the largest value this site may hold without a
                # second change: the truncation retry asks for 2x, and
                # src/services/llm.py's NONSTREAMING_MAX_TOKENS (21_333) is the
                # most the SDK accepts on a non-streaming request. The retry is
                # clamped there rather than doubling, so raising this ceiling
                # again buys the retry nothing at all.
                max_tokens=16000,
                log_meta={
                    "agent_id": agent.agent_id,
                    "phase": "thread_reply",
                    "channel": thread.channel,
                    "thread_ts": thread.thread_id,
                    "thread_phase": _thread_phase_label(reply_thread_phase),
                    "message_ordinal": reply_message_ordinal,
                },
                on_retry=agent.record_api_call,
                # Was the reply the model handed back FINISHED? llm.py returns
                # the partial text either way (see `_was_truncated`), and this
                # is the site where posting it unmarked did the most damage: 4
                # truncated hub replies went to Slack as complete in run
                # 8b64a0e0, mid-sentence, with the PI left to guess.
                on_stop_reason=stop_reasons.append,
                # Cooperative shutdown. `request_stop()` only flips `_running`,
                # and the durable flush runs in main.py's finally — which needs
                # the main loop to RETURN. This is the longest await in the whole
                # engine (measured max 134s: up to max_tool_rounds + 1
                # tool-capable API calls plus a final one), so without this a
                # `docker stop` expired mid-turn and SIGKILLed before the flush,
                # losing the in-flight turn's buffered log rows. Polling the
                # flag here lets a stopping turn finish the round it already
                # started and skip the rest.
                should_continue=lambda: self._running,
            )

            # Extract message from <slack_message> tags, fall back to preamble
            # stripping. Kept as its own variable rather than reassigned in
            # place: a concluding scout_hub reply's <assessment_json> sidecar
            # is written OUTSIDE the <slack_message> block by design (see
            # phase4-thread-reply.md's "Concluding with an Opportunity
            # Assessment" section) — the extraction below (Option A
            # relocation) needs the raw, unfiltered response, not just the
            # text that gets posted.
            response_text = _extract_slack_message(raw_response)

            if not response_text or not response_text.strip():
                thread.empty_response_count += 1
                logger.warning(
                    "[%s] Phase 4: Empty/unparseable response for thread %s (count=%d), skipping",
                    agent.agent_id, thread.thread_id, thread.empty_response_count,
                )
                if thread.empty_response_count >= 2:
                    thread.has_pending_reply = False
                    logger.info(
                        "[%s] Phase 4: Backing off thread %s after %d empty responses",
                        agent.agent_id, thread.thread_id, thread.empty_response_count,
                    )
                    # The back-off is the moment of loss, not the first empty
                    # reply: has_pending_reply stays True after one empty, so
                    # the next Phase-4 pass retries the same ordinal, and a
                    # retry that succeeds owes no drop row. Once backed off,
                    # nothing re-attempts this thread (the lab is waiting on
                    # the hub), so whatever verdict this interview would have
                    # produced — at ANY ordinal, not just CONCLUDE; run
                    # 076e80b6 stranded a thread at count=2 — will never
                    # exist. Hub-only: a lab's empty replies strand the
                    # interview too, but the lab never owed the verdict and
                    # this table records lost assessments.
                    if agent.role == "scout_hub":
                        message_ordinal = thread.message_count + 1
                        thread_phase, _, _ = phase4_guidance(
                            agent.role, message_ordinal
                        )
                        cause = (
                            "the model returned no usable text (see the "
                            "llm.py ERROR for the stop_reason)"
                            if not (raw_response or "").strip()
                            else "the reply could not be parsed into a "
                            "Slack message"
                        )
                        await self._record_assessment_drop(
                            agent.agent_id,
                            "empty_reply",
                            subject_agent_id=thread.other_agent_id,
                            thread_id=thread.thread_id,
                            detail=(
                                f"interview abandoned after "
                                f"{thread.empty_response_count} consecutive "
                                f"empty replies at ordinal {message_ordinal} "
                                f"({thread_phase}); {cause}"
                            ),
                        )
                return

            if _was_truncated(stop_reasons):
                # MARKED, and still posted. The partial text is the only thing
                # this turn produced and the PI is mid-conversation; dropping it
                # would land on the empty-response branch above, which abandons
                # the interview on its second occurrence. See TRUNCATION_NOTICE.
                #
                # Appended to `response_text` itself, not only to the posted
                # copy, so the message log, Slack and `_check_thread_outcome`
                # all see one string. Inert for every downstream reader: the ⏸️
                # close test is a substring search, and `_capture_hub_assessment`
                # parses the UNMARKED `raw_response` for its sidecar.
                logger.warning(
                    "[%s] Phase 4: reply to thread %s was TRUNCATED (%s) — "
                    "posting the partial text with an explicit marker rather "
                    "than as a finished reply",
                    agent.agent_id, thread.thread_id, ", ".join(stop_reasons) or "?",
                )
                response_text = response_text.rstrip() + TRUNCATION_NOTICE

            # Post the reply
            posted = await self._post_message(
                agent.agent_id, thread.channel, response_text,
                thread_ts=thread.thread_id,
            )
            if not posted:
                # _post_message already logged why (e.g. the text stripped to
                # empty once its own sidecar/tag stripping ran, even though it
                # passed the empty-response check above). Nothing reached
                # Slack, so this turn must not count and the reply must not be
                # treated as sent — has_pending_reply stays True so the next
                # Phase 4 pass tries again instead of silently dropping the
                # thread (mirrors the phase-5 new-post suppression handling).
                thread.suppressed_post_count += 1
                logger.info(
                    "[%s] Phase 4: reply to thread %s suppressed — not "
                    "counted, nothing persisted (count=%d)",
                    agent.agent_id, thread.thread_id, thread.suppressed_post_count,
                )
                if thread.suppressed_post_count >= 2:
                    # Same backoff the empty-response branch above uses. Without
                    # it this thread is retried every turn forever at full Opus
                    # price: nothing it does advances message_count, so the
                    # max_thread_messages close can never rescue it either.
                    thread.has_pending_reply = False
                    logger.info(
                        "[%s] Phase 4: Backing off thread %s after %d suppressed posts",
                        agent.agent_id, thread.thread_id, thread.suppressed_post_count,
                    )
                return
            agent.message_count += 1
            thread.has_pending_reply = False
            thread.empty_response_count = 0
            thread.suppressed_post_count = 0

            # Does this reply END the interview? Decided ONCE here, then read
            # twice: `_capture_hub_assessment` needs it to judge the reply's
            # sidecar, and `_check_thread_outcome` below acts on it 3-8 ms later
            # by actually closing the thread. Hoisted rather than recomputed
            # inside the capture because the two must not be able to disagree:
            # the prompts make the hub deliver a NEGATIVE verdict by opening
            # with ⏸️ ("That closes the thread"), so a sidecar on a closing reply
            # is the interview's LAST word — refusing it as "premature" loses
            # the verdict permanently, which is exactly what production did to 4
            # of 5 refusals in run 076e80b6. `_check_thread_outcome` re-derives
            # the same answer from the same helper on the same string rather
            # than taking this bool, so its own direct callers (tests, and any
            # future call site) keep working unchanged.
            closes_thread = _reply_closes_thread(response_text)

            # Option A relocation: the hub's :mag: Opportunity Assessment is
            # no longer a separate Phase-5 post — it is the machine-readable
            # sidecar this same concluding reply carries. Extract and persist
            # it here, gated on `posted` exactly like every other assessment
            # write, so a suppressed reply (stripped to nothing, thread
            # deleted) never produces a phantom row with no corresponding
            # Slack message. A pi_lab reply never carries a sidecar, so this
            # is a no-op for every non-hub agent.
            if agent.role == "scout_hub":
                await self._capture_hub_assessment(
                    agent, thread, raw_response, posted,
                    closes_thread=closes_thread,
                )
                missing = self._warn_if_hub_conclude_missing_assessment(
                    agent, thread, response_text, raw_response,
                )
                if missing:
                    await self._record_assessment_drop(
                        agent.agent_id,
                        missing,
                        subject_agent_id=thread.other_agent_id,
                        thread_id=thread.thread_id,
                        detail=(
                            "concluding reply carried no <assessment_json> sidecar "
                            "and was not a decline"
                        ),
                    )

            # Check for thread outcome
            await self._check_thread_outcome(agent, thread, response_text)

        except Exception as exc:
            logger.error(
                "[%s] Phase 4 reply to thread %s failed: %s",
                agent.agent_id, thread.thread_id, exc,
            )

    async def _check_thread_outcome(
        self,
        agent: Agent,
        thread: ThreadState,
        latest_reply: str,
    ) -> None:
        """Check if a thread should be closed based on the latest reply.

        The ✅-confirms-:memo: proposal handshake that used to live here was
        retired by the pitch-only reconciliation (there is no bilateral
        collaboration left to propose or confirm) — this now only detects the
        explicit ⏸️ no-viable-collaboration close. ``outcome="proposal"`` is
        still a valid ThreadDecision.outcome value for legacy rows, but
        nothing in this method can produce a new one.

        The ⏸️ test itself moved to `_reply_closes_thread` so that
        `_capture_hub_assessment` can ask the SAME question a few lines earlier
        — a sidecar on a reply that closes the interview is that interview's
        real verdict, and the capture gate has to know it before this runs.
        """
        # Check for ⏸️ — explicit "no viable collaboration" signal
        if _reply_closes_thread(latest_reply):
            logger.info(
                "[%s] Thread %s: ⏸️ no-proposal close (by %s)",
                agent.agent_id, thread.thread_id, agent.role,
            )
            # An interview that ends with no verdict on record is the failure the
            # whole assessment pipeline exists to avoid, and until now it was
            # invisible: `_warn_if_hub_conclude_missing_assessment` only fires on
            # a CONCLUDE turn, so on run 8b64a0e0 it fired ZERO times against a
            # run that lost two verdicts and had seven interviews closed
            # mid-screen by the PI's own bot. Record it wherever it happens.
            #
            # A hub ⏸️ decline is NOT this case: `phase4-thread-reply.md`'s
            # Outcome 2 is explicitly "close gracefully, emit no sidecar", and
            # most interviews are meant to end there. Only a close that leaves no
            # verdict AND was not the hub's own decline is anomalous.
            if agent.role != "scout_hub" and thread.thread_id not in self._assessed_threads:
                await self._record_assessment_drop(
                    agent.agent_id,
                    "closed_before_verdict",
                    subject_agent_id=agent.agent_id,
                    thread_id=thread.thread_id,
                    detail=(
                        f"a {agent.role} reply closed the interview with ⏸️ before "
                        "the hub reached a verdict; no assessment was stored and "
                        "none can be now"
                    ),
                )
            await self._close_thread(
                agent, thread, "no_proposal", closed_by_role=agent.role,
            )

    async def _close_thread(
        self,
        agent: Agent,
        thread: ThreadState,
        outcome: str,
        summary_text: str | None = None,
        closed_by_role: str | None = None,
    ) -> None:
        """Close a thread and log the decision.

        ``closed_by_role`` is the role of whoever's reply ended the interview, or
        None for the ``max_thread_messages`` timeout, which no reply triggered.
        Recorded because ⏸️ is an instruction to BOTH roles — the hub's decline
        and a lab withdrawing its own pitch — and ``_check_thread_outcome`` tests
        for it on whichever agent just replied. Seven of run 8b64a0e0's closes
        were a lab bot ending the hub's own screen mid-interview, and in this
        table they looked identical to the single genuine timeout.

        Fires from inside `_reply_to_thread` (system-enforced close) or
        `_check_thread_outcome` (⏸️ decline), both of which run under the
        reply lane's THREAD lock for `thread.thread_id` — see
        `_dispatch_reply_lane`. This method's own mutation of BOTH agents'
        `active_threads` (one of the two motivating cases for `_agent_locks`
        at all, alongside `_evict_dead_thread` below) is additionally guarded
        by the
        AGENT lock, acquired here, nested INSIDE the already-held thread
        lock: thread-lock-outer, agent-lock-inner, per the ordering note on
        `_thread_locks` in __init__. `acquire_all` sorts the two agent ids,
        so two closes racing in opposite directions (this agent/other vs.
        other/this agent — spec §3.2's motivating scenario) converge on the
        same acquisition order and cannot deadlock on each other.
        """
        async with self._agent_locks.acquire_all(agent.agent_id, thread.other_agent_id):
            thread.status = "closed"
            self._closed_thread_ids.add(thread.thread_id)

            # Track for Phase 5 dedup context
            pair_key = tuple(sorted([agent.agent_id, thread.other_agent_id]))
            self._prior_threads.setdefault(pair_key, []).append({
                "channel": thread.channel,
                "outcome": outcome,
                "summary": (summary_text or "")[:400] or None,
            })
            pair_list = self._prior_threads[pair_key]
            if len(pair_list) > PRIOR_THREADS_KEPT_PER_PAIR:
                del pair_list[: len(pair_list) - PRIOR_THREADS_KEPT_PER_PAIR]
            # Remove from active threads
            agent.state.active_threads.pop(thread.thread_id, None)

            # Also close for the other agent if they have this thread active
            other_agent = self.agents.get(thread.other_agent_id)
            if other_agent and thread.thread_id in other_agent.state.active_threads:
                other_agent.state.active_threads[thread.thread_id].status = "closed"
                other_agent.state.active_threads.pop(thread.thread_id, None)

            # Log to DB
            if self.session_factory and self.simulation_run_id:
                try:
                    async with self.session_factory() as db:
                        decision = ThreadDecision(
                            simulation_run_id=self.simulation_run_id,
                            thread_id=thread.thread_id,
                            channel=thread.channel,
                            agent_a=agent.agent_id,
                            agent_b=thread.other_agent_id,
                            outcome=outcome,
                            summary_text=summary_text,
                            closed_by_role=closed_by_role,
                        )
                        db.add(decision)
                        await db.commit()
                except Exception as exc:
                    # No natural retry buffer for a ThreadDecision (unlike
                    # _flush_persisted/_flush_llm_logs, there is no accumulating
                    # list this row is drained from — it is written once, right
                    # here) and the in-memory thread state above has already moved
                    # to "closed" either way, so requeueing would mean inventing a
                    # queue purpose-built for this call site. Make the loss
                    # unmistakable instead: ERROR + a full traceback, up from the
                    # WARNING this used to log.
                    logger.error(
                        "[%s] Failed to log thread decision for %s (outcome=%s): "
                        "%s — LOST, this write will never be retried",
                        agent.agent_id, thread.thread_id, outcome, exc,
                        exc_info=True,
                    )

            logger.info(
                "[%s] Thread %s closed: %s",
                agent.agent_id, thread.thread_id, outcome,
            )

            # Queue working-memory updates for both agents. NOT awaited here:
            # these are LLM calls, and this block holds the thread lock, both
            # agent locks and a reply-lane semaphore slot — running them here
            # serialized every close on the hub's key and starved the reply
            # lane (audit finding 1). _drain_memory_events (main loop / stop)
            # applies them sequentially, which preserves the same lost-update
            # protection the lock provided. summary_text is derived from a
            # cross-agent conversation, so it is fenced as untrusted before it
            # lands in working memory (SEC-14), same as before.
            event = f"Thread in #{thread.channel} with {thread.other_agent_id} closed: {outcome}"
            if summary_text:
                event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
            self._pending_memory_events.append(
                (agent.agent_id, event, VISIBILITY_PUBLIC, None)
            )
            if other_agent:
                other_event = f"Thread in #{thread.channel} with {agent.agent_id} closed: {outcome}"
                if summary_text:
                    other_event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
                self._pending_memory_events.append(
                    (other_agent.agent_id, other_event, VISIBILITY_PUBLIC, None)
                )

            # An interview is over. If it still holds a verdict nobody
            # announced, that verdict has no later turn coming and this is the
            # last moment anything knows the interview ended — before 2026-08-29
            # nothing looked, and production lost two headlines (slusher,
            # rothstein) exactly here. QUEUE only: see `_pending_headlines`.
            held = self._assessed_threads.get(thread.thread_id)
            if (
                held is not None
                and not held.announced
                and thread.thread_id not in self._pending_headlines
            ):
                self._pending_headlines.append(thread.thread_id)

    async def _evict_dead_thread(self, thread_id: str) -> None:
        """Remove a thread_id from every agent's in-memory state.

        Fires when Slack reports the parent message no longer exists (via
        ThreadNotFound or a silent thread_ts drop
        on chat.postMessage). Without eviction the same dead thread gets
        re-polled and replied-to forever, producing noisy error logs and —
        worse — cascading top-level posts.

        Called from `_post_message`'s ThreadNotFound handling, itself called
        from `_reply_to_thread` — i.e. from inside the reply lane's THREAD
        lock for this exact `thread_id` (see `_dispatch_reply_lane`). This
        loops over EVERY agent's `active_threads`, so it needs the AGENT lock
        for every agent, not just the two `_close_thread` above locks: without it, `_close_thread`
        would be the only mutator of `active_threads` under agent-lock
        protection while this one, mutating the SAME dict, raced unguarded.
        `acquire_all` takes every key sorted, so this composes with
        `_close_thread`'s narrower 2-key acquisition (and any other
        `_evict_dead_thread` racing it) without deadlocking on each other —
        one global sorted order across every multi-key acquisition, agent or
        thread. Nested inside the already-held thread lock: thread-lock-
        outer, agent-lock-inner, per the ordering note on `_thread_locks` in
        __init__.
        """
        async with self._agent_locks.acquire_all(*self.agents.keys()):
            evicted_from = 0
            for ag in self.agents.values():
                if thread_id in ag.state.active_threads:
                    ag.state.active_threads.pop(thread_id, None)
                    evicted_from += 1
            # Eviction removes per-agent state but must NEVER un-close a thread.
            # If another caller is racing a _close_thread add() against this eviction,
            # the discard would remove the closed marker, and Phase 3 would re-activate
            # the finished interview. _closed_thread_ids is insert-only.
            if evicted_from:
                logger.info(
                    "Evicted dead thread %s from %d agent(s)' state",
                    thread_id, evicted_from,
                )

    def _get_prior_threads_for_agent(
        self,
        agent_id: str,
        current_visibility: str = VISIBILITY_PUBLIC,
    ) -> dict[str, list[dict]]:
        """Return {other_agent_id: [thread summaries]} visible at the given visibility level.

        Implements G3 (visibility-filtered dedup context): a thread_decision
        with ``origin_visibility='collab_private'`` never surfaces in a
        ``public``-channel Phase 5 prompt. See
        specs/privacy-and-channel-visibility.md §G3.
        """
        result: dict[str, list[dict]] = {}
        for (a, b), threads in self._prior_threads.items():
            if agent_id not in (a, b):
                continue
            other = b if a == agent_id else a
            visible = [
                t for t in threads
                if _visibility_permits(
                    t.get("origin_visibility", VISIBILITY_PUBLIC),
                    current_visibility,
                )
            ]
            if visible:
                result[other] = visible
        return result

    # ------------------------------------------------------------------
    # Phase 5: New Post (conditional)
    # ------------------------------------------------------------------

    async def _phase5_new_post(self, agent: Agent) -> None:
        """Optionally start a new top-level thread.

        Hard-gated for scout_hub (decision 9, reply-only-hub reconciliation):
        the hub's former standalone :mag: Opportunity Assessment is now the
        `<assessment_json>` sidecar carried inside its own Phase-4 CONCLUDE
        reply instead (see `_reply_to_thread`) — it has no top-level post
        type left, ever (role.toml declares `post_types = []`, belt-and-
        suspenders). Returning here before ANY work — no settings lookup, no
        prompt built, no LLM call — is what stops a permanently empty menu
        from burning a full-price Opus call every single turn just to be
        told "skip" (measured cost/noise trap: one production run took the
        hub 30 turns and 0 useful phase-5 LLM calls). Gated on role, not on
        an empty menu, so the invariant holds even if role.toml were ever
        misconfigured back to declaring something.
        """
        if agent.role == "scout_hub":
            return

        # Serialises this agent's Phase-5 turn against the reply lane's
        # _close_thread / _evict_dead_thread mutations of THIS agent's
        # active_threads (spec §4.4/§4.5) — both read by
        # _active_thread_count/_count_today_posts above, and by the daily-cap
        # / thread-threshold checks just below. Single-key acquisition, so
        # ordering relative to _close_thread/_evict_dead_thread's (possibly
        # multi-key) acquisitions is irrelevant here — acquire_all sorts
        # regardless. This never nests a THREAD lock inside it: the only
        # _post_message call below carries no thread_ts, so it can never
        # raise ThreadNotFound / reach _evict_dead_thread — see the ordering
        # note on _thread_locks in __init__.
        async with self._agent_locks.acquire_all(agent.agent_id):
            settings = deps.get_settings()

            # Stamp the spontaneous-post timer up front: consulting Phase 5 consumes
            # the opportunity regardless of whether we end up posting, skipping, or
            # bailing out early. Without this, a "skip" leaves the timer stale and
            # every subsequent turn re-fires Phase 5, burning an LLM call per turn.
            agent.state.last_phase5_action_time = deps.time.time()

            # Daily post cap — pi_lab is capped to one pitch per day (design §9).
            # scout_hub never reaches this line (hard-gated above), and it is the
            # only other role, so `lab_daily_post_cap` is unconditional here — the
            # generic `daily_post_cap` setting this once ternaried against was
            # unreachable and was deleted (2026-08-12 release-gating fix pass, M1).
            today_posts = self._count_today_posts(agent)
            cap = settings.lab_daily_post_cap
            if today_posts >= cap:
                logger.debug("[%s] Phase 5: Skipped (daily cap %d/%d)", agent.agent_id, today_posts, cap)
                return

            # Proposal-count limit: once the run has posted its target number of
            # pitches, stop opening new ones and let interviews drain. Checked
            # here, beside the daily cap, so it costs no LLM call.
            if self.max_proposals > 0 and self._proposals_posted >= self.max_proposals:
                logger.info(
                    "[%s] Phase 5: Skipped (proposal cap %d/%d reached)",
                    agent.agent_id, self._proposals_posted, self.max_proposals,
                )
                return

            # Backpressure against STARTING more work than the agent can finish:
            # too many threads open at once. This used to have a second clause
            # (too many of the agent's proposals awaiting web review) and an
            # exemption letting a blocked agent still file one *terminal*
            # artifact past the block — the hub's assessment. Both are gone: the
            # reconciliation deleted the only post type that was ever exempt (see
            # post_types.py), and nothing on this branch creates a new proposal
            # for a PI to review anymore, so there is nothing left to gate on
            # either. A blocked agent (only ever pi_lab in practice — scout_hub
            # is gated above) now has nothing left it could post regardless, so
            # it skips outright here, no LLM call, exactly like the daily cap.
            if self._active_thread_count(agent) >= settings.active_thread_threshold:
                logger.debug(
                    "[%s] Phase 5: Skipped (at/over active_thread_threshold)",
                    agent.agent_id,
                )
                return

            if random.random() < settings.phase5_skip_probability:
                logger.debug("[%s] Phase 5: Skipped (random)", agent.agent_id)
                return

            # Build prompt — include agent's recent posts for dedup
            recent_entries = self.message_log.get_agent_top_level_posts(agent.agent_id, limit=10)
            recent_posts = [
                {"channel": e.channel, "content_snippet": e.content[:150]}
                for e in recent_entries
            ]

            # Phase 5 always operates in a public channel (see build_phase5_prompt's
            # docstring) — there is no longer any per-turn state that could put it in
            # a private-channel context, so prior-threads dedup uses the default
            # (public) visibility.
            prior_threads = self._get_prior_threads_for_agent(agent.agent_id)

            available_types = self._available_post_types(agent)
            if not available_types:
                # Nothing satisfies role ∩ topology — either a misconfigured
                # role.toml or a cohort gate that leaves this agent with no
                # reachable counterparty for anything it declares. This point is
                # only ever reached by an UNBLOCKED agent (a blocked one already
                # returned above), so an empty menu here is always worth a
                # WARNING — there is no longer a quiet/expected empty-menu case
                # to distinguish it from (that was the hub's, and the hub never
                # reaches this line).
                logger.warning(
                    "[%s] Phase 5: no post type satisfiable — check cohort/roster "
                    "for role %r", agent.agent_id, agent.role,
                )
            post_type_menu = render_menu(
                available_types,
                gate=agent.allowed_sender_ids,
                roles_by_agent=self._roles_by_agent(),
                self_id=agent.agent_id,
                bot_names={aid: a.bot_name for aid, a in self.agents.items()},
            )

            system_prompt, messages = agent.build_phase5_prompt(
                recent_posts=recent_posts,
                prior_threads=prior_threads,
                post_type_menu=post_type_menu,
            )

            # Correlation id for this specific call's log row, generated BEFORE the
            # call and carried through log_meta. `channel` isn't known until the
            # model's response is parsed below, so it can't go in log_meta up
            # front — but the row this call appends to the shared
            # `_llm_log_buffer` can be found again afterward by this id, without
            # trusting the buffer's tail (see the retroactive-channel comment
            # below for why position is unsafe under concurrency).
            llm_call_id = uuid.uuid4().hex

            if not agent.try_reserve(
                self._allowance_for(agent), deps.get_settings().llm_rate_window_seconds
            ):
                logger.warning(
                    "[%s] rate-limited; deferring this post", agent.agent_id,
                )
                return
            # already_reserved=True: try_reserve just appended this exact call to
            # call_times — appending again here would double-book it.
            agent.record_api_call(already_reserved=True)
            # See `_was_truncated`; same collection idiom as the Phase-4 site.
            stop_reasons: list[str] = []
            try:
                response = await deps.generate_agent_response(
                    system_prompt=system_prompt,
                    messages=messages,
                    model=settings.llm_agent_model_opus,
                    # Historical sizing note: this used to also cover scout_hub's
                    # opportunity-assessment post here (an 11-section body plus a
                    # ~15-line <assessment_json> sidecar emitted LAST, where 1000
                    # — sized for a short reply/skip decision — truncated the
                    # verdict first while leaving the Slack post looking
                    # complete, F8). The hub is hard-gated out of this function
                    # now (see the docstring) and its assessment moved to the
                    # Phase-4 CONCLUDE reply's own budget instead, so this
                    # function's only caller today (pi_lab) never needs anywhere
                    # near 2500 tokens for a pitch or a skip — kept at this size
                    # anyway rather than re-tuned down, since a smaller ceiling
                    # buys nothing but risk here. NOTE: src/services/llm.py's
                    # retry-at-2x path logs loudly (logger.error) if the retry
                    # ALSO truncates, but it does not retry again.
                    # 3300, up from 2500: the 4.7-generation tokenizer (Opus 5 /
                    # Sonnet 5) yields ~30% more tokens for the same text, so a
                    # ceiling tuned on Sonnet 4.6 truncates sooner. Thinking is
                    # disabled on this path (llm.py's default), so only the
                    # tokenizer change is being compensated for here.
                    max_tokens=3300,
                    log_meta={
                        "agent_id": agent.agent_id,
                        "phase": "new_post",
                        "call_id": llm_call_id,
                    },
                    on_retry=agent.record_api_call,
                    on_stop_reason=stop_reasons.append,
                )
                if _was_truncated(stop_reasons):
                    # SKIPPED, unlike the Phase-4 reply above, and the asymmetry
                    # is the point: nothing is waiting on this. No thread is open,
                    # no PI is mid-sentence, and no later turn is owed anything —
                    # so a pitch the model did not finish is simply not made. A
                    # half-written action envelope is also the shape most likely
                    # to parse into a post nobody meant (`_parse_phase5_response`
                    # sees a truncated JSON block), which is a workspace-visible
                    # artifact that cannot be retracted.
                    logger.warning(
                        "[%s] Phase 5: response was TRUNCATED (%s) — skipping "
                        "the post rather than publishing a half-written one",
                        agent.agent_id, ", ".join(stop_reasons) or "?",
                    )
                    return
                if not response or not response.strip():
                    logger.warning("[%s] Phase 5: Empty response from LLM, skipping", agent.agent_id)
                    return

                # Parse the JSON + message from the response
                action_data, message_text = self._parse_phase5_response(response)
                if not action_data:
                    logger.warning("[%s] Phase 5: Could not parse response", agent.agent_id)
                    return

                # A missing `action` is an unparseable response, not a license to
                # post something anyway — defaulting to "new_post" here is exactly
                # what let a hijacked action dict (see _parse_phase5_response's
                # sidecar-strip fix) fall through into posting to #general with an
                # empty post_type instead of being rejected outright.
                action = action_data.get("action")
                if not action:
                    logger.warning(
                        "[%s] Phase 5: parsed JSON had no 'action' field — "
                        "treating as unparseable",
                        agent.agent_id,
                    )
                    return

                if action == "skip":
                    agent.state.consecutive_phase5_skips += 1
                    logger.info(
                        "[%s] Phase 5: Agent chose to skip (streak: %d)",
                        agent.agent_id, agent.state.consecutive_phase5_skips,
                    )
                    return

                if not message_text:
                    logger.warning("[%s] Phase 5: No message text in response", agent.agent_id)
                    return

                # Real action — reset skip backoff. Capture the pre-reset value
                # first: several rejection paths below need the TRUE streak, not
                # the just-reset 0, to feed _select_agent's damping
                # (`skips >= 3`). Every remaining rejection path (unsupported
                # action, post-type rejection, body-mention rejection) restores it
                # correctly via `previous_skips + 1`.
                previous_skips = agent.state.consecutive_phase5_skips
                agent.state.consecutive_phase5_skips = 0
                agent.state.last_phase5_action_time = deps.time.time()

                channel = action_data.get("channel", "general").lstrip("#")
                post_type = action_data.get("post_type", "")

                # Retroactively add channel to the LLM log entry (unknown at call
                # time). Found by `llm_call_id`, NOT by buffer position — under
                # concurrency (two agents' Phase-5 turns interleaved on the same
                # event loop), another agent's own `_on_llm_call` can append its
                # row to this SHARED buffer in the gap between this call
                # returning and this line running, so `_llm_log_buffer[-1]` is
                # not reliably this call's row. Scanning from the tail is just an
                # optimization (our own row, being the most recent thing we
                # appended, is usually near the end); if it was already flushed
                # to the DB before we got here, skip silently — the row is still
                # a valid log entry without `channel`, and there's nothing to
                # retry.
                for _entry in reversed(self._llm_log_buffer):
                    if _entry.get("call_id") == llm_call_id:
                        _entry["channel"] = channel
                        break

                # Cross-cohort mention stripping now happens in _post_message, which
                # covers every outbound path instead of only this one. Phase 5 still
                # needs the *cleaned* text locally, though: the tagged_agent decision
                # below reads message_text.
                #
                # The JSON `post_type`/`tagged_agent` pair is not the only place a
                # disallowed mention can hide — a spoke can also name an
                # unreachable lab in PROSE with tagged_agent left null, which
                # layers 1-3 below wave through (a broadcast type addresses no one
                # by declaration). Recording whether THIS strip actually removed
                # something lets the new-post branch reject that case instead of
                # publishing a body with the mention silently deleted out from
                # under it — see the mutilation check below.
                #
                # This reads the count _strip_disallowed_tags returns for THIS call,
                # not a before/after delta on the shared self._cohort_tags_stripped
                # counter — under the two-lane scheduler another agent's concurrent
                # post can bump that counter between the "before" and "after" reads,
                # which used to make Phase 5 reject a perfectly clean post.
                message_text, this_call_stripped = self._strip_disallowed_tags(
                    message_text, agent
                )
                body_mention_was_stripped = this_call_stripped > 0

                if action != "new_post":
                    logger.info(
                        "[%s] Phase 5: unsupported action %r — skipping",
                        agent.agent_id, action,
                    )
                    agent.state.consecutive_phase5_skips = previous_skips + 1
                    return

                # New top-level post. Layers 1-3, against the SAME set that was
                # rendered into the prompt above. Reject rather than strip-and-
                # publish: a mention stripped out of an addressed post leaves a
                # dangling ask no one can answer (259 such posts, 0.8% reply
                # rate). WARNING, not DEBUG — the cohort strip was logged at
                # DEBUG and 200 of them produced no operator-visible signal.
                rejection = self._post_type_rejection(
                    agent,
                    post_type,
                    action_data.get("tagged_agent"),
                    available_types,
                )
                if rejection is not None:
                    logger.warning(
                        "[%s] Phase 5: rejected new post in #%s — %s",
                        agent.agent_id, channel, rejection,
                    )
                    agent.state.consecutive_phase5_skips = previous_skips + 1
                    return
                # Layer 1-3 judge the JSON declaration, but the mutilation this
                # whole gate exists to prevent is driven by the message BODY.
                # A broadcast type with tagged_agent=null sails through the
                # check above even when the body itself @-mentions an
                # unreachable lab in prose — and the strip above would then
                # publish the post with that mention silently deleted,
                # producing exactly the dangling-ask artifact (measured in
                # production: 42 of 259 posts named a lab in prose with no
                # tag). Reject instead of publishing a mutilated body.
                if body_mention_was_stripped:
                    logger.warning(
                        "[%s] Phase 5: rejected new post in #%s — the message "
                        "body @-mentions an agent this cohort gate cannot "
                        "reach; publishing it would silently delete that "
                        "mention rather than deliver it (post_type=%r)",
                        agent.agent_id, channel, post_type,
                    )
                    agent.state.consecutive_phase5_skips = previous_skips + 1
                    return
                # New top-level post
                posted = await self._post_message(agent.agent_id, channel, message_text)
                if not posted:
                    # _post_message already logged why (e.g. the text stripped to
                    # empty). Nothing reached Slack, so neither the turn counter
                    # nor an assessment row may be written for it — either would
                    # be a phantom record with no corresponding Slack message.
                    logger.info(
                        "[%s] Phase 5: New post in #%s suppressed — not counted, "
                        "nothing persisted",
                        agent.agent_id, channel,
                    )
                else:
                    agent.message_count += 1
                    self._proposals_posted += 1

                    # No post type reaching here ever carries an assessment
                    # sidecar anymore — the hub is hard-gated out of this
                    # function entirely (see the docstring), and CANONICAL has
                    # no entry for one (post_types.py). The extraction/persist
                    # step that used to live here for `opportunity_assessment`
                    # moved to `_reply_to_thread`'s Phase-4 CONCLUDE handling
                    # (Option A relocation).

                    # Check if it tags another agent
                    tagged_agent = action_data.get("tagged_agent")
                    if tagged_agent:
                        logger.info(
                            "[%s] Phase 5: New post in #%s tagging @%s",
                            agent.agent_id, channel, tagged_agent,
                        )
                    else:
                        logger.info(
                            "[%s] Phase 5: New post in #%s",
                            agent.agent_id, channel,
                        )

            except Exception as exc:
                logger.error("[%s] Phase 5 failed: %s", agent.agent_id, exc)

    async def _rehydrate_proposal_count(self) -> None:
        """Recover this run's pitch count from the durable store on resume.

        A pitch is a BOT ``agent_messages`` row with ``phase == 'new_post'``
        for this run — the only top-level post kind (the hub is gated out of
        Phase 5, ``pi_lab`` is the only other role, and the two other
        _post_message callers write 'thread_reply' or the panel-note phase).
        The ``is_bot``/``agent_id`` filters keep parity with the live counter,
        which only increments in ``_phase5_new_post``: they exclude the rare
        mirrored human top-level post (``agent_id`` NULL) the Slack poller can
        record in a seeded channel, and collab_private posts are excluded the
        same way ``_count_today_posts`` excludes them (they are flat
        refinement replies/handovers, not spam-prevention-relevant pitches).

        This restores the COUNT only. Whether that count can still gate
        anything depends on ``self.max_proposals`` being the same nonzero
        target the run opened under — main.py's ``_run_simulation`` is what
        keeps that true across a resume, by inheriting ``max_proposals`` from
        the run's stored config when the CLI value is 0. If ``max_proposals``
        is 0 here, the query is skipped entirely: there is nothing to gate.
        """
        if not self.session_factory or self.simulation_run_id is None:
            return
        if self.max_proposals <= 0:
            return
        from sqlalchemy import func, select

        from src.models import AgentMessage
        async with self.session_factory() as db:
            count = (
                await db.execute(
                    select(func.count(AgentMessage.id)).where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        AgentMessage.phase == "new_post",
                        AgentMessage.is_bot.is_(True),
                        AgentMessage.agent_id.isnot(None),
                        AgentMessage.visibility != VISIBILITY_COLLAB_PRIVATE,
                    )
                )
            ).scalar_one()
        self._proposals_posted = int(count or 0)
        logger.info("Rehydrated proposal count: %d pitch(es) this run", self._proposals_posted)

    def _open_interview_count(self) -> int:
        """Distinct interview threads still open across all agents. The hub is
        in every interview thread; a distinct-set is robust even if it is not."""
        open_ids: set[str] = set()
        for agent in self.agents.values():
            for tid, t in agent.state.active_threads.items():
                if t.status == "active":
                    open_ids.add(tid)
        return len(open_ids)

    def _proposal_target_drained(self) -> bool:
        """True once the pitch cap is reached and no interview has been open
        for PROPOSAL_DRAIN_SETTLE_TICKS consecutive checks. A METHOD, not a
        property, because it MUTATES ``_proposal_drain_streak`` — call it
        exactly once per loop iteration (the main-loop condition does)."""
        if self.max_proposals <= 0 or self._proposals_posted < self.max_proposals:
            self._proposal_drain_streak = 0
            return False
        if self._open_interview_count() > 0:
            self._proposal_drain_streak = 0
            return False
        self._proposal_drain_streak += 1
        return self._proposal_drain_streak >= PROPOSAL_DRAIN_SETTLE_TICKS

    def _roles_by_agent(self) -> dict[str, str]:
        """Live roster agent_id -> role. Agents absent from this map (e.g.
        ``grantbot``, which has cohort memberships but no AgentRegistry row and
        is a separate process, not an entry in self.agents) match no post type's
        ``targets``."""
        return {aid: a.role for aid, a in self.agents.items()}

    def _post_types_for_role(self, role: str) -> tuple[PostTypeSpec, ...]:
        """``load_role(role).post_types``, cached.

        load_role() reads TOML from disk on every call — the same reason
        _role_rate_cache exists (see _calls_per_load). This runs once per
        phase-5 turn per agent; the cache keeps it off the disk.

        NOT the same trade-off as the tool allow-list: ``./prompts`` is
        bind-mounted, and ``tools_for_role`` (src/agent/tools.py) re-reads
        ``role.toml`` fresh on every call, so a live edit to a role's ``tools``
        key takes effect immediately. This cache means the same edit to a
        role's ``post_types`` key does NOT — it is picked up only on the next
        process restart. That asymmetry is a known trade-off, not a bug: this
        runs once per phase-5 turn per agent, which is the hot path the cache
        exists for, and there is currently no invalidation hook for it.
        """
        cached = self._role_post_types_cache.get(role)
        if cached is None:
            cached = deps.load_role(role).post_types
            self._role_post_types_cache[role] = cached
        return cached

    # `_PANEL_REQUIRED_FOR` used to alias `specialists.PANEL_REQUIRED_FOR` here,
    # for call sites testing `recommendation in _PANEL_REQUIRED_FOR` directly.
    # All of them now ask `panel_is_owed`, which weighs the COMPUTED band as
    # well — the set alone answers a question this class no longer asks, and
    # leaving the alias in place would let a future call site quietly re-adopt
    # the abandoned rule. Removed 2026-08-22 with the last such site
    # (`_seed_consults_from_db`).

    def _available_post_types(self, agent: "Agent") -> tuple[PostTypeSpec, ...]:
        """Layer 1 ∩ layer 2: what this agent may post as a NEW top-level post.

        The SAME tuple is rendered into the prompt and used to judge the
        response, so the menu and the gate cannot disagree.

        Used to also take a ``restricted`` flag (the caller's
        ``blocked_for_regular``), forwarded to ``available_for`` as
        ``terminal_only`` so a blocked agent could still be offered a
        "reports finished work" type past the regular-work backpressure. That
        mechanism is gone along with the one post type it ever exempted (the
        hub's :mag: Opportunity Assessment — see post_types.py); a blocked
        caller now skips Phase 5 outright instead of calling in here at all
        (see ``_phase5_new_post``), so this always computes the unrestricted
        set.
        """
        return available_for(
            self._post_types_for_role(agent.role),
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
        )

    def _normalize_tagged_agent(self, tagged_agent: object) -> object:
        """Recover a ``tagged_agent`` that names a real agent by a near-miss
        spelling, before the membership tests in ``_post_type_rejection`` run.

        The menu line a model reads offers both forms adjacent — `` `blackbird`
        (@BlackbirdBot) `` — so "@blackbird", "BlackbirdBot", "Blackbird", and
        " blackbird" are all one slip away from the exact agent_id the gate
        compares against, and an exact-string mismatch used to reject and
        publish nothing for every one of them.

        Conservative on purpose: resolves to an agent_id that demonstrably
        exists on the live roster (``self.agents``) or a bot name that
        demonstrably resolves via ``self._bot_name_to_id`` — never guesses at
        one that doesn't. Anything else (including non-string input) passes
        through unchanged, so an unresolved or genuinely unreachable name is
        still rejected downstream exactly as before.
        """
        if not isinstance(tagged_agent, str):
            return tagged_agent
        candidate = tagged_agent.strip()
        if candidate.startswith("@"):
            candidate = candidate[1:]
        if candidate in self.agents:
            return candidate
        lowered = candidate.lower()
        if lowered in self.agents:
            return lowered
        resolved = self._bot_name_to_id.get(lowered)
        if resolved is not None:
            return resolved
        return tagged_agent  # unresolved — pass through; still rejected below

    def _post_type_rejection(
        self,
        agent: "Agent",
        post_type: str,
        tagged_agent: str | None,
        available: tuple[PostTypeSpec, ...],
    ) -> str | None:
        """Why this new top-level post must not be published, or None.

        Applies only to ``action: "new_post"`` — a reply is never gated here.

        Every rejection increments ``self._post_type_rejections[agent.agent_id]``
        (mirroring ``self._cohort_tags_stripped``), so a deployment where a
        role or model is having every post rejected is visible without
        grepping logs. Rejection messages always quote ``tagged_agent`` exactly
        as the model sent it — normalisation is for the membership tests only.
        """
        by_name = {s.name: s for s in available}

        def _reject(reason: str) -> str:
            self._post_type_rejections[agent.agent_id] = (
                self._post_type_rejections.get(agent.agent_id, 0) + 1
            )
            return reason

        spec = by_name.get(post_type)
        if spec is None:
            return _reject(
                f"post_type {post_type!r} is not available to role "
                f"{agent.role!r} with this topology "
                f"(available: {sorted(by_name) or 'none'})"
            )
        # Layers 2 and 3 are inert when the gate is off, so a mesh deployment's
        # behaviour is byte-identical after this change. Today a hallucinated
        # tagged_agent there is logged and the post ships; tightening that is a
        # separate decision, not a side effect of this one.
        if agent.allowed_sender_ids is None:
            return None
        # Normalise a near-miss spelling ("@blackbird", "BlackbirdBot", " blackbird")
        # onto the agent_id the membership tests below compare against. See
        # _normalize_tagged_agent's docstring — this never invents a target that
        # doesn't already resolve to a real agent.
        normalized_tag = self._normalize_tagged_agent(tagged_agent)
        allowed = eligible_targets(
            spec,
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
        )
        if not spec.targets:
            # A broadcast type addresses no one, so the tag is redundant — but
            # redundant is not wrong. The hub used to post its :mag: assessment
            # into the PI's own channel, where naming that PI was the natural
            # thing to do; rejecting it would destroy the artifact and the whole
            # interview behind it over a field nothing routes on. Ignore a
            # REACHABLE tag; an unreachable one is still the dangling-ask bug.
            if normalized_tag and normalized_tag not in agent.allowed_sender_ids:
                return _reject(
                    f"post_type {post_type!r} addresses no one and "
                    f"tagged_agent={tagged_agent!r} is not reachable"
                )
            return None
        if not normalized_tag:
            return _reject(
                f"post_type {post_type!r} must address one of "
                f"{sorted(allowed)}, but tagged_agent was null"
            )
        if normalized_tag not in allowed:
            return _reject(
                f"tagged_agent={tagged_agent!r} is not reachable for post_type "
                f"{post_type!r} (allowed: {sorted(allowed)})"
            )
        return None

    def _parse_phase5_response(self, response: str) -> tuple[dict | None, str | None]:
        """Parse Phase 5 response into (json_data, message_text).

        Expects JSON block + <slack_message> tags.  Uses the LAST JSON code
        block so that if the LLM revises its decision mid-response the final
        action wins.  Requires <slack_message> tags for the message body —
        raw text after the JSON block is never used (prevents reasoning leakage).

        The <assessment_json> sidecar is stripped from the source ONCE, up
        front, before either the fenced-block search or the raw-JSON fallback
        runs — not just the fallback. The sidecar is meant to be bare JSON
        with no fence (see _extract_assessment_json's docstring), but a model
        routinely wraps it in a ```json``` fence anyway despite the prompt's
        instructions. Since the sidecar is emitted LAST (after the action
        fence and the <slack_message> block), a fenced sidecar becomes the
        LAST ```json``` block in the raw response — exactly the one this
        method is looking for — and would silently hijack the action parse:
        the verdict dict gets treated as the action, `action` and `channel`
        fall back to defaults, and the real action is lost. Stripping first
        removes the sidecar (fence and all, since the tags wrap the fence)
        before the action search ever runs, so a fenced sidecar can no longer
        reach it.
        """
        data = None
        stripped = _strip_assessment_sidecar(response)
        try:
            # Find the LAST ```json``` block (LLM may revise mid-response).
            json_matches = list(
                re.finditer(r"```json\s*\n(.*?)\n```", stripped, re.DOTALL)
            )
            if json_matches:
                data = json.loads(json_matches[-1].group(1))
            else:
                # Try finding raw JSON in the same sidecar-stripped source —
                # without the strip, a sidecar-only response (no action fence
                # present at all) would be indistinguishable from the action
                # and get parsed as one, silently discarding a legitimate
                # Phase 5 turn.
                json_start = stripped.find("{")
                json_end = (
                    stripped.find("}", json_start) + 1
                    if json_start >= 0 else -1
                )
                if json_start >= 0 and json_end > json_start:
                    data = json.loads(stripped[json_start:json_end])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse Phase 5 JSON: %s", exc)

        if not data:
            return None, None

        # Extract message from <slack_message> tags (required — no raw-text fallback).
        # Anchor on the LAST tag pair so a prior mention of the tag name in the
        # LLM's reasoning (e.g. "my output is a single `<slack_message>` block")
        # does not pull reasoning into the captured body.
        last_close = response.rfind("</slack_message>")
        if last_close >= 0:
            last_open = response.rfind("<slack_message>", 0, last_close)
            if last_open >= 0:
                body = response[last_open + len("<slack_message>"):last_close].strip()
                return data, body

        return data, None

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
