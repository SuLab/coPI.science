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
from src.agent.end_reasons import FINALIZE, HOLD, TODAY, end_reason_class
from src.agent.engine import constants, deps
from src.agent.engine.channel_directory import ChannelDirectory
from src.agent.engine.context import EngineContext, RunState, _Via
from src.agent.engine.control import Control
from src.agent.engine.llm_log import LlmLog
from src.agent.engine.memory import Memory
from src.agent.engine.panel import Panel
from src.agent.engine.persistence import Persistence
from src.agent.engine.scheduler import Scheduler
from src.agent.engine.slack_io import SlackIO

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
from src.agent.roles import prompt_set_stamp
from src.agent.run_marker import (
    parse_announce_channels,
    render_run_start_announcement,
)
from src.agent.specialists import (
    domain_flatness_warning,
    panel_is_owed,
    signal_mix_report,
)
from src.agent.state import ThreadState
from src.agent.thread_guidance import CONCLUDE, phase4_guidance
from src.agent.tools import execute_tool, tools_for_role
from src.models import (
    AgentMessage,
    AssessmentDrop,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    Job,
    LlmCallLog,
    OpportunityAssessment,
    PromptChangeSuggestion,
    SimulationRun,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
from src.services.assessment_detail import (
    KEY_POINT_ACCEPTED_KEYS,
    LEGACY_KEY_POINT_GROUPS,
    RETIRED_KEY_POINT_GROUPS,
    key_point_shape,
    normalize_bullets,
    normalize_dimension_rationales,
    normalize_key_points,
)
from src.services.assessment_headline import (
    PITCH_DISPLAY_CHARS,
    PROJECT_DISPLAY_CHARS,
    _clip_at_sentence,
    render_assessment_headline,
)
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.cohorts import compute_gates, summarise_gates
from src.services.interview_state import ended_thread_ids
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
}

#: Owner-API names two units define under the same name (``headlines.enqueue()``,
#: ``memory.enqueue()``). They were never engine members, so nothing reaches them as
#: engine.<name>; callers use engine.<unit>.enqueue.
_NOT_FORWARDED = frozenset({"enqueue"})


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

        # Agent name lookups
        self._bot_name_to_id: dict[str, str] = {
            a.bot_name.lower(): a.agent_id for a in agents
        }
        self.message_log.set_bot_name_map(self._bot_name_to_id)

        # Closed thread IDs — prevents Phase 3 from re-activating decided threads
        self._closed_thread_ids: set[str] = set()

        # Threads whose interview has already produced a verdict, so a second
        # `<assessment_json>` sidecar on the same thread cannot become a second
        # `opportunity_assessments` row. Run 60c53424 wrote THREE rows for one
        # pearce interview and run 88d81cd8 wrote up to three per thread for
        # five different labs; see `_capture_hub_assessment` for the full
        # mechanism. A thread is recorded once its verdict is HELD — committed,
        # or queued on `_pending_assessments` for a retry that will still land
        # it.
        #
        # The VALUE (see `_HeldVerdict`) is what makes last-write-wins possible:
        # a same-turn re-capture is still refused as a duplicate, but a strictly
        # later reply that concludes or closes the interview supersedes a
        # provisional earlier verdict instead of being turned away by it — the
        # earlier row is then retired (`_retire_superseded_verdict`) so the
        # one-interview-one-assessment invariant still holds.
        #
        # Process-local on purpose. It is the same scope as the duplicates it
        # prevents (every observed one came from a single process). A restart
        # used to leave it empty, because `opportunity_assessments.slack_ts` is
        # the REPLY's ts and the table carried no thread_id of its own; since
        # migration 0036 it does, and `_rehydrate_assessed_threads` rebuilds
        # this map at startup. A row with a NULL `thread_id` (every pre-0036
        # row) still cannot be placed, so a restart mid-interview can still let
        # a second verdict through for one — but `max_thread_messages` closes a
        # thread the turn after it concludes, so there is normally no second
        # concluding turn to come back to.
        self._assessed_threads: dict[str, _HeldVerdict] = {}

        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}


        # Last-seen mtime of each agent's on-disk public profile file, keyed by
        # agent_id. The web editor runs in a separate process and writes
        # profiles/public/{id}.md on a shared volume; this process caches
        # profile content per Agent, so a per-turn mtime check tells us when an
        # external edit happened and the cache must be invalidated. See
        # _sync_profiles_from_disk.
        self._profile_mtimes: dict[str, float] = {}

        # --- Cohort gate bookkeeping (specs/cohort-system-v2.md) -------------
        # True once a recompute has actually applied a gate to at least one agent.
        self._cohort_gate_active: bool = False
        # Set to the preflight refusal reason while isolation is being forced off
        # (§5.3); None when clean. Surfaced on /admin/cohorts.
        self._cohort_preflight_error: str | None = None
        # Last logged (cohorts, memberships, gated, isolated) signature, so the
        # per-resync INFO line fires on change rather than every 30s.
        self._cohort_log_signature: tuple | None = None
        # Per-agent count of outbound @mentions stripped because the target was
        # outside the sender's cohort (§9). Exposed in the admin UI: a high rate
        # means the topology disagrees with what the agents want to do.
        self._cohort_tags_stripped: dict[str, int] = {}
        # Per-agent count of new-post rejections from _post_type_rejection —
        # unavailable post_type, missing/unreachable tagged_agent, or a
        # mutilated-mention reject. Mirrors _cohort_tags_stripped above: a
        # deployment where every pitch is rejected on (e.g.) a tagged_agent
        # spelling slip is otherwise only visible by grepping logs.
        self._post_type_rejections: dict[str, int] = {}

        # Last wall-clock time the AgentRegistry roster was re-synced (live
        # add/remove of agents as their status flips). See _sync_roster_from_db.
        self._last_roster_poll: float = 0.0

        # DB persistence buffer for OpportunityAssessment rows that failed
        # their first write attempt (e.g. a pool-checkout timeout) — queued
        # here by _persist_assessment instead of being dropped, and drained by
        # _flush_pending_assessments on the SAME per-turn cadence as
        # _pending_persist/_llm_log_buffer above (see _run_main_loop and
        # stop()), so the shutdown flush covers the last assessment of a run
        # too. This table is the actual product of the screening pipeline.
        self._pending_assessments: list[dict] = []
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

        # Every interview still holding an unannounced verdict is over: the run
        # is ending, so no later turn will ever conclude or supersede it. This
        # is the last chance to honour D12, and it must run AFTER the assessment
        # flush above — `_announce_owed_headline` reads the row back from the
        # database, so a verdict still sitting on `_pending_assessments` would
        # be invisible to it. No drain lock is needed here: `start()` awaits
        # `_run_main_loop()` directly in the same coroutine and `stop()` is only
        # awaited (from src/agent/main.py's finally-block) after `start()` has
        # returned, so this sweep can never run concurrently with the main
        # loop's own `_drain_and_flush`. Wrapped like the memory drain above:
        # anything escaping here must not skip the "Simulation stopping..."
        # line below, which docs/operations/host-and-simulation.md documents as the
        # operator's proof the buffers reached disk.
        try:
            # Seed from a DB query for exactly which interviews still owe a
            # headline, rather than from `_assessed_threads` alone.
            # `summary_posted_at IS NULL` is the exact, durable definition of
            # "still owed" — the same predicate `_announce_owed_headline`
            # itself reads before posting, so this seed and that guard can
            # never disagree. The in-memory map can disagree with both, in
            # either direction, and each direction costs something real:
            #
            # * announced=False over a row that IS stamped (a repair-script
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
            # `_rehydrate_assessed_threads` DOES now derive `announced` from
            # `summary_posted_at is not None` (it hardcoded False until this
            # branch), which narrows the first bullet but does not close it:
            # rows written before migration `0041` read NULL whether or not a
            # headline ever posted, so a resumed pre-`0041` run rehydrates them
            # all as owed and this query returns them all too. No seed can fix
            # that — the column is the only record there is — which is why
            # the `0041` box in docs/operations/migration-deploy-notes.md makes running the repair procedure a
            # precondition for resuming such a run.
            owed_thread_ids: list[str] | None = None
            # Owed interviews a HOLD end keeps back (spec P0-04): no
            # ThreadDecision, so still open. Announced on a resume or a finalize.
            held_open: list[str] = []
            if self.session_factory and self.simulation_run_id:
                from sqlalchemy import select as sa_select
                try:
                    async with self.session_factory() as db:
                        owed_thread_ids = list((await db.execute(
                            sa_select(OpportunityAssessment.thread_id)
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
                            held_open = [t for t in owed_thread_ids if t not in ended]
                            owed_thread_ids = [t for t in owed_thread_ids if t in ended]
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
            if owed_thread_ids is None and end_class == HOLD:
                # Without the database a HOLD cannot tell an ended interview
                # from an open one, and it must never announce an open one: it
                # holds every un-announced verdict instead of falling back.
                held_open = [
                    thread_id for thread_id, held in self._assessed_threads.items()
                    if not held.announced
                ]
                owed_thread_ids = []
                logger.error(
                    "HOLD end with an unreadable owed-headline query: holding all "
                    "%d un-announced verdict(s) rather than risk announcing an "
                    "open interview", len(held_open),
                )
            if owed_thread_ids is None:
                owed_thread_ids = [
                    thread_id for thread_id, held in self._assessed_threads.items()
                    if not held.announced
                ]
            for thread_id in owed_thread_ids:
                held = self._assessed_threads.get(thread_id)
                if held is not None and held.announced:
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

    async def _capture_hub_assessment(
        self, agent: Agent, thread: ThreadState, raw_response: str,
        slack_ts: str | None, *, closes_thread: bool,
    ) -> None:
        """Option A relocation: extract the hub's `<assessment_json>` verdict
        sidecar from its own raw Phase-4 CONCLUDE reply and persist it.

        ``closes_thread`` is whether the reply this sidecar rode in on ENDS the
        interview — the same ⏸️ decision ``_check_thread_outcome`` acts on
        moments later, hoisted in ``_reply_to_thread`` and passed down so both
        read one answer (see ``_reply_closes_thread``). Keyword-only and
        REQUIRED, with no default: a silent ``False`` here is exactly the bug
        this argument exists to fix, and every caller genuinely knows the
        answer.

        ``raw_response`` is the full LLM response from BEFORE
        ``_extract_slack_message`` discarded everything outside
        ``<slack_message>`` — the sidecar is written outside that block by
        design (see ``phase4-thread-reply.md``'s "Concluding with an
        Opportunity Assessment" section), so it was never in the text Slack
        actually received. (``_post_message`` also strips it unconditionally
        as a backstop regardless — see ``_strip_assessment_sidecar`` — so the
        sidecar cannot leak to Slack even if a model mistakenly wrote it
        inside the block instead.)

        Mirrors the two outcomes the old Phase-5 ``new_post`` handling of
        this same artifact logged (persisted / present-but-unusable), with
        one deliberate omission: Phase 5 only ever reached that code after
        the model explicitly declared ``post_type: "opportunity_
        assessment"``, so an absent sidecar there was a genuine anomaly
        worth a WARNING every time. Every Phase-4 reply runs through here
        regardless of whether it is the interview's concluding turn, and a
        sidecar is expected on at most 1 of every 12 — logging "no sidecar"
        on every ordinary interview turn would be pure noise, so that case is
        silent here. Only a sidecar tag that IS present but broken is
        anomalous.

        Never raises: a failure to extract or persist a verdict must not cost
        the reply that has already been posted to Slack by the time this
        runs (``_persist_assessment`` already self-guards its own DB write;
        this wraps the extraction step too, for the same reason).
        """
        try:
            verdict = _extract_assessment_json(raw_response)
            if verdict is not None:
                refusal = self._sidecar_refusal(
                    agent.role, thread, closes_thread=closes_thread,
                )
                if refusal is not None:
                    reason, detail = refusal
                    logger.warning(
                        "[%s] Phase 4: REFUSED an <assessment_json> sidecar for "
                        "%s on thread %s — %s. The reply is already in Slack; "
                        "the verdict is recorded as a drop, not stored.",
                        agent.agent_id, thread.other_agent_id or "?",
                        thread.thread_id, detail,
                    )
                    await self._record_assessment_drop(
                        agent.agent_id, reason,
                        subject_agent_id=thread.other_agent_id,
                        thread_id=thread.thread_id,
                        detail=detail,
                        # Keep the verdict itself. A refusal is a decision about
                        # WHERE this verdict belongs, never a licence to destroy
                        # it — see AssessmentDrop.raw_verdict.
                        raw_verdict=verdict,
                    )
                    return
                # Not refused. If the thread ALREADY holds a verdict, then by
                # construction this one supersedes it: `_sidecar_refusal` is the
                # only place that decision is made, and the only second verdict
                # it lets past is one from a strictly later reply that concludes
                # or closes the interview. Read before the write, because the
                # write overwrites this slot.
                superseded = self._assessed_threads.get(thread.thread_id)
                # The model is asked for `subject_agent_id` in the sidecar,
                # but unlike Phase 5's standalone post, a Phase-4 CONCLUDE
                # reply always has a real interview thread behind it — the PI
                # being screened is exactly `thread.other_agent_id`. Passed
                # as a fallback (not written into `verdict` itself) so
                # `raw_verdict` stays exactly what the model emitted — see
                # _persist_assessment's docstring.
                held, replacement_id = await self._persist_assessment(
                    agent.agent_id, thread.channel, verdict, slack_ts=slack_ts,
                    subject_agent_id_fallback=thread.other_agent_id,
                    thread=thread,
                )
                if held:
                    terminal = self._verdict_is_terminal(
                        agent.role, thread, closes_thread=closes_thread,
                    )
                    # Announce once per interview. `superseded.announced` carries
                    # forward because the earlier headline is already public and
                    # unretractable — a second one would describe a row that
                    # replaced a row nobody knew had been replaced.
                    already_announced = (
                        superseded.announced if superseded is not None else False
                    )
                    announce = terminal and not already_announced
                    self._assessed_threads[thread.thread_id] = _HeldVerdict(
                        ordinal=thread.message_count + 1,
                        # `final` is CLOSED, not merely concluding — see
                        # `_HeldVerdict`. A CONCLUDE ordinal can repeat.
                        final=closes_thread,
                        slack_ts=slack_ts,
                        announced=already_announced or announce,
                    )
                    # A queued replacement is never claimed at capture (spec
                    # P0-08, SA6-01). With `replacement_id is None` the verdict is
                    # only on `_pending_assessments`, so a by-thread claim here
                    # would stamp the SUPERSEDED sibling still in the database;
                    # the retire below deletes that row and its stamp, and a
                    # resume would announce again. The close path and the stop
                    # sweep (which runs after the final assessment flush) post it
                    # once the row exists.
                    queued_only = replacement_id is None and bool(
                        self.session_factory and self.simulation_run_id
                    )
                    if announce and queued_only:
                        self._assessed_threads[thread.thread_id] = (
                            self._assessed_threads[thread.thread_id]
                            ._replace(announced=False)
                        )
                        logger.warning(
                            "[%s] The verdict for %s was queued, not committed; its "
                            "#assessments-summary headline waits for the "
                            "interview's close or the shutdown sweep",
                            agent.agent_id, thread.other_agent_id or "?",
                        )
                    elif announce:
                        # Announce only a verdict that ends the interview. Since a
                        # provisional sidecar is now STORED rather than refused, a
                        # single interview can hold several in turn — and a
                        # headline is a public Slack post that cannot be
                        # retracted when the row it described is superseded
                        # moments later. Design D14 says a verdict that is not
                        # held never posts; the same logic says a verdict that is
                        # not final does not post YET.
                        outcome = await self._post_claimed_headline(
                            agent, thread, verdict, slack_ts,
                        )
                        if outcome == "failed":
                            # Definitely nothing reached Slack and the claim was
                            # released: leave the verdict discoverable to the
                            # close path, the shutdown sweep and the repair
                            # script. ("in_doubt" and "unclaimed" stay announced:
                            # re-posting either could duplicate a public post.)
                            self._assessed_threads[thread.thread_id] = (
                                self._assessed_threads[thread.thread_id]
                                ._replace(announced=False)
                            )
                            logger.warning(
                                "[%s] The #assessments-summary headline for %s "
                                "did not post; the verdict is stored and will be "
                                "retried when the interview ends",
                                agent.agent_id, thread.other_agent_id or "?",
                            )
                    elif not terminal:
                        logger.info(
                            "[%s] Provisional verdict stored for %s (message "
                            "ordinal %d); no #assessments-summary headline until "
                            "the interview concludes",
                            agent.agent_id, thread.other_agent_id or "?",
                            thread.message_count + 1,
                        )
                    # Retire the earlier row only once its replacement is
                    # actually HELD — never leave the interview with neither. The
                    # retire carries the retired rows' headline stamp and claim
                    # onto the replacement in the same transaction (spec P0-08),
                    # so an announced interview stays announced across a restart.
                    if superseded is not None:
                        await self._retire_superseded_verdict(
                            agent.agent_id, thread, superseded,
                            replacement_ordinal=thread.message_count + 1,
                            replacement_id=replacement_id,
                        )
            elif _ASSESSMENT_UNCLOSED_RE.search(raw_response or ""):
                # An <assessment_json> opening tag is present but
                # _extract_assessment_json found no usable verdict in it —
                # anomalous regardless of turn type, unlike plain absence.
                if _sidecar_has_valid_json_block(raw_response or ""):
                    logger.warning(
                        "[%s] Phase 4: concluding reply's <assessment_json> "
                        "sidecar parsed as valid JSON but was not an object "
                        "— verdict lost",
                        agent.agent_id,
                    )
                    drop_detail = "sidecar parsed as valid JSON but was not an object"
                else:
                    logger.warning(
                        "[%s] Phase 4: concluding reply's <assessment_json> "
                        "sidecar was present but unparseable — verdict lost",
                        agent.agent_id,
                    )
                    drop_detail = (
                        "sidecar present but unparseable (commonly a max_tokens "
                        "truncation that ate the closing tag)"
                    )
                await self._record_assessment_drop(
                    agent.agent_id,
                    "unparseable_sidecar",
                    subject_agent_id=thread.other_agent_id,
                    thread_id=thread.thread_id,
                    detail=drop_detail,
                )
        except Exception as exc:  # noqa: BLE001 — never lose a posted reply over this
            logger.error(
                "[%s] Failed to extract/persist the assessment sidecar for "
                "thread %s: %s",
                agent.agent_id, thread.thread_id, exc,
            )

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
        for queued in self._pending_assessments:
            if queued.get("thread_id") == thread_id:
                queued["summary_posted_at"] = now
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

    def _run_start_announcement_values(self) -> dict[str, str]:
        """The 12 template placeholders (run_marker.ANNOUNCEMENT_VALUE_KEYS).

        Every value is a plain pre-rendered string so an operator template
        needs no format specs. The git identity describes the IMAGE this
        process runs from (see src/services/build_info.py) — for the agent
        that is exactly the code executing, since src/ is baked at build.
        """
        started = self._start_time or deps.datetime.now(UTC)
        build = deps.get_build_info()
        hub_stamp = prompt_set_stamp("scout_hub")
        pi_stamp = prompt_set_stamp("pi_lab")
        if build.dirty_files is None:
            dirty = "dirty state unknown"
        elif build.dirty_files == 0:
            dirty = "clean"
        else:
            dirty = f"{build.dirty_files} uncommitted change(s) at image build"
        return {
            "run_id": str(self.simulation_run_id) if self.simulation_run_id
            else "unrecorded (--no-db)",
            "started_at": started.strftime("%Y-%m-%d %H:%M UTC"),
            "run_duration": (
                f"{self.max_runtime_minutes} minutes"
                if self.max_runtime_minutes > 0 else "indefinite (until stopped)"
            ),
            "git_commit": build.commit[:7] if build.commit else "unknown",
            "git_branch": build.branch or "unknown",
            "git_dirty": dirty,
            "hub_prompts_version": hub_stamp.version,
            "hub_prompts_hash": hub_stamp.content_hash,
            "pi_prompts_version": pi_stamp.version,
            "pi_prompts_hash": pi_stamp.content_hash,
            "rubric_version": RUBRIC_VERSION,
            "rubric_hash": RUBRIC_CONTENT_HASH,
        }

    async def _announce_overrides(self) -> tuple[str | None, str | None]:
        """Read the two DB-overridable announce settings in one session.

        Returns ``(channels, template_body)``: each is ``None`` when its
        ``app_settings`` row is absent or its value is NULL — the caller
        reads that as "no override, fall back to Settings / the template
        file". Only called by ``_announce_run_start`` when
        ``self.session_factory`` is set. This has its OWN try/except, distinct
        from ``_announce_run_start``'s outer one: a KV hiccup here must cost
        the announcement at most a fallback, not the channel-resolution and
        posting work still to come in that method — the same reasoning as
        ``_poll_control_plane``'s own try/except.
        """
        try:
            from sqlalchemy import select as sa_select

            from src.models import AppSetting

            keys = (
                "run_start_announce_channels",
                "run_start_announcement_template",
            )
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(AppSetting.key, AppSetting.value).where(
                        AppSetting.key.in_(keys)
                    )
                )).all()
            values = dict(rows)
            return (
                values.get("run_start_announce_channels"),
                values.get("run_start_announcement_template"),
            )
        except Exception as exc:
            logger.warning(
                "Run-start announcement: app_settings read failed (%s) — "
                "falling back to Settings/template-file defaults",
                exc,
            )
            return None, None

    async def _announce_run_start(self) -> None:
        """Post the run-start marker to every configured channel (fresh runs
        only — the caller gates on self._fresh_start).

        Best-effort end to end, same philosophy as _post_assessment_summary:
        nothing here may take down a run start. Refusals arrive as a falsy
        return from post_message, not as exceptions, so the return value is
        checked per channel. Posts with the hub's client (the engine's voice,
        and the identity with zero blast radius if the ingest sentinel ever
        regressed — see run_marker.py); falls back to any connected client
        with a WARNING. The markers post AFTER the fresh-start cursor seed,
        so the live poller WILL fetch them on its first tick — the sentinel
        skip (Task 4 of docs/plans/2026-08-29-run-start-announcements.md) is
        what drops them there and on every later resume.
        """
        try:
            names = parse_announce_channels(
                deps.get_settings().run_start_announce_channels
            )
            template_override: str | None = None
            if self.session_factory:
                channels_override, template_override = await self._announce_overrides()
                if channels_override is not None:
                    names = parse_announce_channels(channels_override)
            if not names:
                logger.info("Run-start announcement disabled (no channels configured)")
                return

            hub = next(
                (a for a in self.agents.values() if a.role == "scout_hub"), None
            )
            client = self.slack_clients.get(hub.agent_id) if hub else None
            if not client or not client.is_connected:
                fallback = self._next_poll_client()
                if fallback is None:
                    logger.info(
                        "Run-start announcement skipped: no connected Slack "
                        "client (Slack off or all tokens dead)"
                    )
                    return
                logger.warning(
                    "Run-start announcement: hub client unavailable — "
                    "falling back to [%s]'s client (marker will carry a "
                    "lab bot's identity)", fallback.agent_id,
                )
                client = fallback

            text = render_run_start_announcement(
                self._run_start_announcement_values(), template_override
            )
            posted: dict[str, str] = {}
            failed: list[str] = []
            try:
                for name in names:
                    # Both id resolution and the post itself sit inside this
                    # one per-channel try: a non-SlackApiError exception
                    # during get_channel_id (a network blip, say) must skip
                    # only THIS channel, not abort every channel still to
                    # come. ch_id stays None if resolution itself raised, so
                    # the "raised" WARNING below still has something to log.
                    ch_id = None
                    try:
                        ch_id = self._channel_id_map.get(name)
                        if not ch_id:
                            ch_id = await asyncio.to_thread(client.get_channel_id, name)
                        if not ch_id or ch_id.startswith("local:"):
                            logger.warning(
                                "Run-start announcement: cannot resolve #%s to "
                                "a real Slack channel (got %r) — skipping it",
                                name, ch_id,
                            )
                            failed.append(name)
                            continue
                        result = await client.apost_message(ch_id, text)
                    except Exception:  # noqa: BLE001 — per-channel isolation
                        logger.warning(
                            "Run-start announcement to #%s (%s) raised — skipping",
                            name, ch_id, exc_info=True,
                        )
                        failed.append(name)
                        continue
                    ts = (result or {}).get("ts")
                    if ts:
                        posted[name] = ts
                    else:
                        logger.warning(
                            "Run-start announcement to #%s (%s) was refused by "
                            "Slack — nothing posted there", name, ch_id,
                        )
                        failed.append(name)
            finally:
                # Runs even on a mid-loop escape (nothing above should escape
                # the per-channel try, but this is the recorded contract, not
                # a hope): whatever actually posted before any such escape is
                # still logged and written to the run row.
                logger.info(
                    "Run-start announcement: posted to %d channel(s)%s — %s",
                    len(posted),
                    f", {len(failed)} failed ({', '.join(failed)})" if failed else "",
                    ", ".join(posted) or "none",
                )
                await self._record_run_start_announcement(text, posted, failed)
        except Exception:
            logger.exception("Run-start announcement failed — continuing startup")

    async def _record_run_start_announcement(
        self, text: str, posted: dict[str, str], failed: list[str],
    ) -> None:
        """Durably record what was announced on the run row's config.

        Reassigns the whole dict rather than mutating: SimulationRun.config is
        a plain JSON column (src/models/agent_activity.py:65) with no mutation
        tracking, so an in-place update would silently not persist. Best-effort
        and never raises — the Slack posts already happened.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                run = (await db.execute(
                    sa_select(SimulationRun).where(
                        SimulationRun.id == self.simulation_run_id
                    )
                )).scalar_one_or_none()
                if run is None:
                    return
                run.config = {
                    **(run.config or {}),
                    "run_start_announcement": {
                        "at": deps.datetime.now(UTC).isoformat(),
                        "text": text,
                        "posted": posted,
                        "failed": failed,
                    },
                }
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — record is advisory
            logger.warning(
                "Could not record the run-start announcement on run %s: %s",
                self.simulation_run_id, exc,
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
          `announced` False and `summary_posted_at` NULL, precisely so this
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

        `summary_posted_at IS NULL` in the predicate is the at-most-once guard,
        and it is deliberately in the SQL rather than in Python: the close path
        and the shutdown sweep can both name the same thread, and a headline is
        a public post that cannot be retracted.

        Returns True when a headline actually reached Slack. Never raises.
        """
        held = self._assessed_threads.get(thread_id)
        if held is not None and held.announced:
            return False
        if not self.session_factory or not self.simulation_run_id:
            return False
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                row = (await db.execute(
                    sa_select(OpportunityAssessment)
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
            # `_assessed_threads`) AFTER the final `_flush_pending_assessments`.
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
            if outcome in ("in_doubt", "unclaimed") and held is not None:
                self._assessed_threads[thread_id] = held._replace(announced=True)
            return False

        if held is not None:
            self._assessed_threads[thread_id] = held._replace(announced=True)
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

    def _warn_if_hub_conclude_missing_assessment(
        self, agent: Agent, thread: ThreadState, response_text: str, raw_response: str,
    ) -> str | None:
        """Absent-sidecar detection gap: warn when a hub's structurally-
        concluding reply is neither a decline nor a persistable verdict.

        Returns the ``AssessmentDrop`` reason (``"missing_sidecar"``) when it
        warned, else ``None``. Kept synchronous — its callers include tests that
        invoke it directly — so the async call site does the recording.

        ``_capture_hub_assessment`` already warns when an ``<assessment_json>``
        tag is PRESENT but broken (unparseable, or valid JSON that is not an
        object) — it is deliberately silent when the tag is simply absent,
        because that is the ordinary case on every one of the ~11 non-
        concluding turns of an interview. That silence becomes a real gap at
        the one turn where thread_guidance.py's own CONCLUDE branch tells the
        hub it MUST either decline (⏸️) or close with an inline verdict that
        carries the sidecar (see ``_SCOUT_HUB[CONCLUDE]``) — a reply that does
        neither is a concluding, non-decline verdict that produced nothing
        persistable, and nothing upstream of this ever says so.

        Fires only when ALL THREE hold:
          (a) the thread is at its structural CONCLUDE point. Deliberately
              delegates to ``thread_guidance.phase4_guidance`` — the exact
              function that decided THIS reply's guidance — rather than
              re-deriving the cutoff from ``settings.max_thread_messages``:
              thread_guidance's CONCLUDE branch is a literal 12, not settings-
              derived, so the two can drift apart if ``max_thread_messages``
              is ever configured to anything else. Reading from
              thread_guidance itself keeps this check correct either way.
              Under the default settings this fires for a genuinely real
              reply: a thread with 11 existing messages passes the earlier
              system-enforced-close check (11 < 12), generates a reply at
              ordinal 12 -> CONCLUDE, and is inspected here — see
              ``Agent.build_phase4_prompt``'s ordinal-fix comment for why
              this was NOT true before that fix.
          (b) the posted reply does NOT open with the ⏸️ decline convention
              (see ``_reply_opens_with_pause``).
          (c) no ``<assessment_json>`` tag — well-formed or truncated — is
              present anywhere in the raw response. A present-but-broken tag
              is already covered by ``_capture_hub_assessment``'s own
              warnings above and must not double-warn here.

        Never raises and never persists anything itself — purely an
        observability signal for a case that otherwise leaves no trace at
        all: the reply already posted (this runs after `_post_message`
        succeeded) and no DB row was ever going to exist for it either way.
        """
        # +1: thread.message_count is the prior count; phase4_guidance's
        # contract is the ordinal of the reply just generated — the same
        # correction Agent.build_phase4_prompt applies for this same reply
        # (see that call site's comment for the full rationale).
        message_ordinal = thread.message_count + 1
        thread_phase, _, _ = phase4_guidance(agent.role, message_ordinal)
        if thread_phase != CONCLUDE:
            return None
        if _reply_opens_with_pause(response_text):
            return None
        if _ASSESSMENT_RE.search(raw_response or "") or _ASSESSMENT_UNCLOSED_RE.search(
            raw_response or ""
        ):
            return None
        logger.warning(
            "[%s] Phase 4: thread %s concluded (message_ordinal=%d) with a "
            "non-decline verdict but no persistable <assessment_json> "
            "sidecar was found",
            agent.agent_id, thread.thread_id, message_ordinal,
        )
        return "missing_sidecar"

    async def _persist_assessment(
        self, agent_id: str, channel: str, verdict: dict, slack_ts: str | None = None,
        *, subject_agent_id_fallback: str | None = None, thread: ThreadState | None = None,
    ) -> tuple[bool, uuid.UUID | None]:
        """Store a scouting verdict. Best-effort: a failure here must never cost
        the Slack post that already went out.

        Returns ``(held, assessment_id)``. ``held`` is whether the verdict is
        HELD — committed, or queued on ``_pending_assessments`` for a retry
        that will still land it. False means nothing was stored and nothing
        will be: the engine has no database (see ``__init__``).
        ``_capture_hub_assessment`` uses ``held`` to decide whether the
        thread has had its one verdict; a queued row counts, because letting a
        second verdict through while the first is still queued lands BOTH.

        ``assessment_id`` is the row's pre-generated primary key when the write
        actually committed, and ``None`` in the other two cases (no database;
        queued for a later retry) — a buffered row is not yet a real FK target,
        so ``_retire_superseded_verdict`` cannot re-point a superseded
        verdict's human-review rows onto it until a later flush lands it for
        real.

        ``slack_ts`` is the canonical post id ``_post_message`` returned for
        the post/reply the verdict came from (F7) — the row's link back to
        the Slack message it summarises. Optional and defaulted to ``None``
        so every existing direct caller (tests driving this method on a
        stub) keeps working unchanged.

        ``subject_agent_id_fallback``, when given, is AUTHORITATIVE for the
        ``subject_agent_id`` column and the specialist-floor check below — it
        overrides whatever the verdict itself named, because the engine knows
        the interview partner and the model has never been shown that id (see
        the inline note at the override). It is never written into
        ``raw_verdict``, which always stays exactly what the model emitted
        (see that field's own note below). Option A's caller
        (``_capture_hub_assessment``) passes ``thread.other_agent_id`` here:
        unlike Phase 5's old standalone post, a Phase-4 CONCLUDE reply always
        has a real interview thread behind it, so the engine always knows who
        the sidecar is about. The name is kept for its existing callers; it is
        a fallback only in the sense that callers without a thread omit it.

        ``thread``, when given, is passed straight through to
        ``_specialist_floor_gap`` for two things now. First, the fail-open
        decision, read from ``thread.floor_armed`` (latched once per turn, at
        the top of ``_reply_to_thread``, before this same turn's own consult
        calls or any other task's writes can reach it) instead of a live,
        process-global ``_specialist_consults`` read at this later point in
        the same turn — see that method's docstring, and
        ``ThreadState.floor_armed``'s own comment, for why a plain live read
        here is unsafe under concurrency. Second, ``thread.thread_id``, which
        now joins the consult record together with ``subject_agent_id`` — see
        ``_specialist_floor_gap``'s docstring for why a PI-only join let a
        PI's second interview inherit the first one's panel. There is no
        separate ``thread_id`` parameter here because ``thread`` already
        carries it: every caller with a real interview (Option A's
        ``_capture_hub_assessment``, above) passes ``thread`` for exactly this
        reason, and a caller with none — direct callers, all pre-existing
        tests — omits it and joins against the ``None``-keyed slot instead.

        The weighted score and band are computed here from the verdict's own
        dimension scores, never taken from the model's ``weighted_score`` field
        — the model is instructed to leave that at 0 but will sometimes fill in
        a flattering number anyway. ``recommendation`` (the model's judgement,
        which can legitimately be "route-to-incubation" — a value band() can
        never produce) and the computed ``band`` are kept in separate columns
        and neither ever overwrites the other. The verdict exactly as emitted
        is kept verbatim in ``raw_verdict`` regardless of what could be parsed
        out of it (and regardless of ``subject_agent_id_fallback``), so
        nothing is ever lost to — or invented by — a decision made here.
        """
        # A view with the engine-known subject applied, used ONLY for the
        # subject-derived column and the specialist-floor check — never for
        # `raw_verdict`, which must stay byte-for-byte what the model sent.
        #
        # This OVERRIDES the model's own field rather than only filling a blank
        # one. The phase-4 prompt never shows the hub a PI's `agent_id`: it gets
        # `{other_agent_name}` (bot_name, "WangBot") and `{other_agent_lab}`
        # (pi_name), so it can only guess, and it guesses what it was shown.
        # Consults are recorded under the real `agent_id`, so a guessed
        # "WangBot" made _specialist_floor_gap join against a key that never
        # exists, refuse the verdict, and discard it — after the concluding
        # reply had already gone out, with no later turn to recover it. The
        # caller's value is ground truth (`thread.other_agent_id`: an interview
        # is 1:1 with the hub), so it wins.
        subject_view = verdict
        if subject_agent_id_fallback:
            subject_view = {**verdict, "subject_agent_id": subject_agent_id_fallback}

        # Before the two floor questions below, give the in-memory record a
        # chance to be rehydrated from `specialist_consults` — otherwise a
        # restart mid-interview makes every verdict that follows it permanently
        # UNVERIFIABLE, whatever the panel actually did. Additive, narrow and
        # non-raising; see `_seed_consults_from_db` for why it cannot launder an
        # unconsulted domain into a consulted one.
        await self._seed_consults_from_db(subject_view, thread)

        gap = self._specialist_floor_gap(subject_view, thread=thread)
        # An empty `gap` is two different findings, and the row has to say
        # which: the panel really was complete, or the floor had nothing to
        # check it against (no subject to join on, or a process that has
        # recorded no consult for anyone — the ordinary post-restart state,
        # and production's last exit was a SIGKILL). See `_floor_verifiable`.
        floor_verifiable = self._floor_verifiable(subject_view, thread=thread)
        if gap:
            logger.warning(
                "[%s] Assessment for %s stored with an INCOMPLETE PANEL — "
                "recommendation %r required the %s specialist(s), never "
                "consulted during the interview. The verdict is flagged, not "
                "discarded: this check runs after the concluding reply is "
                "already in Slack, so refusing it left the PI told and "
                "Blackbird holding nothing.",
                agent_id, subject_view.get("subject_agent_id") or "?",
                verdict.get("recommendation"), ", ".join(sorted(gap)),
            )

        # The engine can run without a database (see __init__) — in that mode
        # this is a silent no-op, matching every other run-scoped write in
        # this class (e.g. _close_thread above, :2823 in the class).
        if not self.session_factory or not self.simulation_run_id:
            logger.debug(
                "[%s] Skipping assessment persistence — no database configured",
                agent_id,
            )
            return False, None

        scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
        computed_score, computed_band = self._computed_score_and_band(verdict)
        # Was a panel OWED here, as judged right now, by the same predicate the
        # floor above just used? Computed once and stored, rather than left for
        # the admin page to re-derive at render time — which is what it used to
        # do, and which silently relabels every older row each time the
        # predicate widens (twice in 2026-08 alone: 12 production rows written
        # by the recommendation-only floor rendered a green "panel verified" box
        # for a floor that never ran). See OpportunityAssessment.panel_owed and
        # src/services/assessment_detail.panel_state.
        #
        # `verdict`, not `subject_view`: the two differ only in
        # `subject_agent_id`, and neither field this reads comes from there.
        panel_owed = panel_is_owed(verdict.get("recommendation"), computed_band)
        gating = _normalize_gating(verdict.get("gating"))
        red_flags = verdict.get("red_flags")
        milestones = verdict.get("suggested_derisking_milestones")
        key_points = verdict.get("key_points")
        # subject_agent_id/funnel_stage/recommendation/confidence are bounded
        # VARCHAR columns (see src/models/opportunity.py); every other field
        # degrades per-field on a bad value (wrong type -> None), but a
        # too-long *string* in one of these four is still the right type and
        # would sail past an isinstance check straight into a DataError at
        # commit — which the outer except then drops the WHOLE row for. Clip
        # instead of dropping: a truncated recommendation is still useful for
        # triage, an absent one is not.
        subject_agent_id = _bounded_str(subject_view.get("subject_agent_id"), 50)
        funnel_stage = _bounded_str(verdict.get("funnel_stage"), 20)
        recommendation = _bounded_str(verdict.get("recommendation"), 30)
        confidence = _bounded_str(verdict.get("confidence"), 20)
        # Shape checks are WARNINGS, never drops (A4). Nothing here can verify
        # that a headline tells the whole story — only that it is the shape the
        # contract asks for. A verdict that misses the shape is still the
        # archive's copy of that verdict.
        if isinstance(verdict.get("headline"), str) and len(
            verdict["headline"]
        ) > _HEADLINE_SOFT_LIMIT:
            logger.warning(
                "[%s] Assessment headline is %d chars (contract asks for <=%d): %s",
                agent_id, len(verdict["headline"]), _HEADLINE_SOFT_LIMIT,
                verdict["headline"][:80],
            )
        if isinstance(verdict.get("company_or_project"), str) and len(
            verdict["company_or_project"]
        ) > _PROJECT_SOFT_LIMIT:
            logger.warning(
                "[%s] Assessment company_or_project is %d chars (contract asks "
                "for <=%d); the #assessments-summary headline clips it at %d: %s",
                agent_id, len(verdict["company_or_project"]), _PROJECT_SOFT_LIMIT,
                PROJECT_DISPLAY_CHARS, verdict["company_or_project"][:80],
            )
        if isinstance(verdict.get("elevator_pitch"), str) and len(
            verdict["elevator_pitch"].split()
        ) > _PITCH_WORD_LIMIT:
            logger.warning(
                "[%s] Assessment elevator_pitch is %d words (contract asks for <=%d)",
                agent_id, len(verdict["elevator_pitch"].split()), _PITCH_WORD_LIMIT,
            )
        # The scout_hub 1.7.0 citation budget's ONLY runtime alarm. Item 8 moved
        # the provenance citation from sentence two to sentence four and asks
        # that sentences 1-4 END within ~550 chars, so the citation completes
        # inside the 600 that `#assessments-summary` publishes. Nothing else
        # checks that: a within-bound pitch whose sentence four ends at 640 stores
        # clean, warns nothing, and publishes a citation-free excerpt to a
        # channel the post cannot be retracted from. Compare `_HEADLINE_SOFT_LIMIT`
        # — the same class of write-path drift alarm, for the same reason.
        # Cheap and total: reuse the real clipper rather than re-deriving the
        # boundary, so this can never disagree with what actually posts.
        _pitch = verdict.get("elevator_pitch")
        if isinstance(_pitch, str) and _pitch:
            # Compare citation SETS, not "is there any citation left". A pitch
            # whose element 1 carries a trial registry URL that survives the
            # cut would otherwise mask the sentence-four DOI that did not —
            # which is the only case this alarm exists for.
            _cited = set(_PITCH_CITATION_RE.findall(_pitch))
            if _cited:
                _excerpt = _clip_at_sentence(_pitch, PITCH_DISPLAY_CHARS) or ""
                _lost = _cited - set(_PITCH_CITATION_RE.findall(_excerpt))
                if _lost:
                    logger.warning(
                        "[%s] Assessment elevator_pitch cites %d source(s) the "
                        "#assessments-summary excerpt (first %d chars, cut at a "
                        "sentence boundary) does not carry: %s. Sentences 1-4 "
                        "must END within ~550 chars; the published pitch will "
                        "be missing this provenance.",
                        agent_id, len(_lost), PITCH_DISPLAY_CHARS,
                        ", ".join(sorted(_lost))[:200],
                    )
        # Normalize ONCE, and run the soft-bound checks below against what
        # will actually be STORED (blank bullets stripped), not the raw
        # sidecar — otherwise `["x", "  "]` passes a two-bullet count check
        # and stores one bullet. The raw value is inspected only when
        # normalization rejected it (the field is then stored NULL).
        normalized_key_points = normalize_key_points(key_points)
        checked_key_points = (
            normalized_key_points if normalized_key_points is not None else key_points
        )
        if isinstance(checked_key_points, list) and not (
            _KEY_POINTS_MIN <= len(checked_key_points) <= _KEY_POINTS_MAX
        ):
            logger.warning(
                "[%s] Assessment carries %d key_points (contract asks for %d-%d)",
                agent_id, len(checked_key_points), _KEY_POINTS_MIN, _KEY_POINTS_MAX,
            )
        elif isinstance(key_points, dict):
            # The whole field is about to be DROPPED (stored NULL, kept only in
            # `raw_verdict`) for any dict `normalize_key_points` rejects: an
            # empty object, an unknown group key, a null group value, or a
            # value that is not a list of strings. The reason names which.
            if normalized_key_points is None:
                unknown = sorted(set(key_points) - KEY_POINT_ACCEPTED_KEYS)
                if not key_points:
                    reason = "an empty object"
                elif unknown:
                    reason = f"unknown group key(s) {unknown}"
                elif any(v is None for v in key_points.values()):
                    reason = "a group value is null"
                else:
                    reason = "a group value is not a list of strings"
                logger.warning(
                    "[%s] Assessment key_points was DROPPED (stored NULL; the "
                    "value survives only in raw_verdict): %s. Keys present: %s",
                    agent_id, reason, sorted(key_points),
                )
            shape = key_point_shape(checked_key_points)
            retired_keys = {k for k, _ in RETIRED_KEY_POINT_GROUPS}
            # Retired keys are subtracted here too: `key_questions` is both a
            # legacy (1.3.0-1.7.1) and a retired (1.8.0) group name, and it was
            # never reported as "pre-1.8.0" while 1.8.0 was current — the
            # retired-key warning below is the one that names it now.
            legacy_only = sorted(
                set(checked_key_points)
                & (
                    {k for k, _ in LEGACY_KEY_POINT_GROUPS}
                    - set(_KEY_POINT_GROUP_BULLETS)
                    - retired_keys
                )
            )
            if shape in ("legacy", "mixed"):
                # A stale prompt (scout_hub < 1.8.0) on this image: stored and
                # rendered under the legacy labels, never dropped — but the
                # per-group checks below describe the CURRENT contract, so a
                # legacy object gets this one warning instead of "omits 4 of 6".
                logger.warning(
                    "[%s] Assessment key_points uses pre-1.8.0 group name(s) %s; "
                    "stored and rendered under the legacy labels. Is "
                    "prompts/roles/scout_hub at 1.9.0 on this host?",
                    agent_id, legacy_only,
                )
            if shape in ("current", "mixed"):
                # Its OWN site, not the legacy branch above: a retired key is
                # evidence of neither shape (`key_point_shape`), so a 1.8.0
                # sidecar classifies "current" and never reaches that branch.
                # Milder than the legacy warning (§6.4 of
                # docs/specs/2026-09-28-assessment-chat-entry-and-key-points-design.md)
                # — the group still stores and renders under the label and in the
                # slot 1.8.0 gave it — so it carries no stale-prompt question; that
                # hint stays on the genuinely pre-1.8.0 path above.
                retired = sorted(set(checked_key_points) & retired_keys)
                if retired:
                    logger.warning(
                        "[%s] Assessment key_points carries group(s) %s retired "
                        "as of scout_hub 1.9.0; stored and rendered under the "
                        "1.8.0 label",
                        agent_id, retired,
                    )
                for group_key, expected in _KEY_POINT_GROUP_BULLETS.items():
                    group = checked_key_points.get(group_key)
                    if isinstance(group, list) and len(group) != expected:
                        logger.warning(
                            "[%s] Assessment key_points.%s carries %d bullets "
                            "(contract asks for %d)",
                            agent_id, group_key, len(group), expected,
                        )
                    if isinstance(group, list):
                        for bullet in group:
                            if isinstance(bullet, str) and len(bullet) > _KEY_POINT_BULLET_CHARS:
                                logger.warning(
                                    "[%s] Assessment key_points.%s has a %d-char "
                                    "bullet (contract asks for at most %d)",
                                    agent_id, group_key, len(bullet),
                                    _KEY_POINT_BULLET_CHARS,
                                )
                # An ABSENT group is `None` and fails the isinstance above, so
                # the count check cannot see it; a partial object still stores
                # (normalize accepts a subset), so name the omission here.
                absent = [
                    k for k in _KEY_POINT_GROUP_BULLETS if k not in checked_key_points
                ]
                if absent:
                    logger.warning(
                        "[%s] Assessment key_points omits %d of %d groups: %s",
                        agent_id, len(absent), len(_KEY_POINT_GROUP_BULLETS),
                        ", ".join(absent),
                    )
        # Sidecar items 11/12 (0049) and 13/14 (0050): strengths, risks,
        # competitive_landscape, evidence_maturity. Same soft-bound policy as
        # key_points above — a shape violation is warned about, never a drop
        # by itself. `normalize_bullets` is the only thing that drops the
        # whole field, and only for a genuine type violation (A20).
        for _field_name in (
            "strengths", "risks", "competitive_landscape", "evidence_maturity",
        ):
            _raw_bullets = verdict.get(_field_name)
            if isinstance(_raw_bullets, list):
                if not (_HUB_BULLETS_MIN <= len(_raw_bullets) <= _HUB_BULLETS_MAX):
                    logger.warning(
                        "[%s] Assessment %s carries %d bullets (contract asks for %d-%d)",
                        agent_id, _field_name, len(_raw_bullets),
                        _HUB_BULLETS_MIN, _HUB_BULLETS_MAX,
                    )
                for _bullet in _raw_bullets:
                    if isinstance(_bullet, str) and len(_bullet) > _HUB_BULLET_CHARS:
                        logger.warning(
                            "[%s] Assessment %s bullet is %d chars (contract asks for <=%d): %s",
                            agent_id, _field_name, len(_bullet), _HUB_BULLET_CHARS,
                            _bullet[:80],
                        )
                if normalize_bullets(_raw_bullets) is None:
                    logger.warning(
                        "[%s] Assessment %s was DROPPED (stored NULL; the value "
                        "survives only in raw_verdict): not a non-empty list of "
                        "non-blank strings",
                        agent_id, _field_name,
                    )
            elif _raw_bullets is not None:
                logger.warning(
                    "[%s] Assessment %s was DROPPED (stored NULL; the value "
                    "survives only in raw_verdict): not a list",
                    agent_id, _field_name,
                )
        # Sidecar item 2's companion (0052). Three warnings, no drops beyond
        # what the normalizer already refuses: an over-long sentence, a
        # malformed map, and a dimension that was SCORED but not explained —
        # the last is the one a reader of the Evidence summary actually feels,
        # because the row renders a score with no reason beside it.
        _raw_rationales = verdict.get("dimension_rationales")
        _rationales = normalize_dimension_rationales(_raw_rationales)
        # A map whose every value is blank (the skeleton left unfilled) holds
        # no reasons rather than a malformed one: it stores NULL like a drop,
        # but the per-dimension warning below names every gap, so it is not
        # also reported as a DROPPED field.
        _all_blank = isinstance(_raw_rationales, dict) and bool(_raw_rationales) and all(
            v is None or (isinstance(v, str) and not v.strip())
            for v in _raw_rationales.values()
        )
        if _raw_rationales is not None and _rationales is None and not _all_blank:
            logger.warning(
                "[%s] Assessment dimension_rationales was DROPPED (stored NULL; "
                "the value survives only in raw_verdict): not a non-empty map of "
                "dimension key to non-blank sentence",
                agent_id,
            )
        # Two keys that normalize to one dimension (`Venture_Potential` and
        # `venture_potential`) keep only the later reason; say so rather than
        # losing one silently.
        if _rationales and isinstance(_raw_rationales, dict):
            _slugs = [
                k.strip().lower() for k, v in _raw_rationales.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            ]
            _collided = sorted({s for s in _slugs if _slugs.count(s) > 1})
            if _collided:
                logger.warning(
                    "[%s] Assessment dimension_rationales has keys that collide "
                    "after lower-casing (%s); only the last reason for each is stored",
                    agent_id, ", ".join(_collided),
                )
        for _key, _text in (_rationales or {}).items():
            if len(_text) > _DIMENSION_RATIONALE_CHARS:
                logger.warning(
                    "[%s] Assessment dimension_rationales.%s is %d chars "
                    "(contract asks for <=%d)",
                    agent_id, _key, len(_text), _DIMENSION_RATIONALE_CHARS,
                )
        # Keys compared after the same `.strip().lower()` the normalizer and
        # the read path apply, so a differently-cased score key is not
        # reported as unexplained when its rationale will in fact render. A
        # score counts as SCORED here exactly when the read path's
        # `_score_value` would render it — a real number, never a bool — so the
        # warning never names a dimension the page does not show.
        _unexplained = sorted(
            k for k, v in scores.items()
            if isinstance(k, str)
            and isinstance(v, (int, float)) and not isinstance(v, bool)
            and k.strip().lower() not in (_rationales or {})
        )
        if _unexplained:
            logger.warning(
                "[%s] Assessment has %d scored dimension(s) with no rationale: %s",
                agent_id, len(_unexplained), ", ".join(_unexplained),
            )
        # Built once, up front, so a failed first attempt has a plain dict —
        # not a session-bound ORM instance — ready to hand straight to
        # _pending_assessments for a later retry.
        assessment_kwargs = dict(
            simulation_run_id=self.simulation_run_id,
            agent_id=agent_id,
            subject_agent_id=subject_agent_id,
            # Bounded like its four siblings above. `channel_name` is
            # String(100) NOT NULL and was passed raw, so an over-long channel
            # name was the one string field left able to DataError the whole row
            # out of existence. `or ""` because the column is NOT NULL and
            # `_bounded_str` answers None for a non-string / empty value: an
            # empty channel name is a degraded row, a missing verdict is a lost
            # one.
            channel_name=_bounded_str(channel, 100) or "",
            slack_ts=slack_ts,
            # The interview this verdict came out of. NULL for a caller with no
            # thread (direct callers, pre-existing tests) — never "", which
            # `WHERE thread_id IS NULL` would not match and which would collide
            # across interviews. It is what lets a restarted process rehydrate
            # `_assessed_threads` (see `_rehydrate_assessed_threads`) instead of
            # treating the interview's own concluding verdict as a first one.
            thread_id=(thread.thread_id if thread is not None else None) or None,
            company_or_project=_str_or_none(verdict.get("company_or_project")),
            # Sidecar items 6-8 (2026-09-09): the reviewer-facing narrative.
            # `company_or_project` above stays the short label — these three are
            # what the assessment pages lead with. Each degrades exactly like
            # its existing siblings: a wrong type becomes None and `raw_verdict`
            # keeps the original, because a malformed narrative field must never
            # cost the verdict (A20).
            headline=_str_or_none(verdict.get("headline")),
            key_points=normalized_key_points,
            elevator_pitch=_str_or_none(verdict.get("elevator_pitch")),
            # Sidecar item 10 (0048): why the dimension scores came out where
            # they did. App-only by design (D3) — the six-field
            # #assessments-summary headline never renders it, which is the
            # whole reason it is a column of its own rather than more pitch.
            # Degrades exactly like its narrative siblings above.
            score_rationale=_str_or_none(verdict.get("score_rationale")),
            # Sidecar item 2's companion (0052): the per-dimension reasons.
            # Degrades to None on a wrong shape like its narrative siblings;
            # raw_verdict keeps the original either way.
            dimension_rationales=_rationales,
            # Sidecar items 11/12 (0049): the hub's own strengths/risks
            # bullets. Degrades to None on a wrong type like its narrative
            # siblings above; raw_verdict keeps the original either way.
            strengths=normalize_bullets(verdict.get("strengths")),
            risks=normalize_bullets(verdict.get("risks")),
            # Sidecar items 13/14 (0050): the competitor set with stages, and the
            # per-axis statement of what is settled. Degrade to None on a wrong
            # type like their narrative siblings above; raw_verdict keeps the
            # original either way.
            competitive_landscape=normalize_bullets(
                verdict.get("competitive_landscape")
            ),
            evidence_maturity=normalize_bullets(verdict.get("evidence_maturity")),
            funnel_stage=funnel_stage,
            recommendation=recommendation,
            confidence=confidence,
            weighted_score=computed_score,
            band=computed_band,
            gating=gating,
            scores=scores or None,
            red_flags=red_flags if isinstance(red_flags, list) else None,
            derisking_milestones=(
                milestones if isinstance(milestones, list) else None
            ),
            rationale=_str_or_none(verdict.get("rationale")),
            # Sidecar item 5 (rubric v2.1.0): the single experiment Blackbird
            # should fund next. Degrades to None on a wrong type like its Text
            # siblings; raw_verdict keeps the original either way.
            recommended_next_experiment=_str_or_none(
                verdict.get("recommended_next_experiment")
            ),
            raw_verdict=verdict,
            # WHICH rubric produced this row. The weights, thresholds and
            # prompt text all come from one document now
            # (prompts/rubric/blackbird-rubric.toml, loaded once per process),
            # so a score is only comparable to another score written under the
            # same version — and the content hash catches an edit that shipped
            # without a version bump. Stamped from the module-level constants:
            # the rubric cannot change under a running process, so these are
            # the same values the startup banner reported.
            rubric_version=RUBRIC_VERSION,
            rubric_content_hash=RUBRIC_CONTENT_HASH,
            panel_incomplete=bool(gap),
            # Three states, one column — see OpportunityAssessment.missing_domains:
            #   [names] a real gap, these domains were owed and never consulted
            #   NULL    NO GAP RECORDED — read this column ALONE and that is all
            #           it says. It covers BOTH "a floor evaluated this verdict
            #           and found nothing owed and unconsulted" and "no floor
            #           ran on it at all", and only `panel_owed` below (written
            #           two lines from here) separates them. This comment used
            #           to call NULL "VERIFIED complete (or none was owed)",
            #           which is the exact reading `panel_owed` exists to end:
            #           12 production rows written by a floor that EXEMPTED them
            #           were later re-read as completed audits, at least five
            #           with a demonstrable gap.
            #   []      the floor could not be checked at all; this row is
            #           UNVERIFIED, and must not be counted as a clean panel
            # `panel_incomplete` stays False for [] on purpose: we have no
            # evidence of a gap, only an inability to look. The distinction is
            # what keeps spec §10's panel-gap surface from reading every
            # post-restart verdict as a vetted one.
            missing_domains=sorted(gap) if gap else (None if floor_verifiable else []),
            # The fourth state `missing_domains` alone cannot express. NULL there
            # means "no gap recorded", which is a verification only if a floor
            # ran at all — and that answer belongs to the moment of the write,
            # not to whatever the predicate says on the day someone opens the
            # page. Deliberately NOT nullable-by-omission: every row this method
            # writes states a real boolean, so NULL in the column means exactly
            # "written before 0036" (or backfilled, or hand-built by a test).
            panel_owed=panel_owed,
            # Ships together with the phase4 prompt instruction to write
            # `rationale`/`recommended_next_experiment` as Markdown: the stamp
            # is what gates rendering, so a new row is always marked, never
            # inferred from content. See OpportunityAssessment.prose_format.
            prose_format="markdown",
        )
        # Pre-generated rather than left to the column's Python-side default:
        # a buffered row (queued below on a failed first attempt) now carries a
        # FIXED id from the moment it is built. Deliberate retry-semantics
        # change: a retry whose PREVIOUS attempt actually reached the server
        # before failing (e.g. the commit succeeded but the ack was lost) now
        # surfaces as a PK violation -> an `unwritable_row` drop, instead of
        # silently landing a second row under a fresh id and duplicating the
        # verdict — an improvement on this, the engine's most protected write
        # path. It is also what lets `_retire_superseded_verdict` re-point a
        # superseded verdict's human-review rows onto the row that will hold
        # this verdict once it actually commits.
        assessment_kwargs["id"] = uuid.uuid4()
        try:
            async with self.session_factory() as db:
                db.add(OpportunityAssessment(**assessment_kwargs))
                await db.commit()
            logger.info(
                "[%s] Assessment stored: %s -> %s (%s, %s)",
                agent_id, subject_agent_id or "?",
                recommendation or "?", computed_score, computed_band,
            )
            return True, assessment_kwargs["id"]
        except Exception as exc:  # noqa: BLE001 — never lose a posted assessment
            # This row is the actual product of the screening pipeline, and
            # unlike _close_thread/_record_assessment_drop it is fully built
            # before this point with nothing else in-process reading it back
            # immediately — the same shape as _pending_persist/
            # _llm_log_buffer. Queue it for retry (drained by
            # _flush_pending_assessments on the same cadence as those two —
            # see _run_main_loop and stop()) instead of dropping it. Still
            # loud: a pool-checkout timeout on the FIRST attempt is worth an
            # ERROR + traceback even though it is now recoverable, so an
            # operator sees the pool pressure immediately rather than only if
            # the retry also fails.
            self._pending_assessments.append(assessment_kwargs)
            logger.error(
                "[%s] Failed to persist assessment on first attempt, queued "
                "for retry: %s",
                agent_id, exc, exc_info=True,
            )
            return True, None

    @staticmethod
    def _verdict_is_terminal(
        role: str, thread: ThreadState, *, closes_thread: bool
    ) -> bool:
        """Is this reply the LAST word the interview will get?

        True when the reply closes the thread (⏸️) or is the turn the guidance
        asks to conclude on. Two consequences, and they must agree, which is why
        both callers ask this one function: a terminal verdict marks
        ``_HeldVerdict.final`` so nothing later can re-capture it, and it is the
        only thing that releases the public ``#assessments-summary`` headline.

        NOT an admission test. ``_sidecar_refusal`` deliberately no longer asks
        it — a non-terminal sidecar is stored as provisional rather than
        destroyed. Announcing one, though, is not reversible: the headline goes
        to a Slack channel and cannot be retracted when a later turn supersedes
        the row it described.
        """
        thread_phase, _, _ = phase4_guidance(role, thread.message_count + 1)
        return closes_thread or thread_phase == CONCLUDE

    def _sidecar_refusal(
        self, role: str, thread: ThreadState, *, closes_thread: bool,
    ) -> tuple[str, str] | None:
        """Why this thread may not turn a sidecar into a verdict, or ``None``.

        Returns the ``AssessmentDrop.reason`` and the human-facing detail, so the
        log line and the stored row can never say different things. ``None`` means
        PERSIST — and when the thread already holds a verdict, ``None`` means this
        one SUPERSEDES it. This is the only place either decision is made; the
        caller infers the supersession from ``_assessed_threads`` alone (see
        ``_capture_hub_assessment``), so the two cannot drift apart.

        **A sidecar is now trusted on its own.** Emitting one IS the hub saying
        "this is my verdict", and that is a better signal than either proxy this
        gate used to compute from outside the artifact. The only refusals left
        are re-captures (below); an EARLY verdict is accepted as provisional and
        superseded by any later one.

        Two rounds of evidence forced that. The gate first asked only "is the
        ordinal 12", which destroyed every ``pass`` — delivering one opens with
        ⏸️, and ⏸️ closes the thread 3-8 ms later in this same code path, so no
        ordinal-12 turn ever arrives (run 076e80b6: 4 of 5 refusals were the
        thread's terminal message; only 1 of 62 threads reached 12). The fix
        added ``or closes_thread``, which rescued declines and left positives
        exposed, because the prompts bind the two to MUTUALLY EXCLUSIVE outcomes:
        ``phase4-thread-reply.md``'s Outcome 1 is verdict + sidecar and NO ⏸️,
        Outcome 2 is ⏸️ and "emit no sidecar". So the only sidecar the code
        reliably accepted was one the prompt forbids. Run 8b64a0e0 measured the
        result: the CONCLUDE door was offered **once in 140 hub reply turns**, 0
        of 15 sidecars used it, all 13 stored verdicts came through the ⏸️ door,
        and the two refused at ordinal 10 included the run's highest-scoring
        idea (markham, 3.04, its only ``route-to-incubation``) — refused 6
        minutes before the run's timer ended the interview that was supposedly
        still owed a verdict. A prompt-COMPLIANT model would have stored nothing
        at all that run.

        The "wait for a better-informed turn" instinct behind the old refusal is
        right and is now served by ``_retire_superseded_verdict`` — which landed
        in the SAME commit as the refusal it makes unnecessary. Last write wins,
        so a later turn still overrides an earlier one; the difference is that
        the interview is never left with nothing when that later turn does not
        come.

        ``duplicate_thread_verdict`` — the thread already holds a verdict this
        reply may not replace, in one of two ways:
          * the held verdict is ``final`` (its reply concluded or closed the
            interview): there is no legitimate later turn, so anything after it
            is a re-capture.
          * this reply is not strictly LATER than the held one (same ordinal =
            the same turn captured twice). A true duplicate, refused.
        See ``_assessed_threads`` for why the record is process-local.

        ``closes_thread`` still matters, just not for admission: the caller uses it
        to decide whether the verdict is TERMINAL — which marks the held record
        ``final`` and is the only thing that releases the public
        ``#assessments-summary`` headline. A provisional verdict is stored and
        visible to staff; it is not announced.

        The ordinal arithmetic is ``_warn_if_hub_conclude_missing_assessment``'s
        exactly — prior count plus one, because ``phase4_guidance`` wants the
        ordinal of the reply just generated.
        """
        ordinal = thread.message_count + 1
        held = self._assessed_threads.get(thread.thread_id)
        if held is not None:
            if held.final:
                return (
                    "duplicate_thread_verdict",
                    "this interview already closed with a verdict (message "
                    f"ordinal {held.ordinal}); one interview yields one "
                    "assessment",
                )
            if ordinal <= held.ordinal:
                return (
                    "duplicate_thread_verdict",
                    f"this interview's verdict from message ordinal {held.ordinal} "
                    "is already stored; re-capturing the same turn is not a new "
                    "verdict",
                )
            # Later, and the earlier verdict was provisional: last write wins.
            # A later turn has strictly more of the interview behind it (more
            # answers, more consults), so it is better-informed by construction —
            # which is the same argument the old `premature_sidecar` arm used to
            # justify DESTROYING the early one, applied in the direction that
            # keeps data. The caller retires the superseded row.
            return None
        return None

    async def _retire_superseded_verdict(
        self, agent_id: str, thread: ThreadState, superseded: _HeldVerdict,
        *, replacement_ordinal: int, replacement_id: uuid.UUID | None,
    ) -> None:
        """Remove the provisional verdict a later reply's verdict just replaced.

        Last-write-wins needs both halves: without this, the later verdict lands
        and the earlier one STAYS, which is precisely the duplication production
        showed (three rows, 2.51/2.66/2.69, for one pearce interview). The
        interview keeps exactly one row, and the one it keeps is the
        better-informed one.

        ``replacement_id`` is the replacement row's pre-generated primary key
        (``_persist_assessment``'s second return value) — ``None`` when the
        replacement itself only made it to ``_pending_assessments`` and not yet
        to the database. When it is not ``None``, any ``AssessmentReview``,
        ``AssessmentReviewEvent``, ``AssessmentReviewAssignment`` or
        ``PromptChangeSuggestion`` row a human attached to the row being
        retired is re-pointed onto the replacement BEFORE the delete, in the
        same transaction — and so is the payload of any pending or processing
        ``review_feedback_analysis`` ``Job`` that names the retired id.
        When it is ``None`` the re-point is skipped and the retired row's
        review rows CASCADE away with it: there is no live replacement row yet
        to re-point them onto, and stamping them onto a row that may never
        land (or may land under a different id after `_flush_pending_
        assessments` retries) would be worse than losing them outright.

        The supersession itself is recorded as a ``duplicate_thread_verdict``
        drop for the SUPERSEDED verdict — the trail has to survive the deletion,
        and ``assessment_drops`` is where every other lost verdict on this
        surface already appears.

        **The drop keeps the verdict.** The refusal path in
        ``_capture_hub_assessment`` passes ``raw_verdict`` under a comment saying
        a refusal "is never a licence to destroy it"; supersession was the one
        path that both DELETED a row and kept nothing, so the earlier verdict —
        its scores, its rationale, its red flags — existed nowhere afterwards.
        The row is read back BEFORE it is deleted (``_record_assessment_drop``
        opens its own session, so the sequence is SELECT -> drop -> DELETE) using
        the SAME predicate the DELETE uses: if the two diverged and a thread held
        two rows mid-transition, the drop would preserve the WRONG verdict, which
        is worse than preserving none because it looks authoritative.

        The retired rows' ``summary_posted_at``/``summary_claimed_at`` are carried
        onto the replacement (or its queued entry) in the same transaction as the
        delete, so an announced interview stays announced across a restart.

        Best-effort in the same sense as every other write on this path: the
        concluding reply is already in Slack, so nothing here may raise. Two
        honest limits, both logged loudly rather than hidden:
          * a superseded row with no ``slack_ts`` cannot be located again. The
            row now carries ``thread_id``, but that is NOT enough on its own —
            see ``_superseded_row_filter`` — so it is left in place and the
            duplicate is reported.
          * a copy still sitting on ``_pending_assessments`` is dropped from the
            queue first, because a retry that landed afterwards would recreate
            the duplicate this just removed. A flush already in flight holds its
            own list reference and can still land such a row; that is the same
            process-local approximation ``_assessed_threads`` itself is.
        """
        detail = (
            f"verdict from message ordinal {superseded.ordinal} superseded by the "
            f"interview's concluding verdict at ordinal {replacement_ordinal}; "
            "one interview yields one assessment, and the later verdict is the "
            "better-informed one"
        )
        logger.info(
            "[%s] Phase 4: superseded the earlier verdict for %s on thread %s — %s",
            agent_id, thread.other_agent_id or "?", thread.thread_id, detail,
        )
        # Read the row BEFORE recording the drop, because the drop is what has to
        # carry it and `_record_assessment_drop` commits in its own session.
        retired_verdict = await self._superseded_raw_verdict(
            agent_id, thread, superseded,
        )
        await self._record_assessment_drop(
            agent_id, "duplicate_thread_verdict",
            subject_agent_id=thread.other_agent_id,
            thread_id=thread.thread_id,
            detail=detail,
            raw_verdict=retired_verdict,
        )
        # Prune the retry queue BEFORE the no-slack_ts bail below, and guard the
        # match explicitly rather than relying on that bail to keep a `None` out
        # of it. `row.get("slack_ts") == superseded.slack_ts` with `None` on the
        # right matches EVERY queued row that never got a Slack ts — other
        # interviews' verdicts included — and those rows would be dropped from
        # the queue and never written. A rehydrated verdict
        # (`_rehydrate_assessed_threads`) is exactly where a `None` comes from,
        # so this is reachable rather than theoretical, and the guard has to live
        # here rather than upstream: a later edit that moves the bail must not be
        # able to re-open it.
        if superseded.slack_ts:
            queued = [
                row for row in self._pending_assessments
                if row.get("slack_ts") == superseded.slack_ts
                and row.get("thread_id") == thread.thread_id
            ]
            if queued:
                self._pending_assessments[:] = [
                    row for row in self._pending_assessments
                    if row not in queued
                ]
                logger.info(
                    "[%s] Phase 4: dropped %d superseded verdict(s) from the "
                    "assessment retry queue (thread=%s slack_ts=%s)",
                    agent_id, len(queued), thread.thread_id, superseded.slack_ts,
                )
        elif self._pending_assessments:
            logger.info(
                "[%s] Phase 4: the superseded verdict on thread %s has no "
                "slack_ts, so the assessment retry queue (%d row(s)) is left "
                "untouched — a NULL match there would sweep every queued verdict "
                "that has no Slack ts of its own",
                agent_id, thread.thread_id, len(self._pending_assessments),
            )
        if not superseded.slack_ts:
            logger.warning(
                "[%s] Phase 4: the superseded verdict on thread %s has no "
                "slack_ts to find its row by — it stays stored, so this "
                "interview now has TWO assessments",
                agent_id, thread.thread_id,
            )
            return
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            from sqlalchemy import delete as sa_delete
            from sqlalchemy import func as sa_func
            from sqlalchemy import select as sa_select
            from sqlalchemy import update as sa_update

            async with self.session_factory() as db:
                if replacement_id is not None:
                    old_ids = sa_select(OpportunityAssessment.id).where(
                        *self._superseded_row_filter(agent_id, thread, superseded)
                    ).scalar_subquery()
                    for model in (
                        AssessmentReview, AssessmentReviewEvent, PromptChangeSuggestion,
                    ):
                        await db.execute(
                            sa_update(model)
                            .where(model.assessment_id.in_(old_ids))
                            .values(assessment_id=replacement_id)
                        )
                    existing = sa_select(AssessmentReviewAssignment.assignee_user_id).where(
                        AssessmentReviewAssignment.assessment_id == replacement_id
                    ).scalar_subquery()
                    await db.execute(
                        sa_update(AssessmentReviewAssignment)
                        .where(AssessmentReviewAssignment.assessment_id.in_(old_ids),
                               AssessmentReviewAssignment.assignee_user_id.not_in(existing))
                        .values(assessment_id=replacement_id)
                    )
                    # The job queue is the FOURTH place the retired id lives.
                    # A review job still queued against it would run after
                    # this delete, find no assessment, complete as a no-op and
                    # leave the re-pointed 'learn' rows above unconsumed with
                    # nothing left to consume them (audit 2026-09-02, D3).
                    # 'processing' is included deliberately: a job mid-flight
                    # whose suggestion INSERT then fails on the deleted FK is
                    # retried by the worker, and the retry must run against
                    # the replacement.
                    retired_ids = [
                        str(row) for row in (
                            await db.execute(
                                sa_select(OpportunityAssessment.id).where(
                                    *self._superseded_row_filter(
                                        agent_id, thread, superseded,
                                    )
                                )
                            )
                        ).scalars().all()
                    ]
                    if retired_ids:
                        await db.execute(
                            sa_update(Job)
                            .where(
                                Job.type == "review_feedback_analysis",
                                Job.status.in_(("pending", "processing")),
                                Job.payload["assessment_id"].astext.in_(retired_ids),
                            )
                            .values(payload={"assessment_id": str(replacement_id)})
                        )
                # Stamps survive retirement (spec P0-08): the retired rows may
                # carry this interview's headline stamp or claim, and deleting
                # them must not un-announce the interview. Copied onto the
                # replacement in THIS transaction, COALESCE so a stamp the
                # replacement already has is never overwritten — or, when the
                # replacement is still queued, onto its queued entry.
                retired_posted, retired_claimed = (await db.execute(
                    sa_select(
                        sa_func.max(OpportunityAssessment.summary_posted_at),
                        sa_func.max(OpportunityAssessment.summary_claimed_at),
                    ).where(*self._superseded_row_filter(agent_id, thread, superseded))
                )).one()
                carried = {
                    key: value for key, value in (
                        ("summary_posted_at", retired_posted),
                        ("summary_claimed_at", retired_claimed),
                    ) if value is not None
                }
                if carried and replacement_id is not None:
                    await db.execute(
                        sa_update(OpportunityAssessment)
                        .where(OpportunityAssessment.id == replacement_id)
                        .values(**{
                            key: sa_func.coalesce(getattr(OpportunityAssessment, key), value)
                            for key, value in carried.items()
                        })
                    )
                elif carried:
                    for queued in self._pending_assessments:
                        if queued.get("thread_id") == thread.thread_id:
                            for key, value in carried.items():
                                if queued.get(key) is None:
                                    queued[key] = value
                result = await db.execute(
                    sa_delete(OpportunityAssessment).where(
                        *self._superseded_row_filter(agent_id, thread, superseded)
                    )
                )
                await db.commit()
            logger.info(
                "[%s] Phase 4: removed %d superseded assessment row(s) for "
                "thread %s (slack_ts=%s)",
                agent_id, result.rowcount or 0, thread.thread_id,
                superseded.slack_ts,
            )
        except Exception as exc:  # noqa: BLE001 — never lose a posted reply over this
            logger.error(
                "[%s] Failed to remove the superseded assessment row (and "
                "re-point its review rows) for thread %s (slack_ts=%s): %s — "
                "the interview now has TWO assessments, the drop row above "
                "says which is which, and any review re-point for this "
                "retirement may not have completed",
                agent_id, thread.thread_id, superseded.slack_ts, exc,
                exc_info=True,
            )

    def _superseded_row_filter(
        self, agent_id: str, thread: ThreadState, superseded: _HeldVerdict,
    ) -> tuple:
        """The predicate that identifies the row a supersession retires.

        ONE definition, because two statements need it and they must not
        disagree: the SELECT that copies the verdict onto its drop row, and the
        DELETE that removes it. If they diverged and the thread held two rows
        mid-transition, the drop would preserve a DIFFERENT verdict from the one
        deleted — worse than preserving none, because it looks authoritative.

        ``slack_ts`` is load-bearing and cannot be replaced by ``thread_id``.
        ``_capture_hub_assessment`` reads ``superseded`` BEFORE
        ``_persist_assessment`` writes the replacement and retires AFTER, so by
        the time this runs the replacement is already committed on the same run,
        the same agent and the SAME THREAD. A thread-keyed DELETE would match it
        too and end every supersession with ZERO assessments while logging
        success. ``slack_ts`` is the one field that differs.

        ``thread_id`` is therefore an additional NARROWING predicate, never the
        key: it stops a row from another interview that happens to carry the same
        Slack ts. ``thread_id IS NULL`` is tolerated alongside it, because a row
        written by an earlier build of this method (or by a caller with no
        thread) has no thread on it and is still the row this is retiring.

        Callers must never omit ``superseded.slack_ts`` — a ``None`` here would
        collapse the predicate to "this thread's rows", which is the trap above.
        The caller bails before reaching this.

        The narrowing is DEFENCE IN DEPTH and is deliberately NOT pinned by a
        test. Measured 2026-08-23: deleting the ``sa_or`` element below leaves
        ``test_hub_assessment_capture_gate.py`` and
        ``test_opportunity_assessment_persistence.py`` at 79 passed. That is
        expected, not a coverage gap — ``slack_ts`` is already unique within a
        ``(simulation_run_id, agent_id)`` pair, so no fixture can construct the
        collision this guards against without first constructing a different bug.
        Keep it anyway: it costs one OR and it is the only thing standing between
        a same-ts row from another interview and a wrong retirement.
        """
        from sqlalchemy import or_ as sa_or

        return (
            OpportunityAssessment.simulation_run_id == self.simulation_run_id,
            OpportunityAssessment.agent_id == agent_id,
            OpportunityAssessment.slack_ts == superseded.slack_ts,
            sa_or(
                OpportunityAssessment.thread_id == thread.thread_id,
                OpportunityAssessment.thread_id.is_(None),
            ),
        )

    async def _superseded_raw_verdict(
        self, agent_id: str, thread: ThreadState, superseded: _HeldVerdict,
    ) -> dict | None:
        """The verdict about to be deleted, so its drop row can keep it.

        ``None`` when there is nothing to read, in FIVE distinct ways: no
        ``slack_ts`` to find the row by, no database, no matching row, a failed
        SELECT, or — the one this docstring used to omit — a row that IS found
        but whose ``raw_verdict`` column is itself NULL. Never raises: the
        concluding reply is already in Slack, and a lookup that cannot answer
        must cost the copy, not the supersession.

        All five produce a drop row with ``raw_verdict IS NULL``, so the LOG is
        the only thing that can tell them apart — and on the one path whose whole
        purpose is "never lose the retired verdict", "there was no verdict to
        copy" must not be indistinguishable from "the copy was never attempted".
        The not-found branch and the NULL-column branch therefore warn
        explicitly; the two early returns are ordinary, expected states with
        their own callers' logging (the no-``slack_ts`` case is already reported
        loudly by the caller, and a DB-less engine is a documented silent no-op
        everywhere).

        The NULL-column case is rare but real: ``opportunity_assessments.
        raw_verdict`` has been a nullable column since migration 0025 and every
        engine writer has set it since then, so a NULL here means a row written
        some other way (a hand-seeded or test row). Migration 0035 added
        ``assessment_drops.raw_verdict``, not this column.
        """
        if not superseded.slack_ts:
            return None
        if not self.session_factory or not self.simulation_run_id:
            return None
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(OpportunityAssessment.raw_verdict).where(
                        *self._superseded_row_filter(agent_id, thread, superseded)
                    )
                )).scalars().all()
        except Exception as exc:  # noqa: BLE001 — a copy must not cost the retire
            logger.error(
                "[%s] Failed to read back the superseded verdict for thread %s "
                "(slack_ts=%s): %s — the drop row will record the supersession "
                "but not the verdict itself",
                agent_id, thread.thread_id, superseded.slack_ts, exc,
                exc_info=True,
            )
            return None
        if not rows:
            logger.warning(
                "[%s] Supersession on thread %s found no stored row for "
                "slack_ts=%s — the drop row records that a verdict was "
                "superseded but cannot carry the verdict itself, and the DELETE "
                "below will match nothing either",
                agent_id, thread.thread_id, superseded.slack_ts,
            )
            return None
        if rows[0] is None:
            logger.warning(
                "[%s] Supersession on thread %s found the stored row for "
                "slack_ts=%s but its raw_verdict is NULL (every engine writer "
                "sets it, so this row was written out of band) — the drop row "
                "records that a verdict was superseded but cannot carry the "
                "verdict itself",
                agent_id, thread.thread_id, superseded.slack_ts,
            )
            return None
        return rows[0]

    async def _rehydrate_assessed_threads(self) -> None:
        """Rebuild ``_assessed_threads`` from this run's stored verdicts.

        The map is process-local, so a restart used to leave the engine blind to
        every verdict it had already written: the interview's own later turn
        looked like a FIRST verdict and landed a second row, and a lab bot
        ⏸️-closing a thread that already held one produced a spurious
        ``closed_before_verdict`` drop. ``opportunity_assessments.thread_id``
        (migration 0036, written by ``_persist_assessment``) is what makes this
        answerable at all — before it the table did not record which interview a
        verdict came from.

        Every field of the restored record is a decision about which way to
        fail, and three of them are not guesses:

        * ``ordinal=0``. The table does not store the turn a verdict came from.
          Any guess at or above the real ordinal makes ``_sidecar_refusal``
          refuse the interview's legitimate LATER verdict
          (``if ordinal <= held.ordinal``); zero costs at most a spurious
          ``duplicate_thread_verdict`` drop if the very same turn is re-captured.
        * ``announced`` is READ, not defaulted, from
          ``summary_posted_at`` (migration 0041). It used to be hardcoded
          ``False`` with the reasoning that ``True`` "would suppress the
          headline for a verdict stored provisionally before the restart — a
          silent D12 breach". That was the right call against a schema with no
          answer in it, but it traded one breach for another: a verdict whose
          headline was ALREADY public got a second one, and a headline cannot
          be retracted. The column answers the question directly, so neither
          trade is necessary. A pre-0041 row reads NULL and therefore False,
          which is exactly the old behaviour.
        * ``final`` is DERIVED, not defaulted: closing a thread writes a
          ``ThreadDecision``, so ``thread_id in self._closed_thread_ids`` is the
          real answer. This must therefore run AFTER ``_rebuild_agent_state``
          populates that set. ``final=True`` as a "conservative" default would be
          the worst of the three: ``_sidecar_refusal`` refuses EVERYTHING on a
          final thread, so the interview's own concluding verdict would be
          refused and only its ``raw_verdict`` would survive, on a drop row.

        Rows with a NULL ``thread_id`` (every row written before 0036, and any
        verdict whose thread could not be identified) are skipped: they cannot be
        placed, and placing them under a guessed thread is how a real verdict
        gets refused. Ordered oldest-first so that a thread carrying several
        historical rows is represented by its NEWEST — the same last-write-wins
        rule ``_retire_superseded_verdict`` applies.

        Never raises: a failed read costs the de-duplication, not the run.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(
                        OpportunityAssessment.thread_id,
                        OpportunityAssessment.slack_ts,
                        OpportunityAssessment.summary_posted_at,
                    )
                    .where(
                        OpportunityAssessment.simulation_run_id == self.simulation_run_id,
                        OpportunityAssessment.thread_id.is_not(None),
                    )
                    .order_by(OpportunityAssessment.created_at)
                )).all()
        except Exception as exc:  # noqa: BLE001 — never fail startup over this
            logger.warning(
                "Failed to rehydrate the assessed-thread record: %s — this "
                "process may store a SECOND verdict for an interview it already "
                "assessed before the restart",
                exc,
            )
            return
        for thread_id, slack_ts, summary_posted_at in rows:
            self._assessed_threads[thread_id] = _HeldVerdict(
                ordinal=0,
                final=thread_id in self._closed_thread_ids,
                slack_ts=slack_ts,
                announced=summary_posted_at is not None,
            )
        if rows:
            # `len(rows)` — the number of stored verdicts read — NOT
            # `len(self._assessed_threads)`. The two are equal only while every
            # thread holds exactly one row, which is precisely the invariant this
            # mechanism exists because production BROKE (one pearce interview
            # held three), so the count diverged exactly when an operator was
            # reading this line to size the damage.
            logger.info(
                "Rehydrated %d stored verdict(s) across %d interview(s) from "
                "opportunity_assessments — a verdict already stored for one of "
                "these threads will be superseded rather than duplicated",
                len(rows), len(self._assessed_threads),
            )

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

    async def _record_assessment_drop(
        self,
        agent_id: str,
        reason: str,
        *,
        subject_agent_id: str | None = None,
        thread_id: str | None = None,
        detail: str | None = None,
        raw_verdict: dict | None = None,
    ) -> None:
        """Record that a verdict was lost — generated and discarded, or, for
        ``empty_reply``, never produced at all.

        ``raw_verdict`` is the discarded verdict itself, and passing it whenever
        one exists is the point of the column. Without it a refusal is
        irreversible: run 8b64a0e0 refused markham's sidecar — 3.04, that run's
        highest score and its only ``route-to-incubation`` — and the JSON
        survived only because ``llm_call_logs.response_text`` happens to keep the
        whole response. A gate decision about WHERE a verdict belongs must never
        also be a decision to destroy it.

        Best-effort in exactly the same sense as ``_persist_assessment``: for
        every reason except ``empty_reply`` the concluding reply is already in
        Slack by the time any of these fire; for ``empty_reply`` nothing was
        ever generated or posted. Either way nothing here may raise, and a
        DB-less engine is a silent no-op.

        This exists because every loss path is otherwise invisible — one WARNING
        in a container log — which leaves an empty ``/admin/assessments`` page
        meaning either "nothing screened yet" or "everything screened and every
        verdict thrown away", with no way to tell them apart. See
        ``AssessmentDrop`` for the ``reason`` vocabulary.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                db.add(AssessmentDrop(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    subject_agent_id=(subject_agent_id or None),
                    thread_id=(thread_id or None),
                    reason=reason,
                    detail=detail,
                    raw_verdict=raw_verdict,
                ))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — visibility must never cost a reply
            # This is already the fallback path for a verdict _persist_assessment
            # lost — if the fallback's own write fails there is nothing left to
            # requeue it into (same reasoning as _persist_assessment above), so
            # make the double loss unmistakable: ERROR + a full traceback.
            logger.error(
                "[%s] Failed to record assessment drop (%s): %s — LOST, the "
                "verdict AND its drop record are both gone now",
                agent_id, reason, exc, exc_info=True,
            )

    def _strip_disallowed_tags(
        self, message_text: str | None, agent: Agent
    ) -> tuple[str | None, int]:
        """Remove @BotName mentions of non-cohort agents from an outbound message.

        Defense-in-depth for the cohort gate: the receiving agent already filters
        tags from non-cohort senders (Phase 3), but emitting a tag toward an agent
        that will never respond leaves a dangling ask in the channel. No-op when
        the gate is off for this agent (``allowed_sender_ids is None``).

        Applied from ``_post_message``, so it covers **every** outbound path —
        Phase 4 replies, Phase 5 posts, private-channel messages — rather than just
        the one call site Phase 5 used to have.

        Three deliberate behaviours (specs/cohort-system-v2.md §9):

        - The whole mention is removed and the surrounding whitespace normalised.
          Keeping the bare name ("Great point WisemanBot") reads like an addressed
          message that isn't one.
        - An unknown bot name is left alone and logged at WARNING. A name missing
          from ``_bot_name_to_id`` means the roster is lagging, which is an
          operational problem, not a policy decision — fail open, loudly (§5.1).
        - Self-mentions are never stripped.

        Strips are counted per agent and surfaced in the admin UI: a high rate means
        the cohort topology disagrees with what the agents are trying to do.

        Returns ``(cleaned_text, n_stripped_this_call)`` — the second element is
        how many mentions THIS call removed, never inferred from the shared
        ``self._cohort_tags_stripped`` counter below (that counter is engine-wide
        and any concurrent agent's post can bump it between two reads of it, which
        is exactly the bug this per-call return exists to avoid — see Phase 5's
        caller). No-op paths (gate off, empty/None text, nothing matched) always
        report 0.
        """
        allowed = agent.allowed_sender_ids
        if allowed is None or not message_text:
            return message_text, 0

        stripped = 0

        def _repl(m: "re.Match[str]") -> str:
            nonlocal stripped
            bot_name = m.group(1)
            target_id = self._bot_name_to_id.get(bot_name.lower())
            if target_id is None:
                logger.warning(
                    "[%s] cohort gate: unknown bot name @%s in outbound text — "
                    "leaving the mention in place (roster may be lagging)",
                    agent.agent_id, bot_name,
                )
                return m.group(0)
            if target_id == agent.agent_id or target_id in allowed:
                return m.group(0)
            stripped += 1
            logger.debug(
                "[%s] cohort gate: stripped cross-cohort mention @%s",
                agent.agent_id, bot_name,
            )
            return ""

        # The pattern swallows any run of spaces/tabs immediately BEFORE the
        # mention, so "Great point @CravattBot, shall we?" collapses cleanly to
        # "Great point, shall we?" without a global reflow. The lookbehind requires
        # the '@' to start a token, the way a real Slack mention does — without it,
        # "a@subot.example" or a URL path ending in a bot name would be mangled, and
        # this strip now runs on EVERY outbound message.
        cleaned = re.sub(r"[ \t]*(?<![\w./@-])@(\w+[Bb]ot)\b", _repl, message_text)
        if not stripped:
            return message_text, 0

        self._cohort_tags_stripped[agent.agent_id] = (
            self._cohort_tags_stripped.get(agent.agent_id, 0) + stripped
        )
        # Targeted tidy-up only. Deliberately NOT a global whitespace normalisation:
        # stripping leading indentation would mangle the code blocks and bullet lists
        # agents put in messages. Collapse interior runs only after a non-space, and
        # trim end-of-line space; never touch line-leading whitespace.
        cleaned = re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"(?m)[ \t]+$", "", cleaned)
        cleaned = cleaned.lstrip(" \t") if cleaned[:1] in (" ", "\t") else cleaned
        return cleaned, stripped

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

    def _build_lab_directories(self) -> None:
        """Build a condensed publications directory for each agent (excluding their own lab)."""
        lab_pubs: dict[str, list[str]] = {}
        for agent in self.agents.values():
            profile_text = agent.public_profile
            match = re.search(
                r"## Recent Publications\n(.*?)(?=\n## |\Z)",
                profile_text,
                re.DOTALL,
            )
            if match:
                pubs = [
                    line.strip()
                    for line in match.group(1).strip().split("\n")
                    if line.strip().startswith("- ")
                ]
                if pubs:
                    lab_pubs[agent.agent_id] = pubs[:5]

        for agent in self.agents.values():
            allowed = agent.allowed_sender_ids  # None == gate off
            sections = []
            for other_id, pubs in sorted(lab_pubs.items()):
                if other_id == agent.agent_id:
                    continue
                if allowed is not None and other_id not in allowed:
                    continue  # cohort gate: don't prime this agent with a non-mate's work
                other_agent = self.agents[other_id]
                sections.append(f"### {other_agent.pi_name} Lab")
                sections.extend(pubs)
                sections.append("")
            agent._lab_directory = "\n".join(sections) if sections else None

    # Public alias. `_build_lab_directories` is called from several places whose
    # ordering relative to the cohort gate is the whole bug this name documents:
    # it must run AFTER _recompute_allowed_sender_ids, never before.
    def refresh_lab_directories(self) -> None:
        """Rebuild every agent's lab directory against its CURRENT gate."""
        self._build_lab_directories()

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

    async def _flush_pending_assessments(self, *, final: bool = False) -> None:
        """Retry OpportunityAssessment rows queued by _persist_assessment.

        _persist_assessment attempts an immediate write; a failure there
        (most commonly the pool-checkout timeout that Task 2 of
        docs/plans/2026-08-14-two-lane-concurrent-scheduler.md sized the pool for)
        appends the fully-built row here instead of dropping it. Mirrors
        _flush_persisted's buffer/retry pattern exactly, just against
        _pending_assessments instead of _pending_persist.

        Must be drained by the SAME per-turn cadence as _flush_persisted/
        _flush_llm_logs (see _run_main_loop) so the shutdown flush in
        stop() covers it too — a buffer only retried on the next assessment
        would strand the last one at shutdown, which is exactly the
        durability gap this exists to close. An opportunity_assessments row
        is the actual product of the screening pipeline, so a repeat failure
        here stays at ERROR (louder than _flush_persisted/_flush_llm_logs'
        WARNING on the same kind of retry failure).
        """
        if not self._pending_assessments:
            return
        if not self.session_factory or not self.simulation_run_id:
            self._pending_assessments.clear()
            return
        rows = self._pending_assessments
        self._pending_assessments = []
        try:
            async with self.session_factory() as db:
                for row in rows:
                    db.add(OpportunityAssessment(**row))
                await db.commit()
            logger.info("Flushed %d queued assessment(s) to DB", len(rows))
        except Exception as exc:
            # Same re-queue-in-front reasoning as _flush_persisted: new
            # failures may have been appended to _pending_assessments while
            # we were awaiting the (failed) commit, so put this batch back in
            # front to preserve retry order. And, on a ROW-level error only,
            # isolate the poison row first so the verdicts beside it survive —
            # these rows are the actual product of the screening pipeline.
            requeue = rows
            if isinstance(exc, _ROW_LEVEL_DB_ERRORS):
                async def _one(db, row):
                    db.add(OpportunityAssessment(**row))

                _written, lost, requeue = await self._recover_rows_individually(
                    rows, _one, what="assessment",
                )
                # A row the database refuses outright is a SCREENING VERDICT
                # discarded, and every other way of losing one writes an
                # `AssessmentDrop`. Sequenced after `_recover_rows_individually`
                # returns (so its session is already closed) rather than inside
                # the per-row loop, which would nest a second checkout inside the
                # recovery session on a pool that may already be under pressure.
                for lost_row, lost_exc in lost:
                    await self._record_unwritable_assessment(lost_row, lost_exc)
            if self._report_flush_failure(
                what="assessment", requeue=requeue, exc=exc, final=final,
                log=logger.error, exc_info=True,
            ):
                self._pending_assessments[0:0] = requeue

    async def _record_unwritable_assessment(
        self, row: dict, exc: BaseException,
    ) -> None:
        """An assessment row the database refused, kept as an ``AssessmentDrop``.

        The per-row recovery in ``_recover_rows_individually`` is a path A3.4
        itself created: before it, a poison row lost its whole batch loudly and
        re-queued; after it, ONE row is dropped. For an
        ``opportunity_assessments`` row that means a screening verdict discarded
        on a single log line — while every other way a verdict fails to land
        (``missing_sidecar``, ``duplicate_thread_verdict``,
        ``closed_before_verdict``, ...) writes a drop row. "Every way an
        assessment can be lost is silent" is the exact defect ``AssessmentDrop``
        exists to end.

        ``reason='unwritable_row'`` — deliberately outside the existing
        vocabulary, because this is the only reason that is not a GATE decision:
        the engine wanted the row and the database refused it. The verdict itself
        rides along in ``raw_verdict``, so the refusal is non-destructive like
        every other.

        Best-effort, and it may never raise into the flush path. It is not
        retried: the row already failed twice (batch, then alone), and the drop
        is the record OF that, not another attempt at it.
        ``_record_assessment_drop`` already opens its own session — which
        matters here, because the recovery session may be in an aborted
        transaction — and already swallows its own failures with an ERROR. The
        wrapper is for everything before that point (a malformed ``row``), so a
        bad row cannot take the surviving verdicts of its batch down with it.

        THE HANDLER MUST NOT TOUCH ``row``. That is the whole failure it is
        handling: an earlier version read ``row.get("thread_id")`` inside the
        ``except`` and so raised WHILE HANDLING a row that had no ``.get`` —
        and because the loop that calls this sits inside
        ``_flush_pending_assessments``'s own ``except``, that escaped the
        flusher entirely, skipping ``_report_flush_failure``, the re-queue, and
        every later lost row's drop. Hence the two locals: bound to ``None``
        BEFORE the ``try``, filled inside it, and read by the handler in place of
        ``row``. Not reachable from production today (``_pending_assessments``
        has one append site and it always appends a dict) — which is exactly why
        the claim above needed a test rather than trust.
        """
        thread_id = None
        slack_ts = None
        try:
            thread_id = row.get("thread_id")
            slack_ts = row.get("slack_ts")
            await self._record_assessment_drop(
                row.get("agent_id") or "unknown",
                "unwritable_row",
                subject_agent_id=row.get("subject_agent_id"),
                thread_id=thread_id,
                detail=(
                    f"the database refused this row: {type(exc).__name__}: {exc} "
                    f"(channel={row.get('channel_name')!r} "
                    f"slack_ts={slack_ts!r})"
                ),
                raw_verdict=row.get("raw_verdict"),
            )
        except Exception as drop_exc:  # noqa: BLE001 — never cost the batch
            logger.error(
                "Failed to record the drop for an un-writable assessment "
                "(thread=%s slack_ts=%s): %s — the verdict AND its drop record "
                "are both gone now",
                thread_id, slack_ts, drop_exc,
                exc_info=True,
            )

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

    def _infer_agent_id(self, name: str) -> str | None:
        """Try to infer agent_id from a bot name or display name."""
        name_lower = name.lower()
        # Direct lookup
        if name_lower in self._bot_name_to_id:
            return self._bot_name_to_id[name_lower]
        # Partial match
        for bot_name, agent_id in self._bot_name_to_id.items():
            if agent_id in name_lower or bot_name in name_lower:
                return agent_id
        return None

    # ------------------------------------------------------------------
    # LLM call logging
    # ------------------------------------------------------------------

    def _sync_profiles_from_disk(self) -> None:
        """Reload any agent whose public profile file changed on disk since last turn.

        The public profile can be edited from the web app, which runs in a
        separate process and writes profiles/public/{id}.md on a shared
        mounted volume. Each Agent caches its profile content in memory.
        Without this check, a web edit would not reach the running simulation
        until a restart.

        Detection is by file mtime: cheap (one stat() call per agent, no DB
        round-trip) and tied to exactly what the agent reads.
        """
        for agent in self.agents.values():
            mtime = 0.0
            for sub in ("public",):
                path = constants.PROFILES_DIR / sub / f"{agent.agent_id}.md"
                try:
                    mtime = max(mtime, path.stat().st_mtime)
                except OSError:
                    continue  # file may not exist yet — agent falls back to default

            prev = self._profile_mtimes.get(agent.agent_id)
            if prev is None:
                # First observation — record the baseline without reloading.
                self._profile_mtimes[agent.agent_id] = mtime
                continue
            if mtime > prev:
                agent.reload_profiles()
                self._profile_mtimes[agent.agent_id] = mtime
                logger.info(
                    "[%s] Reloaded profiles from disk (external edit detected)",
                    agent.agent_id,
                )

    async def _sync_roster_from_db(self) -> None:
        """Re-sync the live agent roster from AgentRegistry (active_roster_select: status=='active', pi_lab linked to a user).

        Adds agents that have just been activated (and have a usable token) and
        removes agents that have been inactivated/suspended — all without a
        process restart. Tokens are read from the DB row (falling back to .env),
        so a freshly provisioned token is picked up on the next tick too.

        Mutates self.agents / self.slack_clients IN PLACE — never reassigned —
        in case anything else in the engine has taken a reference to either dict.
        """
        if not self.session_factory:
            return
        now = deps.time.time()
        if now - self._last_roster_poll < ROSTER_POLL_INTERVAL:
            return
        self._last_roster_poll = now

        try:
            from src.agent.roster_query import active_roster_select
            from src.agent.slack_client import AgentSlackClient
            from src.services.slack_tokens import env_token, is_valid_token

            async with self.session_factory() as db:
                rows = (await db.execute(active_roster_select())).all()

            desired = {r.agent_id: r for r in rows}

            # Role-diff for surviving agents (agents present in both current and
            # desired). Must run even when to_add/to_remove are empty, or a role
            # reassignment on a running agent is invisible until the next add/remove.
            role_changed = False
            for aid, agent in self.agents.items():
                r = desired.get(aid)
                if r is not None and getattr(r, "role", "pi_lab") != agent.role:
                    logger.info("[roster] %s role %s -> %s", aid, agent.role, r.role)
                    agent.role = r.role
                    role_changed = True

            # Token-diff for surviving agents. `main.py` admits every active
            # agent to self.agents regardless of token, so an agent provisioned
            # AFTER startup is in neither to_add nor to_remove: the membership
            # diff below early-returns and the client-building loop (which only
            # runs over to_add) never sees it. It then posts DB-only, silently,
            # until the process restarts. Measured 2026-08-06: 48 bots installed
            # mid-run, tokens all in AgentRegistry, and `Connected as` never rose
            # above the 7 that had tokens at boot. Adopt them here, before the
            # early return, so the docstring's promise is actually true.
            if self.slack_enabled:
                for aid in self.agents:
                    r = desired.get(aid)
                    if r is None or aid in self.slack_clients:
                        continue
                    token = (
                        r.slack_bot_token
                        if is_valid_token(r.slack_bot_token)
                        else env_token(aid)
                    )
                    if not is_valid_token(token):
                        continue  # still tokenless — retry on a later tick
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not await asyncio.to_thread(client.connect):
                        logger.warning(
                            "[roster] Slack connect failed adopting %s — will retry", aid,
                        )
                        continue
                    self.slack_clients[aid] = client
                    logger.info(
                        "[roster] Adopted Slack client for %s (token provisioned "
                        "after startup)", aid,
                    )

            current = set(self.agents)
            to_remove = current - set(desired)
            to_add = set(desired) - current
            if not to_remove and not to_add:
                # Recompute the gate FIRST: _recompute_allowed_sender_ids ends by
                # refreshing the directory (step 4), so after this line the
                # directory already agrees with the gate. The role branch stays
                # because a role change alters the directory's *contents*
                # (pi_name headings) without moving the gate at all.
                await self._recompute_allowed_sender_ids()
                if role_changed:
                    self.refresh_lab_directories()
                return

            # --- Removals: agent no longer active ---------------------------
            for aid in to_remove:
                self.agents.pop(aid, None)
                self.slack_clients.pop(aid, None)  # Web API only — no socket to close
                bot_name = next(
                    (n for n, a in self._bot_name_to_id.items() if a == aid), None
                )
                if bot_name:
                    self._bot_name_to_id.pop(bot_name, None)
                logger.info("[roster] Removed inactive agent %s from live roster", aid)

            # --- Additions: agent newly active ------------------------------
            for aid in to_add:
                r = desired[aid]
                if self.slack_enabled:
                    token = r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(aid)
                    if not is_valid_token(token):
                        logger.info(
                            "[roster] Agent %s is active but has no usable token yet — "
                            "skipping (will retry next sync once a token is set)", aid,
                        )
                        continue
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not await asyncio.to_thread(client.connect):
                        logger.warning("[roster] Slack connect failed for new agent %s — skipping", aid)
                        continue
                else:
                    # Slack off: admit the agent with a no-op transport (never
                    # gate on a token/connection that doesn't apply in DB-only mode).
                    from src.agent.transport import NullTransport
                    client = NullTransport(agent_id=aid)
                agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
                self.agents[aid] = agent
                self.slack_clients[aid] = client
                self._bot_name_to_id[agent.bot_name.lower()] = aid
                logger.info("[roster] Added newly-active agent %s to live roster", aid)

            # Rebuild cross-agent derived structures after any membership change.
            self.message_log.set_bot_name_map(self._bot_name_to_id)

            # Recompute cohort interaction sets after roster changes so newly
            # active agents get their gate populated this tick.
            await self._recompute_allowed_sender_ids()
        except Exception as exc:
            # A transient DB hiccup must never crash the main loop.
            logger.warning("[roster] roster sync failed: %s", exc)

    def _disable_all_gates(self) -> None:
        """Set every agent's gate to None (no filtering). See v2 §5.4."""
        for agent in self.agents.values():
            agent.allowed_sender_ids = None

    def _validate_star_topology(self) -> list[str]:
        """Check the live cohort gates against the hub-and-spoke ("star") design.

        The design (docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md
        §5) is strictly hub-and-spoke: every ``pi_lab`` agent's cohort is
        ``{lab, hub}`` — it may reach the ``scout_hub`` agent and nothing else. Two
        ways a gate can violate that, checked for every ``pi_lab`` agent whose gate
        is not None (``gate is None`` means isolation is off for that agent, which
        is vacuously fine — an ungated agent can always reach the hub):

        (a) the gate contains another ``pi_lab`` agent — labs can reach each other
            directly, which the hub-only design forbids.
        (b) the gate contains no ``scout_hub`` agent — the hub is unreachable, so
            the agent has nowhere to land a pitch.

        Returns one human-readable violation string per broken rule (a lab-to-lab
        pair is reported once, not once per side); empty when the topology is
        star-shaped, including when every gate is None.
        """
        violations: list[str] = []
        reported_pairs: set[frozenset[str]] = set()
        for agent_id, agent in self.agents.items():
            if agent.role != "pi_lab":
                continue
            gate = agent.allowed_sender_ids
            if gate is None:
                continue

            has_hub = False
            for other_id in gate:
                other = self.agents.get(other_id)
                if other is None or other_id == agent_id:
                    continue
                if other.role == "scout_hub":
                    has_hub = True
                elif other.role == "pi_lab":
                    pair = frozenset((agent_id, other_id))
                    if pair not in reported_pairs:
                        reported_pairs.add(pair)
                        violations.append(
                            f"{agent_id} and {other_id} are both pi_lab agents but "
                            "can reach each other directly — labs may only be "
                            "cohorted with the hub"
                        )

            if not has_hub:
                violations.append(
                    f"{agent_id} has no scout_hub agent in its cohort gate — the "
                    "hub is unreachable, so pitch targets are unsatisfiable"
                )

        return violations

    async def _recompute_allowed_sender_ids(self) -> None:
        """Recompute each live agent's cohort-mate set for the interaction gate.

        Called on the roster-sync cadence (ROSTER_POLL_INTERVAL) and once in setup
        before the first turn, so no turn can run with an unset gate while isolation
        is on.

        The decision logic lives in ``src.services.cohorts.compute_gates`` so the
        engine and the admin UI's preview cannot drift — the whole point of v2 is
        that a documented rule and the running code agreed. This method is the I/O
        and side-effect wrapper: read memberships, apply the computed gates, log on
        change, then reconcile in-memory state (§8 grandfathering, §6.1 pruning).

        On a transient DB error the existing gates are left in place: flapping the
        gate open on every blip would be worse than a briefly stale topology.
        """
        settings = deps.get_settings()
        if not settings.cohort_isolation_enabled:
            self._cohort_preflight_error = None
            self._disable_all_gates()
            self.refresh_lab_directories()
            self._cohort_gate_active = False
            self._cohort_log_signature = None
            # Reconcile state even on the disabled path: turning isolation off must
            # clear grandfathered flags, or cohort_topology_snapshot keeps
            # reporting threads the gate no longer affects.
            self._apply_cohort_gate_to_state()
            return

        rows: list[tuple[Any, str]] = []
        cohort_count = 0
        if self.session_factory:
            try:
                from sqlalchemy import func as sa_func
                from sqlalchemy import select as sa_select

                from src.models import Cohort, CohortMembership

                async with self.session_factory() as db:
                    rows = list((await db.execute(
                        sa_select(CohortMembership.cohort_id, CohortMembership.agent_id)
                    )).all())
                    cohort_count = (await db.execute(
                        sa_select(sa_func.count()).select_from(Cohort)
                    )).scalar() or 0
            except Exception as exc:
                logger.warning("[cohort] membership sync failed: %s", exc)
                # The gates themselves are left in place deliberately (flapping
                # open on every blip is worse than a briefly stale topology —
                # see the docstring). But the directory is DERIVED from those
                # gates, so a gate that is correct-but-stale makes a directory
                # rebuilt from it correct-but-stale too — which is strictly
                # better than leaving it absent. Without this, a newly-added
                # agent whose gate isn't reflected in any directory yet gets
                # _lab_directory = None for the rest of this failed tick, and
                # existing agents' directories omit it until the next
                # successful sync.
                self.refresh_lab_directories()
                return

        gates, reason = compute_gates(
            membership_rows=rows,
            agent_ids=list(self.agents),
            isolation_enabled=True,
            policy=settings.cohort_default_policy,
            cohort_count=cohort_count,
            has_db=self.session_factory is not None,
        )

        if reason is not None:
            if self._cohort_preflight_error != reason:
                logger.error("[cohort] isolation forced OFF: %s", reason)
            self._cohort_preflight_error = reason
            self._disable_all_gates()
            self.refresh_lab_directories()
            self._cohort_gate_active = False
            self._apply_cohort_gate_to_state()
            return
        if self._cohort_preflight_error is not None:
            logger.info("[cohort] preflight now clean — isolation active")
            self._cohort_preflight_error = None

        for aid, gate in gates.items():
            agent = self.agents.get(aid)
            if agent is not None:
                agent.allowed_sender_ids = gate

        summary = summarise_gates(gates)
        self._cohort_gate_active = summary["gated"] > 0
        signature = (
            cohort_count, len(rows), summary["gated"], tuple(summary["isolated"]),
        )
        if signature != self._cohort_log_signature:
            logger.info(
                "[cohort] gate: %d cohorts, %d memberships, %d/%d agents gated, "
                "%d isolated%s",
                cohort_count, len(rows), summary["gated"], summary["total"],
                len(summary["isolated"]),
                (" (" + ", ".join(summary["isolated"]) + ")")
                if summary["isolated"] else "",
            )
            if summary["isolated"]:
                logger.warning(
                    "[cohort] uncohorted agents isolated by policy: %s",
                    ", ".join(summary["isolated"]),
                )
            topology_changed = self._cohort_log_signature is not None
            self._cohort_log_signature = signature
        else:
            topology_changed = False

        self._apply_cohort_gate_to_state()
        # The directory is derived from the gate, so it is refreshed on the same
        # cadence. Cheap: it re-reads in-memory profiles, no I/O.
        self.refresh_lab_directories()
        if topology_changed:
            # The topology moved mid-run — snapshot the new one so the run stays
            # attributable to every configuration it actually ran under (v2 §13.1).
            await self._record_topology_snapshot()

        # Star-topology check: log only. The startup call site (start(), right
        # after the FIRST invocation of this method) raises instead — a live run
        # must not crash on an admin's transient cohort edit, but the edit still
        # needs to show up somewhere an operator will see it.
        for violation in self._validate_star_topology():
            logger.error("[cohort] star-topology violation: %s", violation)

    def _apply_cohort_gate_to_state(self) -> None:
        """Reconcile in-memory agent state with the freshly computed gate.

        **Grandfather** active threads whose partner is no longer permitted
        (v2 §8), because the gate is a *read-time* filter and state outlives a
        membership change. They still get Phase 4 replies (via the reply lane —
        an open conversation is entitled to conclude rather than waste the
        calls already spent); the flag is reported in
        ``cohort_topology_snapshot`` and affects no scheduling. This is also
        the path that marks a *resumed* run's threads: the DB rebuild runs
        before the first recompute, so every restart reconstructs its open
        partnerships gate-blind.
        """
        newly_grandfathered = 0
        for agent in self.agents.values():
            allowed = agent.allowed_sender_ids
            if allowed is None:
                # Gate off for this agent: nothing to grandfather, and a partner
                # that becomes permitted again is un-grandfathered.
                for thread in agent.state.active_threads.values():
                    if thread.grandfathered:
                        thread.grandfathered = False
                continue

            for thread in agent.state.active_threads.values():
                other = thread.other_agent_id
                permitted = bool(other) and other in allowed
                if permitted:
                    if thread.grandfathered:
                        logger.info(
                            "[cohort] %s: thread %s with %s is permitted again "
                            "(un-grandfathered)",
                            agent.agent_id, thread.thread_id, other,
                        )
                        thread.grandfathered = False
                    continue
                if self._channel_visibility.get(thread.channel) == VISIBILITY_COLLAB_PRIVATE:
                    # PI-created pairing outranks the gate (v2 §7) — never
                    # grandfather a private-channel collaboration.
                    thread.grandfathered = False
                    continue
                if not thread.grandfathered:
                    thread.grandfathered = True
                    newly_grandfathered += 1
                    logger.info(
                        "[cohort] %s: thread %s with %s grandfathered — partner is "
                        "outside the cohort; it may still conclude",
                        agent.agent_id, thread.thread_id, other,
                    )

        if newly_grandfathered:
            logger.info(
                "[cohort] state reconciled: %d threads grandfathered",
                newly_grandfathered,
            )

    def cohort_topology_snapshot(self) -> dict[str, Any]:
        """Serialise the gate configuration and its observed effects.

        Written to cohort_audit_events at run start and on every mid-run topology
        change, so a finished run stays attributable to every configuration it
        actually ran under (v2 §13.1). Derived from the live in-memory gate rather
        than re-querying, so it records what the engine actually applied — including
        a preflight override.

        Also carries the counters the admin UI cannot otherwise see: they live in
        this process's memory, and the web app is a different process (v2 §9 req. 4 / §13).
        """
        settings = deps.get_settings()
        grandfathered = sorted(
            f"{aid}:{t.thread_id}"
            for aid, a in self.agents.items()
            for t in a.state.active_threads.values()
            if t.grandfathered
        )
        return {
            "cohort_isolation_enabled": settings.cohort_isolation_enabled,
            "cohort_default_policy": settings.cohort_default_policy,
            "gate_active": self._cohort_gate_active,
            "preflight_error": self._cohort_preflight_error,
            "agents": {
                aid: (
                    None if a.allowed_sender_ids is None
                    else sorted(a.allowed_sender_ids)
                )
                for aid, a in sorted(self.agents.items())
            },
            "counters": {
                "tags_stripped": dict(sorted(self._cohort_tags_stripped.items())),
                "post_type_rejections": dict(sorted(self._post_type_rejections.items())),
                "grandfathered_threads": grandfathered,
            },
        }

    async def _record_topology_snapshot(self) -> None:
        """Persist a topology snapshot for this run.

        Called once in setup and again whenever the gate signature changes mid-run.
        Never raises: provenance is valuable but not worth failing a run over.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            from src.models import COHORT_ACTION_TOPOLOGY_SNAPSHOT, COHORT_NAME_ALL
            from src.services.cohorts import record_cohort_audit_event

            async with self.session_factory() as db:
                await record_cohort_audit_event(
                    db,
                    action=COHORT_ACTION_TOPOLOGY_SNAPSHOT,
                    cohort_name=COHORT_NAME_ALL,
                    simulation_run_id=self.simulation_run_id,
                    topology=self.cohort_topology_snapshot(),
                    commit=True,
                )
        except Exception as exc:
            logger.warning("[cohort] topology snapshot failed: %s", exc)


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
