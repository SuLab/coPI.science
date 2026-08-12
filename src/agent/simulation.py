"""Turn-based simulation engine — coordinates all agents across all channels."""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from src.agent.agent import PROFILES_DIR, Agent
from src.agent.channels import SEEDED_CHANNELS
from src.agent.ids import WRITER_ENGINE, TsMinter
from src.agent.message_log import LogEntry, MessageLog
from src.agent.post_types import (
    TERMINAL_POST_TYPES,
    PostTypeSpec,
    available_for,
    eligible_targets,
    render_menu,
    resolve_post_type_name,
)
from src.agent.prompt_safety import delimit
from src.agent.roles import load_role
from src.agent.slack_client import SlackListingIncomplete, ThreadNotFound
from src.agent.specialists import required_domains_for
from src.agent.state import PostRef, ProposalRef, ThreadState
from src.agent.tools import execute_tool, tools_for_role
from src.config import get_settings
from src.models import (
    AgentChannel,
    AgentMessage,
    LlmCallLog,
    OpportunityAssessment,
    ProposalReview,
    SimulationRun,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score
from src.services.cohorts import compute_gates, summarise_gates
from src.services.llm import (
    generate_agent_response,
    generate_with_tools,
    set_call_log_callback,
)

logger = logging.getLogger(__name__)


def _visibility_permits(origin: str, current: str) -> bool:
    """True iff an origin-visibility record may appear in a current-visibility context.

    Implements the ordering `public < collab_private` from G3:
    - public origins are visible in any context.
    - collab_private origins are visible only in a collab_private context.

    See specs/privacy-and-channel-visibility.md §G3.
    """
    if origin == VISIBILITY_PUBLIC:
        return True
    return current == VISIBILITY_COLLAB_PRIVATE


# A private channel whose newest message is older than this is treated as
# settled: the cursor rewind won't reach back into it. Without this, a single
# stale sibling channel (e.g. an old refinement between the same pair) drags the
# bot's global cursor months into the past. See _rewind_cursors_for_private_channels.
_PRIVATE_CHANNEL_ACTIVE_WINDOW_S = 14 * 24 * 3600  # 14 days


def _strip_reopen_prefix(comment: str) -> str:
    """Strip the ``[Reopened]`` / ``[Reopened via email]`` marker the web/email
    reopen routes prepend to the PI guidance stored in ProposalReview.comment."""
    for prefix in ("[Reopened via email] ", "[Reopened] "):
        if comment.startswith(prefix):
            return comment[len(prefix):]
    return comment


def _restored_slack_ts(row: AgentMessage) -> str | None:
    """Slack ts for a restored ``agent_messages`` row, or None if it has none.

    Restoring this mapping is what lets ``_slack_parent_ts`` tell a Slack-backed
    thread from a DB-origin one after a restart. The column is the only evidence:
    a NULL means the message is not on Slack.

    This used to *infer* a missing mapping — "a row stored against a real Slack
    ``channel_id`` was born on Slack, so its canonical id is its Slack ts" — to
    cover pre-Stage-6 rows written before the mapping was recorded. That
    inference is unsound, because a DB-origin message can also carry a real Slack
    channel id: a PI message written through the web inbox resolves ``channel_id``
    from the ``agent_channels`` row (Slack's id when Slack is on), and so does an
    agent post whose Slack mirror failed. Both mint a *local* canonical id, and
    inferring turns that id into a Slack ts Slack never issued — which
    ``_slack_parent_ts`` then hands to ``chat.postMessage`` as a ``thread_ts``,
    producing an orphan post, a ``ThreadNotFound`` and an evicted thread. Nothing
    in the row distinguishes the two cases, so the guess is now refused.

    Legacy rows are repaired by ``scripts/backfill_slack_ts.py``, a one-time pass
    that asks Slack which timestamps actually exist rather than assuming. Run it
    before deploying this change on a workspace with pre-Stage-6 history.
    """
    return row.slack_ts


# Keywords for channel-profile matching
_CHANNEL_KEYWORDS: dict[str, list[str]] = {
    "drug-repurposing": [
        "drug", "repurpos", "pharmacolog", "therapeutic", "compound",
        "small molecule", "target", "ligand", "polypharmacol",
    ],
    "structural-biology": [
        "structur", "cryo", "crystallograph", "x-ray", "microscop",
        "tomograph", "molecular visualization", "conformation",
    ],
    "aging-and-longevity": [
        "aging", "longevity", "lifespan", "neurodegenerat", "age-related",
        "senescen", "alzheimer", "parkinson",
    ],
    "single-cell-omics": [
        "single-cell", "single cell", "scrna", "transcriptom", "genomic",
        "multiom", "sequencing", "omics",
    ],
    "chemical-biology": [
        "chemical biolog", "proteomics", "chemoproteom", "covalent",
        "activity-based", "abpp", "chemical probe", "mass spectrom",
    ],
}
_UNIVERSAL_CHANNELS = {"general"}

# Slack poll throttles. PI messages come from humans, so sub-turn latency is
# unnecessary; polling every turn was saturating one bot token's rate limit.
CHANNEL_POLL_INTERVAL = 15.0   # seconds between conversations.history sweeps
PROPOSAL_POLL_INTERVAL = 30.0  # seconds between conversations.replies sweeps
ROSTER_POLL_INTERVAL = 30.0    # seconds between AgentRegistry roster re-syncs

# How often to log the reactive:proactive selection split. Starvation under the
# reactive-priority tier should be observable, not inferred.
# See .notes/cohort-system-v2.md §10.3.
SELECTION_RATIO_LOG_EVERY = 100

# Distinguishes "role has no cached rate yet" from "role's cached rate is None
# (no override)". A plain dict.get() default cannot tell those apart, so the
# cache would re-read role.toml from disk on every tick for every default role.
_UNSET = object()

# The DB inbox pollers bound their query to recent rows for performance, but the
# timestamp is stamped at row *creation*, not commit. A row written by another
# process (a PI web message) can therefore become visible only after this process
# has already advanced its cursor past that timestamp — a read-committed
# visibility race that would silently, permanently skip the row (PR #19 review
# H2). To close it, the pollers query a lookback window behind the cursor and
# dedup by identity (the message log for channels, a seen-set for DMs), so a
# late-committing row is re-queried within the window and ingested exactly once.
# Polls are LLM-paced, so the re-scan is cheap; the window is sized far above any
# realistic write-to-commit latency.
#
# The cursor axis is ``created_at``, not ``posted_at`` (R3). posted_at derives
# from the *writing process's* clock (it is float(minted ts)), so a cursor over it
# only works while every writer's clock agrees with the engine's to within this
# window — true on one host, not guaranteed across hosts, and a skewed writer's
# messages would be dropped silently and forever. created_at is
# ``server_default=now()``, i.e. stamped by the single Postgres server, so the
# window depends on one clock only. posted_at remains the *ordering* key for
# conversation content; it is just no longer the delivery cursor.
PI_INBOX_LOOKBACK_S = 300.0
PI_INBOX_LOOKBACK = timedelta(seconds=PI_INBOX_LOOKBACK_S)

# Cursor value meaning "nothing seen yet" — every real created_at sorts after it.
EPOCH_UTC = datetime.fromtimestamp(0, tz=UTC)

# The run's total_messages / total_api_calls are cosmetic counters shown in the
# admin UI. Recomputing total_messages with a full COUNT(*) on every flush is
# wasteful once a run accumulates many rows (B1), so refresh the run-stats row at
# most this often (a final refresh is forced on shutdown). The message rows
# themselves are still upserted every flush.
RUN_STATS_UPDATE_INTERVAL = 30.0

# Startup rebuild window (B2): the MessageLog is hydrated with messages from the
# last REBUILD_WINDOW_S plus the full history of any still-undecided thread, so
# RAM/startup cost grows with recent + live volume rather than all-time history.
# Old *closed* threads are left in the DB and hydrated on demand if a PI reopens
# one (see _hydrate_thread_from_db). Sized to comfortably cover any active
# conversation's lifetime.
REBUILD_WINDOW_S = 14 * 24 * 3600  # 14 days

# Agents exempt from the unreviewed-proposal Phase-5 block — they keep making
# new posts no matter how many of their proposals are awaiting review. Scoped to
# SchultzBot (the reunion host) so he stays active without a human reviewer.
UNBLOCK_EXEMPT_AGENTS = {"schultz"}


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
    ):
        self.agents = {a.agent_id: a for a in agents}
        self.slack_clients = slack_clients
        self.max_runtime_minutes = max_runtime_minutes
        self.budget_cap = budget_cap
        self.session_factory = session_factory
        self.simulation_run_id = simulation_run_id
        self._reset_cursors = reset_cursors
        # When False, the local DB is the sole conversation store and no Slack
        # API calls are made (transports are NullTransport). Drives the roster
        # gate and the DB inbox poller. See specs/local-db-conversations.md.
        self.slack_enabled = slack_enabled

        # role name -> calls_per_load_per_window override (or None). See _calls_per_load.
        self._role_rate_cache: dict[str, int | None] = {}

        # role name -> declared post_types. Same reason as _role_rate_cache
        # above: load_role() hits the disk on every call.
        self._role_post_types_cache: dict[str, tuple[PostTypeSpec, ...]] = {}

        # thread_id -> the specialist domains consulted during that interview.
        # In-memory on purpose: it is read by _persist_assessment one LLM call
        # later, in the SAME process. A restart clears it, and the floor then
        # fails OPEN for threads that predate the restart — see
        # _persist_assessment. Blocking every assessment on every resumed thread
        # would be worse than one unvetted verdict.
        self._specialist_consults: dict[str, set[str]] = {}

        self._start_time: datetime | None = None
        self._running = False
        self.message_log = MessageLog()
        self._pi_slack_id_to_agent_ids: dict[str, list[str]] = {}  # PI slack_user_id -> [agent_ids]
        self._dm_poll_cursors: dict[str, str] = {}  # agent_id -> latest DM ts
        self._pi_handler = None  # Initialized in start() after PI mappings loaded

        # Agent name lookups
        self._bot_name_to_id: dict[str, str] = {
            a.bot_name.lower(): a.agent_id for a in agents
        }
        self.message_log.set_bot_name_map(self._bot_name_to_id)

        # LLM call log buffer
        self._llm_log_buffer: list[dict] = []
        self._llm_log_flush_size = 10

        # Channel ID map (populated during setup)
        self._channel_id_map: dict[str, str] = {}  # name -> id
        # Channel visibility map (populated from agent_channels.visibility
        # during setup; defaults to 'public' for any name not present). Used
        # by G1 prompt scoping and G3 dedup filtering.
        self._channel_visibility: dict[str, str] = {}  # name -> 'public' | 'collab_private'
        # Per-private-channel member-bot set (channel_id -> {agent_id, ...}).
        # Used to route polling/history calls through a bot that can actually
        # see the private channel; non-member bots get channel_not_found from
        # Slack for private channels they aren't in.
        self._private_channel_members: dict[str, set[str]] = {}

        # Slack poll cursor: channel_id -> latest ts seen
        self._poll_cursors: dict[str, str] = {}

        # Closed thread IDs — prevents Phase 3 from re-activating decided threads
        self._closed_thread_ids: set[str] = set()

        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}

        # Thread IDs already reopened via DB-synced PI guidance (rating=0 reviews)
        # to avoid re-processing on every turn.
        self._db_reopened_thread_ids: set[str] = set()

        # Names of collab_private channels whose refinement had converged on a
        # recorded revised proposal (outcome='proposal', origin_visibility=
        # collab_private). The live handshake that finalized these was retired
        # by the pitch-only reconciliation — this set is now populated only at
        # startup/rebuild from legacy ThreadDecision rows, but is still read so
        # a legacy-finalized channel stays closed for further discussion.
        self._finalized_private_channels: set[str] = set()

        # Last-seen mtime of each agent's on-disk profile files (private +
        # public), keyed by agent_id. The web editor runs in a separate process
        # and writes profiles/{private,public}/{id}.md on a shared volume; this
        # process caches profile content per Agent, so a per-turn mtime check
        # tells us when an external edit happened and the cache must be
        # invalidated. See _sync_profiles_from_disk.
        self._profile_mtimes: dict[str, float] = {}

        # Last agent to make an LLM call — prevents the same agent from making
        # back-to-back LLM calls when it's the only active agent.
        self._last_llm_caller: str | None = None

        # Count of consecutive turns granted to the reactive tier (agents that
        # owe a thread reply). Reset when a proactive turn is taken. Bounds how
        # long owed-reply draining can starve new-conversation formation. See
        # _select_agent and settings.max_consecutive_reactive_turns.
        self._reactive_streak: int = 0
        # Running reactive/proactive selection tallies. Logged every
        # SELECTION_RATIO_LOG_EVERY selections so starvation is observable rather
        # than inferred. See .notes/cohort-system-v2.md §10.3.
        self._reactive_selections: int = 0
        self._proactive_selections: int = 0

        # --- Cohort gate bookkeeping (.notes/cohort-system-v2.md) -------------
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

        # Wall-clock throttles for Slack pollers + round-robin cursor over
        # connected clients, so one agent's token doesn't carry all poll load.
        self._last_channel_poll: float = 0.0
        self._last_proposal_poll: float = 0.0
        self._poll_client_cursor: int = 0
        # Last wall-clock time the AgentRegistry roster was re-synced (live
        # add/remove of agents as their status flips). See _sync_roster_from_db.
        self._last_roster_poll: float = 0.0

        # DB persistence buffer for the message log. MessageLog.append fires a
        # sync callback that enqueues here; _flush_persisted() batch-writes to
        # agent_messages once per main-loop tick. This makes the DB the primary
        # conversation store. See specs/local-db-conversations.md.
        self._pending_persist: list[LogEntry] = []
        # Monotonic ts-shaped id minter, seeded at DB rebuild. Owns the engine's
        # writer slot so its ids can never collide with the web app's or
        # GrantBot's, which mint into the same agent_messages table from other
        # processes (R1). See mint_ts and src/agent/ids.py.
        self._ts_minter = TsMinter(WRITER_ENGINE)
        # High-water mark (created_at — the DB server's clock, not any writer's;
        # see PI_INBOX_LOOKBACK_S / R3) for the DB inbound poller: the Slack-
        # independent path by which messages written by other processes (PI web
        # interface, private-channel handover) enter the simulation. See
        # _poll_inbound_from_db.
        self._pi_inbox_cursor: datetime = EPOCH_UTC
        # Slack ts values already represented in the DB (canonical id may differ
        # if a DB-origin message was later mirrored to Slack). Lets the Slack
        # reconcile skip a message it already has. See _rebuild_state_from_slack.
        self._known_slack_ts: set[str] = set()
        # High-water mark (created_at) for the DB DM inbox poller (Slack-off /
        # web PI DMs). See _poll_pi_dms_from_db.
        self._pi_dm_cursor: datetime = EPOCH_UTC
        # Identity dedup for the DM poller's lookback re-scan (ts -> created_at),
        # so a DM is processed exactly once even though the query re-scans a
        # window behind the cursor (H2). Pruned to the lookback window each poll.
        self._pi_dm_seen: dict[str, datetime] = {}
        # Wall-clock of the last cosmetic run-stats refresh (total_messages /
        # total_api_calls), throttled to RUN_STATS_UPDATE_INTERVAL. See
        # _flush_persisted (B1).
        self._last_run_stats_update: float = 0.0
        # Set by request_stop() (the signal handler's sync entry point) to both
        # end the main loop and cut short an in-progress idle-backoff sleep, so
        # the final flush happens well inside the container's stop grace period.
        # See _sleep / request_stop (R2).
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_within_time_limit(self) -> bool:
        if self.max_runtime_minutes <= 0:
            return True  # run forever (until SIGTERM)
        if not self._start_time:
            return True
        elapsed = (datetime.now(UTC) - self._start_time).total_seconds()
        return elapsed < self.max_runtime_minutes * 60

    def _agent_within_budget(self, agent: Agent) -> bool:
        if self.budget_cap <= 0:
            return True  # unlimited
        return agent.api_call_count < self.budget_cap

    def _agent_load(self, agent: Agent) -> int:
        """Concurrent conversational obligations for one agent.

        The shared signal behind BOTH the rate allowance (``_within_rate_limit``)
        and the selection weight (``_select_agent``). Deriving both from one
        number is the point: the failure this fixes was the limiter and the
        scheduler holding contradictory views of what a hub deserves — the
        reactive tier gave the blackbird hub a 7x boost while the cumulative cap
        benched it for 161 consecutive turns, and the cap won, silently. See
        docs/specs/2026-08-06-hub-budget-scheduler-design.md §1.4.

        Floors at 1 so an idle agent stays eligible. Ceilings at
        ``active_thread_threshold`` so nothing can inflate its own allowance past
        the thread cap it is already bound by — that clamp is what stops a
        thread-opening runaway from financing itself (§4.1).
        """
        live = sum(
            1 for t in agent.state.active_threads.values() if t.status == "active"
        )
        return max(1, min(live, get_settings().active_thread_threshold))

    def _calls_per_load(self, agent: Agent) -> int:
        """Per-unit-of-load LLM allowance for this agent's role.

        Cached by role NAME, so an agent flipping roles at runtime simply looks
        up a different key and needs no invalidation. The only staleness is a
        role.toml edited mid-run, which matches get_settings() already being
        lru_cached — both need a container recreate (design §5).

        The cache exists because load_role() reads TOML from disk on every call
        and this runs for every agent on every scheduler tick.
        """
        cached = self._role_rate_cache.get(agent.role, _UNSET)
        if cached is _UNSET:
            cached = load_role(agent.role).calls_per_load_per_window
            self._role_rate_cache[agent.role] = cached
        if cached is not None:
            return cached
        return get_settings().llm_calls_per_load_per_window

    def _within_rate_limit(self, agent: Agent, now: float) -> bool:
        """Sliding-window LLM rate check — the LIVE throttle.

        allowance = _calls_per_load(agent) * _agent_load(agent), over
        llm_rate_window_seconds. Unlike the cumulative cap this replaces, it
        self-heals: entries age out, so an agent throttled now is eligible later.
        See design §4.2.
        """
        allowance = self._calls_per_load(agent) * self._agent_load(agent)
        window_start = now - get_settings().llm_rate_window_seconds
        times = agent.state.call_times
        while times and times[0] < window_start:
            times.popleft()
        ok = len(times) < allowance
        if not ok and not agent.state.throttled:
            logger.warning(
                "[%s] throttled: %d LLM calls in the last %ds at load %d "
                "(allowance %d). Eligible again as the window slides.",
                agent.agent_id, len(times),
                get_settings().llm_rate_window_seconds,
                self._agent_load(agent), allowance,
            )
        agent.state.throttled = not ok
        return ok

    def _active_thread_count(self, agent: Agent) -> int:
        """Count this agent's active threads."""
        return len(agent.state.active_threads)

    def _count_today_posts(self, agent: Agent) -> int:
        """Count top-level posts by this agent in public channels, in the current Pacific time day.

        collab_private channels are flat (every refinement reply is a top-level
        post) and also host PI-initiated handover messages under the bot's
        token. Both are legitimate per the 2-party private-channel design, so
        they must not consume the spam-prevention cap that's scoped to public
        new-conversation posts.
        """
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
        today_start = datetime.now(pacific).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        return sum(
            1 for e in self.message_log.get_agent_top_level_posts(agent.agent_id, limit=100)
            if e.posted_at >= today_start
            and self._channel_visibility.get(e.channel) != VISIBILITY_COLLAB_PRIVATE
        )

    async def start(self) -> None:
        """Run the full simulation."""
        self._start_time = datetime.now(UTC)
        self._running = True
        settings = get_settings()

        logger.info(
            "Simulation started. Max runtime: %dm, Budget: %d calls/agent",
            self.max_runtime_minutes, self.budget_cap,
        )

        # Setup
        self._ensure_seeded_channels()
        await self._persist_seeded_channels()
        # Load any collab_private channels created via the web-UI reopen flow
        # BEFORE rebuilding state so the rebuild's history-fetch loop covers
        # them too — otherwise the handover message wouldn't land in the
        # message log until the first per-turn poll tick.
        await self._sync_private_channels_from_db()
        await self._load_pi_mappings()
        # The DB is the primary conversation store. Register the persist hook,
        # hydrate the log from the DB, then (only when Slack is connected)
        # reconcile with Slack history, and finally reconstruct per-agent state
        # from the combined log. This whole sequence runs with Slack fully off.
        self.message_log.set_persist_callback(self._enqueue_persist)
        await self._rebuild_state_from_db()
        await self._rebuild_state_from_slack()
        await self._rebuild_agent_state()
        await self._seed_pi_dm_cursor()
        # Rebuild advanced last_seen_cursor to max(all_messages), which can
        # overshoot messages in private channels (typically older than the
        # latest public chatter). Rewind member-bot cursors so Phase 2 can
        # still scan the handover and any subsequent private-channel activity.
        self._rewind_cursors_for_private_channels()
        set_call_log_callback(self._on_llm_call)

        # Compute the cohort gate BEFORE the first turn. The rebuild above is
        # deliberately gate-blind (it populates the log and state that every agent
        # shares), so on a resumed run this is where cross-cohort threads inherited
        # from the previous process get grandfathered and stale banked posts get
        # pruned. The loop's roster sync would also reach it (_last_roster_poll
        # starts at 0.0), but doing it here means no turn can ever run with an
        # unset gate while isolation is on. See .notes/cohort-system-v2.md §8.
        await self._recompute_allowed_sender_ids()
        # AFTER the gate, never before: the filter inside reads
        # agent.allowed_sender_ids, which is None until the line above runs.
        self.refresh_lab_directories()
        # Record which topology this run actually started with, so the run's output
        # stays attributable to its configuration (v2 §13.1).
        await self._record_topology_snapshot()

        # Initialize PI handler after mappings are loaded
        from src.agent.pi_handler import PIHandler
        self._pi_handler = PIHandler(
            agents=self.agents,
            slack_clients=self.slack_clients,
            pi_slack_id_to_agent_ids=self._pi_slack_id_to_agent_ids,
            message_log=self.message_log,
            session_factory=self.session_factory,
            simulation_run_id=self.simulation_run_id,
        )

        await self._run_main_loop()

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
        consecutive_idle = 0
        while self._running and self.is_within_time_limit:
            # Poll Slack for PI messages (channels, DMs, and proposal threads).
            # No-ops when Slack is off (NullTransport / no connected clients).
            await self._poll_slack_for_pi_messages()
            await self._poll_pi_dms()
            await self._poll_proposal_threads_for_pi()

            # DB-native inbound path: messages written by other processes (PI
            # web interface, private-channel handover). Runs regardless of Slack,
            # and is how PIs interact when Slack is off.
            await self._poll_inbound_from_db()
            # DB-native PI DM processing (Slack DMs recorded by _poll_pi_dms and
            # web DMs both converge here).
            await self._poll_pi_dms_from_db()

            # Sync proposal reviews and any newly-created private channels from
            # the web app. Both are DB-driven, so a single tick picks them up.
            await self._sync_proposal_reviews_from_db()
            await self._sync_private_channels_from_db()

            # Pick up active/inactive flips (and newly-provisioned tokens) from
            # the DB so the roster changes live, without a process restart.
            await self._sync_roster_from_db()

            # Pick up profile edits made from the web app (separate process).
            self._sync_profiles_from_disk()

            # Select agent
            agent = self._select_agent()
            if agent is None:
                # No agent is currently eligible. Throttling and the per-agent
                # cooldown both lapse with time, so this is normally TRANSIENT:
                # back off and retry rather than ending the run. Only
                # _terminal_stall_reason's two permanent cases stop the loop.
                reason = self._terminal_stall_reason()
                if reason is not None:
                    logger.info("No eligible agent: %s. Stopping.", reason)
                    break
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

            # Prevent the same agent from making back-to-back LLM calls.
            # If this agent was the last to make an LLM call, skip its turn
            # so other agents get a chance (or the simulation idles).
            if self._last_llm_caller == agent.agent_id:
                agent.state.last_selected = time.time()
                consecutive_idle += 1
                delay = self._idle_backoff(consecutive_idle)
                logger.debug(
                    "[%s] Skipped: was last LLM caller (idle backoff: %ds)",
                    agent.agent_id, delay,
                )
                await self._sleep(delay)
                continue

            logger.info("=== Turn %d: %s ===", turn_count + 1, agent.agent_id)

            # Run 5-phase turn
            did_work = False
            try:
                did_work = await self._run_turn(agent)
            except Exception:
                logger.exception("Error during turn for %s", agent.agent_id)

            # Track last agent to make an LLM call. Clear it on an idle turn:
            # the back-to-back guard only needs to block the agent that just
            # *called*. If this turn did no work, leaving the flag set would
            # perpetually skip the OTHER agent while this one idles — a 2-agent
            # livelock. See project_two_agent_scheduler_livelock.
            if did_work:
                self._last_llm_caller = agent.agent_id
            else:
                self._last_llm_caller = None

            # Update last_selected
            agent.state.last_selected = time.time()
            turn_count += 1

            # Idle backoff: if no LLM calls were made, delay before next turn
            if did_work:
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
            # See .notes/cohort-system-v2.md §10.3.

            # Flush buffered message-log entries + LLM logs periodically
            await self._flush_persisted()
            if self._llm_log_buffer:
                await self._flush_llm_logs()

        logger.info("Main loop exited after %d turns", turn_count)

    def request_stop(self) -> None:
        """Ask the main loop to exit — safe to call from a signal handler.

        Deliberately does no I/O: it only flips the flag and wakes any in-flight
        idle-backoff sleep. The flush is done by ``stop()`` on the main
        coroutine's own path (see src/agent/main.py), so it can be awaited to
        completion rather than left in a fire-and-forget task that the
        interpreter may cancel at shutdown (R2).
        """
        self._running = False
        self._stop_event.set()

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
        """
        self._running = False
        self._stop_event.set()
        set_call_log_callback(None)
        await self._flush_persisted(force_stats=True)
        await self._flush_llm_logs()
        logger.info("Simulation stopping...")

    # ------------------------------------------------------------------
    # Agent selection (weighted random)
    # ------------------------------------------------------------------

    def _owes_reply(self, agent: Agent) -> bool:
        """True if the agent has an active thread with a new reply from the other
        party that it hasn't answered yet.

        This is the scheduler-visible signal that drives reactive priority: an
        agent that owes a reply should be selected ahead of the staleness-weighted
        proactive pool, so 1:1 conversations conclude promptly rather than waiting
        for a random re-selection. Reuses the same primitive Phase 4 uses.

        Two cohort rules apply here and nowhere else (v2 §8):

        - **Grandfathered threads are skipped.** A thread whose partner has left the
          cohort still gets answered by Phase 4 so it can conclude, but it must not
          jump the queue ahead of gate-compliant work. Without this the gate and the
          scheduler contradict each other and the scheduler wins.
        - **The remaining threads are read through the agent's gate.** An
          untagged thread with fewer than 2 posters is still open
          (``get_thread_allowed_agents`` returns None) — so a non-cohort third
          party posting into an otherwise legal thread would otherwise
          manufacture reactive priority for a sender the agent is not supposed
          to act on. (Funding threads used to be unconditionally open-to-all
          here too; that exception was removed — ex-funding thread roots now
          follow this same normal rule. See message_log.get_thread_allowed_agents.)
        """
        cursor = agent.state.last_seen_cursor
        for thread in agent.state.active_threads.values():
            if thread.status != "active":
                continue
            if thread.grandfathered:
                continue
            if thread.has_pending_reply or self.message_log.has_new_reply_from_other(
                thread.thread_id, agent.agent_id, cursor,
                allowed_sender_ids=agent.allowed_sender_ids,
            ):
                return True
        return False

    def _turn_eligible(self, agent: Agent, now: float) -> bool:
        """Selection eligibility for one agent.

        - within the LEGACY cumulative cap. Inert by default (``budget_cap``
          defaults to 0, and ``_agent_within_budget`` short-circuits at <= 0);
          armed only when an operator passes ``--budget``. Retained, not removed,
          for back-compat — see design §6;
        - within its sliding-window rate limit. This is the live throttle;
        - past its per-agent cooldown. ``turn_delay_seconds`` throttles an
          individual agent's tempo; enforcing it here (rather than as a global
          ``asyncio.sleep`` after every productive turn) leaves the rest of the
          roster free to act while one agent sits out. See v2 §10.3.
        """
        if not self._agent_within_budget(agent):
            # Ordering is deliberate and must not change: the legacy cap decides
            # eligibility first (that is what keeps the --budget compat tests
            # meaningful). But short-circuiting here also froze
            # ``state.throttled``, so with --budget armed an agent's next genuine
            # throttle transition logged nothing. Evaluate the window check for
            # its SIDE EFFECT (expire old entries, refresh the flag, emit the
            # one-shot warning) and discard the result — eligibility is still
            # decided by the cap alone.
            self._within_rate_limit(agent, now)
            return False
        if not self._within_rate_limit(agent, now):
            return False
        delay = get_settings().turn_delay_seconds
        if delay > 0 and (now - agent.state.last_selected) < delay:
            return False
        return True

    def _select_agent(self) -> Agent | None:
        """Select the next agent to take a turn (sequential — one at a time).

        Two tiers:
        1. **Reactive** — agents that owe a thread reply are chosen first
           (oldest-waiting), so an in-flight 1:1 conversation drains one message
           per turn instead of waiting on random re-selection. The just-called
           agent (`_last_llm_caller`) is excluded so the A→B→A→B baton alternates
           without a wasted skip-tick. A fairness valve
           (`max_consecutive_reactive_turns`, default 3) forces a proactive turn
           after a run of reactive ones so new-conversation formation isn't
           starved — at the original default of 8, a single live pair took 24 of
           27 turns. See .notes/cohort-system-v2.md §10.3.
        2. **Proactive** — staleness-weighted random, scaled by load:
           P(agent) ∝ (now - last_selected) * _agent_load(agent), with a penalty
           for agents that have repeatedly skipped Phase 5
           (weight /= 2^(skips-2) once skips >= 3). The load factor is what makes
           a star's hub — one endpoint of every conversation — draw a share that
           tracks the edges it actually sits on, instead of the 1/N a uniform
           weighting gave it. See design §4.3.

        Both tiers draw from the same eligibility pool (`_turn_eligible`): budget
        plus the per-agent `turn_delay_seconds` cooldown.
        """
        settings = get_settings()
        now = time.time()
        candidates = [a for a in self.agents.values() if self._turn_eligible(a, now)]
        if not candidates:
            return None

        # --- Reactive tier: drain owed replies fast ------------------------
        if self._reactive_streak < settings.max_consecutive_reactive_turns:
            owed = [
                a for a in candidates
                if a.agent_id != self._last_llm_caller and self._owes_reply(a)
            ]
            if owed:
                self._reactive_streak += 1
                self._reactive_selections += 1
                self._log_selection_ratio()
                # Weighted by load, NOT bare last_selected. The hub is selected
                # often, so its last_selected is always recent — under
                # min(last_selected) it lost every tiebreak to a long-idle spoke,
                # i.e. it was penalised precisely for being the busiest agent.
                # Still "longest wait wins", now scaled by obligation count.
                # See design §1.3 / §4.3.
                return max(
                    owed,
                    key=lambda a: (now - a.state.last_selected) * self._agent_load(a),
                )

        # --- Proactive tier: staleness-weighted random ---------------------
        self._reactive_streak = 0
        self._proactive_selections += 1
        self._log_selection_ratio()
        weights = []
        for a in candidates:
            w = max(now - a.state.last_selected, 1.0) * self._agent_load(a)
            skips = a.state.consecutive_phase5_skips
            if skips >= 3:
                w /= 2 ** (skips - 2)
            weights.append(w)
        return random.choices(candidates, weights=weights, k=1)[0]

    def _log_selection_ratio(self) -> None:
        """Log the reactive:proactive split every SELECTION_RATIO_LOG_EVERY picks."""
        total = self._reactive_selections + self._proactive_selections
        if total and total % SELECTION_RATIO_LOG_EVERY == 0:
            logger.info(
                "[sched] selections: %d reactive / %d proactive (%.0f%% reactive, "
                "valve=%d)",
                self._reactive_selections, self._proactive_selections,
                100.0 * self._reactive_selections / total,
                get_settings().max_consecutive_reactive_turns,
            )

    # ------------------------------------------------------------------
    # Turn execution (5 phases)
    # ------------------------------------------------------------------

    async def _run_turn(self, agent: Agent) -> bool:
        """Run all 5 phases for a single agent turn. Returns True if work was done."""
        settings = get_settings()
        api_calls_before = agent.api_call_count

        # Phase 1: Channel discovery
        self._phase1_channel_discovery(agent)

        # Phase 3: Activate threads from tags and replies
        self._phase3_activate_threads(agent)

        # Phase 4: Reply to active threads (parallel)
        phase4_thread_ids = await self._phase4_reply_threads(agent)

        # Phase 4 activity resets skip backoff — agent is actively engaged
        if phase4_thread_ids:
            agent.state.consecutive_phase5_skips = 0
            agent.state.last_phase5_action_time = time.time()

        # State-change gate: skip Phase 5 (no LLM call) unless there's
        # new actionable state or the spontaneous post timer has expired.
        has_interesting = len(agent.state.interesting_posts) > 0
        has_phase4_work = len(phase4_thread_ids) > 0
        has_pi = agent.state.has_pi_directive

        # Spontaneous post timer — allow one Phase 5 call after enough
        # idle time so agents can organically start new conversations.
        base_interval = settings.phase5_spontaneous_interval * 60  # to seconds
        skips = agent.state.consecutive_phase5_skips
        stretch = min(max(skips, 1), settings.phase5_spontaneous_interval_max_multiplier)
        spontaneous_interval = base_interval * stretch
        since_last_action = time.time() - agent.state.last_phase5_action_time
        spontaneous_ready = since_last_action >= spontaneous_interval

        has_new_work = has_interesting or has_phase4_work or has_pi

        if has_new_work or spontaneous_ready:
            await self._phase5_new_post(agent, phase4_thread_ids)
        else:
            logger.debug(
                "[%s] Phase 5: Skipped (no state change, spontaneous in %ds)",
                agent.agent_id,
                int(spontaneous_interval - since_last_action),
            )

        # Clear PI directive flag after the turn
        agent.state.has_pi_directive = False

        # Update cursor
        agent.state.last_seen_cursor = time.time()

        return agent.api_call_count > api_calls_before

    # ------------------------------------------------------------------
    # Phase 1: Channel Discovery
    # ------------------------------------------------------------------

    def _phase1_channel_discovery(self, agent: Agent) -> None:
        """Join new channels based on profile keyword matching."""
        profile_text = agent.public_profile.lower()
        channels_to_join = set(_UNIVERSAL_CHANNELS)

        for channel_name, keywords in _CHANNEL_KEYWORDS.items():
            if any(kw in profile_text for kw in keywords):
                channels_to_join.add(channel_name)

        new_channels = channels_to_join - agent.state.subscribed_channels
        if new_channels:
            for ch_name in new_channels:
                ch_id = self._channel_id_map.get(ch_name)
                if ch_id:
                    client = self.slack_clients.get(agent.agent_id)
                    if client:
                        client.join_channel(ch_id)
            agent.state.subscribed_channels.update(new_channels)
            logger.info("[%s] Phase 1: Joined channels: %s", agent.agent_id, new_channels)

    # ------------------------------------------------------------------
    # Phase 2: Scan & Filter
    # ------------------------------------------------------------------

    async def _phase2_scan_filter(self, agent: Agent) -> None:
        """Scan new top-level posts and decide which to add to interesting_posts.

        Disabled: no caller since the pitch-only reconciliation; prompts/phase2-*.md
        carry the matching preamble.
        """
        settings = get_settings()

        # Get new top-level posts since agent's last turn
        new_posts = self.message_log.get_new_top_level_posts(
            since=agent.state.last_seen_cursor,
            channels=agent.state.subscribed_channels,
            exclude_agent_id=agent.agent_id,
            allowed_sender_ids=agent.allowed_sender_ids,
        )

        # Exclude posts already in interesting_posts or active_threads
        known_ids = {p.post_id for p in agent.state.interesting_posts}
        known_ids.update(agent.state.active_threads.keys())
        new_posts = [p for p in new_posts if p.ts not in known_ids]

        if not new_posts:
            logger.debug("[%s] Phase 2: No new posts to evaluate", agent.agent_id)
            return

        # Build post data for LLM
        post_dicts = [
            {
                "post_id": p.ts,
                "channel": p.channel,
                "sender": p.sender_name,
                "content_snippet": p.content,
            }
            for p in new_posts
        ]

        system_prompt, messages = agent.build_phase2_scan_prompt(post_dicts)

        agent.record_api_call()
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500,
                log_meta={"agent_id": agent.agent_id, "phase": "scan"},
                on_retry=agent.record_api_call,
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 2: Empty response from LLM, skipping", agent.agent_id)
                return
            result = _extract_json(response)
            selected_ids = set(result.get("selected_post_ids", []))

            # Add selected posts to interesting_posts
            for post in new_posts:
                if post.ts in selected_ids:
                    agent.state.interesting_posts.append(PostRef(
                        post_id=post.ts,
                        channel=post.channel,
                        sender_agent_id=post.sender_agent_id or post.sender_name,
                        content_snippet=post.content[:200],
                        posted_at=post.posted_at,
                    ))

            logger.info(
                "[%s] Phase 2: Evaluated %d posts, added %d to interesting",
                agent.agent_id, len(new_posts), len(selected_ids),
            )
        except Exception as exc:
            logger.error("[%s] Phase 2 scan failed: %s", agent.agent_id, exc)

        # Prune if over cap
        if len(agent.state.interesting_posts) > settings.interesting_posts_cap:
            await self._phase2_prune(agent)

    async def _phase2_prune(self, agent: Agent) -> None:
        """Prune interesting_posts to ≤ cap.

        Disabled: no caller since the pitch-only reconciliation; prompts/phase2-*.md
        carry the matching preamble.
        """
        system_prompt, messages = agent.build_phase2_prune_prompt()

        agent.record_api_call()
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500,
                log_meta={"agent_id": agent.agent_id, "phase": "prune"},
                on_retry=agent.record_api_call,
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 2 prune: empty response", agent.agent_id)
                return
            result = _extract_json(response)
            keep_ids = set(result.get("keep_post_ids", []))

            before = len(agent.state.interesting_posts)
            agent.state.interesting_posts = [
                p for p in agent.state.interesting_posts if p.post_id in keep_ids
            ]
            logger.info(
                "[%s] Phase 2 prune: %d → %d",
                agent.agent_id, before, len(agent.state.interesting_posts),
            )
        except Exception as exc:
            logger.error("[%s] Phase 2 prune failed: %s", agent.agent_id, exc)

    # ------------------------------------------------------------------
    # Phase 3: Activate Threads from Tags
    # ------------------------------------------------------------------

    def _phase3_activate_threads(self, agent: Agent) -> None:
        """
        Auto-activate threads where this agent was tagged or
        where someone replied to this agent's top-level posts.

        Skipped entirely for entries in collab_private channels: those channels
        are flat discussions (no threading), so tags and replies there are
        just content for Phase 2/5 to consider, not thread-activation signals.
        """
        cursor = agent.state.last_seen_cursor

        # Check for tags
        tagged_entries = self.message_log.get_tags_for_agent(
            agent.bot_name, cursor, allowed_sender_ids=agent.allowed_sender_ids
        )
        for entry in tagged_entries:
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
            new_posts = self.message_log.get_new_top_level_posts(
                since=cursor,
                channels=agent.state.subscribed_channels,
                exclude_agent_id=agent.agent_id,
                allowed_sender_ids=agent.allowed_sender_ids,
            )
            for entry in new_posts:
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
                    )
                    logger.info(
                        "[%s] Phase 3: Auto-activated interview thread %s (lab post by %s)",
                        agent.agent_id, thread_id, other_id,
                    )

    # ------------------------------------------------------------------
    # Phase 4: Reply to Active Threads (parallel)
    # ------------------------------------------------------------------

    async def _phase4_reply_threads(self, agent: Agent) -> set[str]:
        """Reply to all active threads that have a pending reply from the other agent.

        Returns the set of thread IDs that were replied to (so Phase 5 can skip them).
        """
        settings = get_settings()

        # Identify threads needing a reply
        threads_to_reply: list[ThreadState] = []
        for thread in agent.state.active_threads.values():
            if thread.status != "active":
                continue
            # Safety net: Phase 4 does threaded replies, which are never the
            # right thing in a collab_private channel. Skip any active_thread
            # that somehow ended up pointing at a private channel — Phase 2/5
            # handle those flat.
            if self._channel_visibility.get(thread.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            # Check if there's a new reply from the other agent. Read UNGATED
            # (allowed_sender_ids=None) on purpose: this thread is already open, so
            # it is entitled to conclude even if the partner has since dropped out
            # of the cohort — abandoning it mid-flight would waste every call
            # already spent on it, and thread participation rules already bound who
            # may post here. What a grandfathered thread does NOT get is reactive
            # *priority*; that is enforced in _owes_reply. See v2 §8.
            has_new = self.message_log.has_new_reply_from_other(
                thread.thread_id, agent.agent_id, agent.state.last_seen_cursor,
                allowed_sender_ids=None,
            )
            if has_new:
                # Genuine new reply from the other agent — reset empty-response
                # backoff so we give the thread a fresh attempt.
                thread.empty_response_count = 0
            if has_new or thread.has_pending_reply:
                # Promote to durable flag so a failed/empty/exception reply
                # attempt is retried on the next turn. The cursor advances
                # unconditionally each turn, so has_new can't be relied on
                # for retry — only has_pending_reply persists. Successful
                # replies clear this back to False.
                thread.has_pending_reply = True
                threads_to_reply.append(thread)

        if not threads_to_reply:
            logger.debug("[%s] Phase 4: No threads needing reply", agent.agent_id)
            return set()

        logger.info(
            "[%s] Phase 4: Replying to %d threads",
            agent.agent_id, len(threads_to_reply),
        )

        # Run replies in parallel
        tasks = [
            self._reply_to_thread(agent, thread)
            for thread in threads_to_reply
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        return {t.thread_id for t in threads_to_reply}

    async def _reply_to_thread(self, agent: Agent, thread: ThreadState) -> None:
        """Compose and post a reply to a single thread."""
        settings = get_settings()

        # Get thread history from message log
        history_entries = self.message_log.get_thread_history(thread.thread_id)
        thread_history = [
            {"sender": e.sender_name, "content": e.content}
            for e in history_entries
        ]

        # Update message count (subtract offset for PI-reopened threads)
        thread.message_count = len(history_entries) - thread.message_count_offset

        # Final participation check before composing a reply
        allowed = self.message_log.get_thread_allowed_agents(thread.thread_id)
        if allowed and agent.agent_id not in allowed:
            logger.info(
                "[%s] Phase 4: Aborting reply to thread %s — not in allowed set %s",
                agent.agent_id, thread.thread_id, allowed,
            )
            agent.state.active_threads.pop(thread.thread_id, None)
            return

        # Check for system-enforced close
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

        # Build prompt
        system_prompt, messages = agent.build_phase4_prompt(
            thread=thread,
            thread_history=thread_history,
            other_agent_name=other_name,
            other_agent_lab=other_lab,
            visibility=thread_visibility,
            channel_id=thread_channel_id,
        )

        # Create tool executor bound to this thread's state
        async def tool_executor(tool_name: str, tool_input: dict) -> str:
            return await execute_tool(
                tool_name, tool_input, agent.agent_id, thread, role=agent.role,
                on_consult=lambda domain, _pi=thread.other_agent_id: self._record_consult(
                    _pi, domain
                ),
            )

        agent.record_api_call()
        try:
            response_text = await generate_with_tools(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools_for_role(agent.role),
                tool_executor=tool_executor,
                model=settings.llm_agent_model_opus,
                max_tokens=1500,
                log_meta={
                    "agent_id": agent.agent_id,
                    "phase": "thread_reply",
                    "channel": thread.channel,
                },
                on_retry=agent.record_api_call,
            )

            # Extract message from <slack_message> tags, fall back to preamble stripping
            response_text = _extract_slack_message(response_text)

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
                return

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
                logger.info(
                    "[%s] Phase 4: reply to thread %s suppressed — not "
                    "counted, nothing persisted",
                    agent.agent_id, thread.thread_id,
                )
                return
            agent.message_count += 1
            thread.has_pending_reply = False
            thread.empty_response_count = 0

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
        still a valid ThreadDecision.outcome value for legacy rows and is
        still handled by _close_thread/admin routes/ProposalReview, but
        nothing in this method can produce a new one.
        """
        # Check for ⏸️ — explicit "no viable collaboration" signal
        if "⏸️" in latest_reply or ":pause_button:" in latest_reply:
            logger.info(
                "[%s] Thread %s: ⏸️ no-proposal close",
                agent.agent_id, thread.thread_id,
            )
            await self._close_thread(agent, thread, "no_proposal")

    async def _close_thread(
        self,
        agent: Agent,
        thread: ThreadState,
        outcome: str,
        summary_text: str | None = None,
    ) -> None:
        """Close a thread and log the decision."""
        thread.status = "closed"
        self._closed_thread_ids.add(thread.thread_id)

        # Track for Phase 5 dedup context
        pair_key = tuple(sorted([agent.agent_id, thread.other_agent_id]))
        self._prior_threads.setdefault(pair_key, []).append({
            "channel": thread.channel,
            "outcome": outcome,
            "summary": (summary_text or "")[:400] or None,
        })
        # Remove from active threads
        agent.state.active_threads.pop(thread.thread_id, None)

        # Also close for the other agent if they have this thread active
        other_agent = self.agents.get(thread.other_agent_id)
        if other_agent and thread.thread_id in other_agent.state.active_threads:
            other_agent.state.active_threads[thread.thread_id].status = "closed"
            other_agent.state.active_threads.pop(thread.thread_id, None)
            # If proposal, add to other agent's pending_proposals too.
            # Replace any existing entry for the same thread so reopen/re-propose
            # cycles don't accumulate duplicates during a single run.
            if outcome == "proposal" and summary_text:
                other_agent.state.pending_proposals = [
                    p for p in other_agent.state.pending_proposals
                    if p.thread_id != thread.thread_id
                ]
                other_agent.state.pending_proposals.append(ProposalRef(
                    thread_id=thread.thread_id,
                    channel=thread.channel,
                    other_agent_id=agent.agent_id,
                    summary_text=summary_text,
                    proposed_at=time.time(),
                ))

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
                    )
                    db.add(decision)
                    await db.commit()
            except Exception as exc:
                logger.warning("Failed to log thread decision: %s", exc)

        logger.info(
            "[%s] Thread %s closed: %s",
            agent.agent_id, thread.thread_id, outcome,
        )

        # Notify PI via DM
        if self._pi_handler:
            try:
                await self._pi_handler.notify_thread_conclusion(
                    agent.agent_id, thread, outcome, summary_text,
                )
            except Exception as exc:
                logger.debug("Failed to notify PI of thread conclusion: %s", exc)

        # Update working memory for both agents
        # summary_text is derived from a cross-agent conversation, so fence it
        # as untrusted before it lands in working memory (which is later fed
        # back into prompts) (SEC-14).
        event = f"Thread in #{thread.channel} with {thread.other_agent_id} closed: {outcome}"
        if summary_text:
            event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
        await self._update_agent_memory(agent, event)
        if other_agent:
            other_event = f"Thread in #{thread.channel} with {agent.agent_id} closed: {outcome}"
            if summary_text:
                other_event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
            await self._update_agent_memory(other_agent, other_event)

    def _evict_dead_thread(self, thread_id: str) -> None:
        """Remove a thread_id from every agent's in-memory state.

        Fires when Slack reports the parent message no longer exists (via
        ThreadNotFound from conversations.replies or a silent thread_ts drop
        on chat.postMessage). Without eviction the same dead thread gets
        re-polled and replied-to forever, producing noisy error logs and —
        worse — cascading top-level posts.
        """
        evicted_from = 0
        for ag in self.agents.values():
            removed = False
            if thread_id in ag.state.active_threads:
                ag.state.active_threads.pop(thread_id, None)
                removed = True
            before = len(ag.state.interesting_posts)
            ag.state.interesting_posts = [
                p for p in ag.state.interesting_posts if p.post_id != thread_id
            ]
            if len(ag.state.interesting_posts) != before:
                removed = True
            before = len(ag.state.pending_proposals)
            ag.state.pending_proposals = [
                p for p in ag.state.pending_proposals if p.thread_id != thread_id
            ]
            if len(ag.state.pending_proposals) != before:
                removed = True
            if removed:
                evicted_from += 1
        self._poll_cursors.pop(f"proposal_thread:{thread_id}", None)
        self._closed_thread_ids.discard(thread_id)
        if evicted_from:
            logger.info(
                "Evicted dead thread %s from %d agent(s)' state",
                thread_id, evicted_from,
            )

    async def _sync_private_channels_from_db(self) -> None:
        """Discover collab_private channels created via the web-UI reopen flow.

        Queries ``agent_channels`` for rows with ``visibility='collab_private'``
        and integrates each new one into the engine state:

        - Adds to ``_channel_id_map`` and ``_channel_visibility``.
        - Adds the channel name to every member bot's ``subscribed_channels``
          (resolved from ``private_channel_members``), so Phase 2 scans it and
          Phase 4/5 can act in it.
        - Seeds a poll cursor so the first poll picks up the handover message.

        Cheap to call every main-loop tick — a single query returning a handful
        of rows. Idempotent: channels already known are skipped.
        """
        if not self.session_factory:
            return
        try:
            from sqlalchemy import select as sa_select

            from src.models import AgentChannel, PrivateChannelMember

            async with self.session_factory() as db:
                priv_rows = (await db.execute(
                    sa_select(AgentChannel).where(
                        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE,
                        AgentChannel.archived_at.is_(None),
                    )
                )).scalars().all()

                # Integrate each channel we haven't seen yet.
                newly_discovered: list[AgentChannel] = []
                for ac in priv_rows:
                    if ac.channel_name in self._channel_id_map:
                        continue
                    self._channel_id_map[ac.channel_name] = ac.channel_id
                    self._channel_visibility[ac.channel_name] = VISIBILITY_COLLAB_PRIVATE
                    newly_discovered.append(ac)

                if not newly_discovered:
                    return

                # Load bot memberships for the newly-discovered channels.
                new_ids = [ac.id for ac in newly_discovered]
                members = (await db.execute(
                    sa_select(PrivateChannelMember).where(
                        PrivateChannelMember.agent_channel_id.in_(new_ids),
                        PrivateChannelMember.role == "bot",
                        PrivateChannelMember.removed_at.is_(None),
                    )
                )).scalars().all()

                by_channel: dict[uuid.UUID, list[str]] = {}
                for m in members:
                    if m.agent_id:
                        by_channel.setdefault(m.agent_channel_id, []).append(m.agent_id)

                for ac in newly_discovered:
                    bot_ids = by_channel.get(ac.id, [])
                    logger.info(
                        "Discovered private channel #%s (id=%s); subscribing bots: %s",
                        ac.channel_name, ac.channel_id, bot_ids,
                    )
                    for aid in bot_ids:
                        agent = self.agents.get(aid)
                        if agent:
                            agent.state.subscribed_channels.add(ac.channel_name)
                    # Record membership so polling/history calls for this
                    # private channel route through a bot that can see it.
                    self._private_channel_members[ac.channel_id] = set(bot_ids)
                    # Share channel name↔id with every client cache so post_message
                    # can resolve the name if one is passed.
                    for c in self.slack_clients.values():
                        c.cache_channel_ids({ac.channel_name: ac.channel_id})

            # Cursor rewind — scoped to the channels discovered in THIS pass.
            # A broad rewind across all known private channels would drag
            # unrelated bots' cursors back every time any new private channel
            # appears (observed: discovering priv-lairson-su was rewinding
            # lotz's cursor back into priv-lotz-su territory).
            discovered_channel_ids = [ac.channel_id for ac in newly_discovered]
            self._rewind_cursors_for_private_channels(
                only_channel_ids=discovered_channel_ids
            )

        except Exception as exc:
            logger.warning("Failed to sync private channels from DB: %s", exc)

    def _rewind_cursors_for_private_channels(
        self,
        only_channel_ids: list[str] | None = None,
    ) -> None:
        """Rewind member bots' cursors just enough to scan *unread* private-channel
        messages, without dragging them back into settled channels.

        For every tracked collab_private channel (or the subset in
        ``only_channel_ids``), and for each member bot, rewind the bot's
        ``last_seen_cursor`` to just before the oldest message in that channel
        that the bot has **not yet acted on** — i.e. the oldest message newer
        than the bot's own most recent post there. Two key constraints keep the
        rewind tight:

        - **Settled channels are skipped.** A channel whose newest message is
          older than ``_PRIVATE_CHANNEL_ACTIVE_WINDOW_S`` is considered done;
          rewinding into it would resurrect a long-dead conversation (this was
          the bug: a 2-month-old sibling channel pulled the global cursor back
          ~2 months, burying a fresh handover under a huge Phase-2 backlog).
        - **Caught-up bots are skipped.** If a bot has already posted after the
          newest message in a channel, it has nothing to scan there.

        The cursor only ever moves backward, and only to the minimum needed
        across the bot's active channels. No-op when the log has no messages
        for a target channel yet (discovery fired before the poll populated it).

        Call with ``only_channel_ids=None`` at startup, after rebuild, to cover
        all known private channels. For per-tick discoveries, pass the list of
        newly-discovered channel IDs so already-scanned channels aren't revisited.
        """
        if not self._private_channel_members:
            return
        target_ids = (
            set(only_channel_ids) if only_channel_ids is not None
            else set(self._private_channel_members.keys())
        )
        if not target_ids:
            return

        # cid -> list of (posted_at, sender_agent_id) for target channels.
        msgs_by_cid: dict[str, list[tuple[float, str | None]]] = {}
        for entry in self.message_log._entries:
            cid = self._channel_id_map.get(entry.channel)
            if not cid or cid not in target_ids:
                continue
            msgs_by_cid.setdefault(cid, []).append((entry.posted_at, entry.sender_agent_id))

        now = time.time()
        # agent_id -> lowest rewind target across its active private channels.
        rewind_targets: dict[str, float] = {}
        for cid, msgs in msgs_by_cid.items():
            newest = max(p for p, _ in msgs)
            if now - newest > _PRIVATE_CHANNEL_ACTIVE_WINDOW_S:
                continue  # settled channel — leave the cursor alone
            for aid in self._private_channel_members.get(cid, set()):
                if aid not in self.agents:
                    continue
                bot_last = max(
                    (p for p, s in msgs if s == aid), default=float("-inf"),
                )
                unacted = [p for p, _ in msgs if p > bot_last]
                if not unacted:
                    continue  # bot has posted after everything here — caught up
                target = min(unacted) - 0.001  # just before, so "> cursor" includes it
                if aid not in rewind_targets or target < rewind_targets[aid]:
                    rewind_targets[aid] = target

        for aid, target in rewind_targets.items():
            agent = self.agents[aid]
            if agent.state.last_seen_cursor > target:
                logger.info(
                    "[%s] Rewinding last_seen_cursor %.3f -> %.3f to scan private channel",
                    aid, agent.state.last_seen_cursor, target,
                )
                agent.state.last_seen_cursor = target

    def _resolve_channel_visibility(self, channel_name: str) -> str:
        """Look up the visibility class of a channel by its name.

        Backed by an in-memory map (``self._channel_visibility``) populated
        alongside ``self._channel_id_map`` at rebuild/bootstrap time. Defaults
        to VISIBILITY_PUBLIC when the channel is not tracked (e.g., seeded
        channels before their AgentChannel row is created).
        """
        return self._channel_visibility.get(channel_name, VISIBILITY_PUBLIC)

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

    async def _phase5_new_post(self, agent: Agent, phase4_thread_ids: set[str] | None = None) -> None:
        """Optionally start a new thread or reply to an interesting post."""
        settings = get_settings()
        phase4_thread_ids = phase4_thread_ids or set()

        # Stamp the spontaneous-post timer up front: consulting Phase 5 consumes
        # the opportunity regardless of whether we end up posting, skipping, or
        # bailing out early. Without this, a "skip" leaves the timer stale and
        # every subsequent turn re-fires Phase 5, burning an LLM call per turn.
        agent.state.last_phase5_action_time = time.time()

        # Daily post cap — pi_lab is capped to one pitch per day (design §9);
        # other roles (e.g. scout_hub) keep the general cap.
        today_posts = self._count_today_posts(agent)
        cap = settings.lab_daily_post_cap if agent.role == "pi_lab" else settings.daily_post_cap
        if today_posts >= cap:
            logger.debug("[%s] Phase 5: Skipped (daily cap %d/%d)", agent.agent_id, today_posts, cap)
            return

        # Check preconditions
        at_thread_threshold = self._active_thread_count(agent) >= settings.active_thread_threshold
        unreviewed_count = sum(
            1 for p in agent.state.pending_proposals
            if not p.reviewed
        )
        has_unreviewed = (
            agent.agent_id not in UNBLOCK_EXEMPT_AGENTS
            and unreviewed_count >= settings.unreviewed_proposal_block_count
        )
        blocked_for_regular = at_thread_threshold or has_unreviewed

        # Check for PI-priority posts — these bypass random skip and blocking
        has_pi_priority = any(p.pi_priority for p in agent.state.interesting_posts)

        if not has_pi_priority and random.random() < settings.phase5_skip_probability:
            logger.debug("[%s] Phase 5: Skipped (random)", agent.agent_id)
            return

        # Filter out interesting posts that are already active threads (replied in Phase 4)
        # or that already have a thread with another agent (2-party limit)
        available_posts = []
        for post in agent.state.interesting_posts:
            if post.post_id in phase4_thread_ids:
                continue
            if post.post_id in agent.state.active_threads:
                continue

            # Used below for the flat-channel turn-taking rule. Private posts
            # get no other special treatment here — the reconciliation
            # retired the bypass that let them skip the unreviewed-proposal
            # block along with the refinement flow it existed for.
            is_private = (
                self._channel_visibility.get(post.channel) == VISIBILITY_COLLAB_PRIVATE
            )

            # A private channel whose refinement already converged on a
            # recorded revised proposal (legacy rows only — see
            # _finalized_private_channels) is closed for further discussion.
            if post.channel in self._finalized_private_channels:
                continue

            # PI-priority posts bypass regular blocking.
            if blocked_for_regular and not post.pi_priority:
                continue

            # Turn-taking in flat private channels: don't reply if we were
            # the most recent bot to post there. Wait for the other bot.
            if is_private and (
                self.message_log.get_last_bot_sender_in_channel(post.channel)
                == agent.agent_id
            ):
                logger.debug(
                    "[%s] Phase 5: Skipping private-channel post %s — we were last to post in #%s",
                    agent.agent_id, post.post_id, post.channel,
                )
                continue

            # Check thread participation rules: if the post tags a specific agent,
            # only that agent can reply; otherwise generic 2-party rule applies
            allowed = self.message_log.get_thread_allowed_agents(post.post_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                logger.debug(
                    "[%s] Phase 5: Skipping post %s — not in allowed set %s",
                    agent.agent_id, post.post_id, allowed,
                )
                continue
            available_posts.append(post)

        # A blocked agent with nothing to reply to normally has nothing to do,
        # and bailing here saves an LLM call. But "nothing to reply to" is not
        # the same as "nothing to post": a hub saturated with interviews still
        # owes an assessment for each one it finished, and that is precisely
        # the state this early return used to strand it in. Ask the post-type
        # layer whether anything is actually postable before giving up.
        nothing_postable = not self._available_post_types(
            agent, restricted=blocked_for_regular
        )
        if not available_posts and blocked_for_regular and nothing_postable:
            logger.debug("[%s] Phase 5: Skipped (blocked, nothing postable)", agent.agent_id)
            return

        # Temporarily replace interesting_posts for prompt building
        original_posts = agent.state.interesting_posts
        agent.state.interesting_posts = available_posts

        # Build prompt — include agent's recent posts for dedup
        recent_entries = self.message_log.get_agent_top_level_posts(agent.agent_id, limit=10)
        recent_posts = [
            {"channel": e.channel, "content_snippet": e.content[:150]}
            for e in recent_entries
        ]

        # Resolve the visibility context for the prompt. When the agent's only
        # actionable posts are in a private channel, build the prompt in that
        # channel's context so the right private-memory segment is injected
        # (build_phase5_prompt -> build_system_prompt) and the prior-threads
        # dedup context is filtered for that visibility (G3). Mixed/empty
        # cases stay public (the default for new public posts).
        private_available = [
            p for p in available_posts
            if self._channel_visibility.get(p.channel) == VISIBILITY_COLLAB_PRIVATE
        ]
        public_available = [
            p for p in available_posts
            if self._channel_visibility.get(p.channel) != VISIBILITY_COLLAB_PRIVATE
        ]
        private_channel_id = None
        if private_available and not public_available:
            current_visibility = VISIBILITY_COLLAB_PRIVATE
            private_channel_id = self._channel_id_map.get(private_available[0].channel)
        else:
            current_visibility = VISIBILITY_PUBLIC
        prior_threads = self._get_prior_threads_for_agent(
            agent.agent_id, current_visibility=current_visibility,
        )

        available_types = self._available_post_types(
            agent, restricted=blocked_for_regular,
        )
        if not available_types and not blocked_for_regular:
            # Not restricted, yet nothing satisfies role ∩ topology — either a
            # misconfigured role.toml or a cohort gate that leaves this agent
            # with no reachable counterparty for anything it declares. Quiet
            # for a blocked agent (restricted=True): an empty menu there is
            # the expected, unremarkable shape for e.g. a pi_lab agent with no
            # terminal type to report (design §6 C4).
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
            visibility=current_visibility,
            channel_id=private_channel_id,
            post_type_menu=post_type_menu,
        )

        # Restore
        agent.state.interesting_posts = original_posts

        agent.record_api_call()
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                model=settings.llm_agent_model_opus,
                # scout_hub's opportunity assessment is an 11-section body
                # plus a ~15-line <assessment_json> sidecar emitted LAST, so
                # truncation drops the machine-readable verdict first while
                # still leaving the Slack post looking complete (F8). 1000
                # was sized for a short reply/skip decision, not this
                # artifact. NOTE: src/services/llm.py's retry-at-2x path logs
                # loudly (logger.error) if the retry ALSO truncates, but it
                # does not retry again — this ceiling still needs to be big
                # enough that truncation stops being the common case.
                max_tokens=2500,
                log_meta={"agent_id": agent.agent_id, "phase": "new_post"},
                on_retry=agent.record_api_call,
            )
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
            # the just-reset 0, to feed _select_next_agent's damping
            # (`skips >= 3`). Every remaining rejection path (unsupported
            # action, post-type rejection, body-mention rejection) restores it
            # correctly via `previous_skips + 1`.
            previous_skips = agent.state.consecutive_phase5_skips
            agent.state.consecutive_phase5_skips = 0
            agent.state.last_phase5_action_time = time.time()

            channel = action_data.get("channel", "general").lstrip("#")
            post_type = action_data.get("post_type", "")

            # If agent is blocked, only allow a terminal artifact reporting
            # finished work — the start-new-work backpressure does not apply
            # to it. See TERMINAL_POST_TYPES.
            if blocked_for_regular:
                is_terminal_post = (
                    action == "new_post" and post_type in TERMINAL_POST_TYPES
                )
                if not is_terminal_post:
                    logger.info(
                        "[%s] Phase 5: Blocked action while proposals pending",
                        agent.agent_id,
                    )
                    return

            # Retroactively add channel to the LLM log entry (unknown at call time)
            if self._llm_log_buffer:
                self._llm_log_buffer[-1]["channel"] = channel

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
            tags_stripped_before = self._cohort_tags_stripped.get(agent.agent_id, 0)
            message_text = self._strip_disallowed_tags(message_text, agent)
            body_mention_was_stripped = (
                self._cohort_tags_stripped.get(agent.agent_id, 0) > tags_stripped_before
            )

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
                # be a phantom record with no corresponding Slack message
                # (Task 11 fix round 1, Finding 3).
                logger.info(
                    "[%s] Phase 5: New post in #%s suppressed — not counted, "
                    "nothing persisted",
                    agent.agent_id, channel,
                )
            else:
                agent.message_count += 1

                # A :mag: Opportunity Assessment carries a machine-readable
                # verdict sidecar (stripped from the Slack body). Persist it —
                # the whole point of the artifact is that staff can triage it
                # later. ``verdict`` can legitimately be ``{}`` (an empty but
                # parsed sidecar) — that is falsy but not a failure, so the
                # gate is `is not None`, never a truthiness check (Finding 1).
                if post_type == "opportunity_assessment":
                    verdict = _extract_assessment_json(response)
                    if verdict is not None:
                        # `posted` is the canonical id _post_message just
                        # minted/returned for this post (F7) — thread it
                        # through so the triage row can link back to the
                        # Slack post it summarises.
                        #
                        # thread_id=None: this is the "new top-level post"
                        # branch (not a reply to an existing thread), so
                        # there is no phase-4 interview thread to point the
                        # specialist floor at here. That makes the floor
                        # fail open by the same rule as a post-restart map.
                        await self._persist_assessment(
                            agent.agent_id, channel, verdict, slack_ts=posted,
                        )
                    elif _ASSESSMENT_UNCLOSED_RE.search(response or ""):
                        # An <assessment_json> opening tag is present.
                        # _extract_assessment_json already logged the
                        # per-block reason; this names the consequence
                        # without re-claiming "no sidecar" (Finding 1) —
                        # and without calling a block that parsed fine but
                        # was the wrong shape (e.g. a JSON array)
                        # "unparseable", which it was not (Finding A3).
                        if _sidecar_has_valid_json_block(response or ""):
                            logger.warning(
                                "[%s] Phase 5: opportunity_assessment "
                                "post's <assessment_json> sidecar parsed "
                                "as valid JSON but was not an object — "
                                "verdict lost",
                                agent.agent_id,
                            )
                        else:
                            logger.warning(
                                "[%s] Phase 5: opportunity_assessment post's "
                                "<assessment_json> sidecar was present but "
                                "unparseable — verdict lost",
                                agent.agent_id,
                            )
                    else:
                        logger.warning(
                            "[%s] Phase 5: opportunity_assessment post had no "
                            "<assessment_json> sidecar present — verdict lost",
                            agent.agent_id,
                        )

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

    async def _persist_assessment(
        self, agent_id: str, channel: str, verdict: dict, slack_ts: str | None = None,
    ) -> None:
        """Store a scouting verdict. Best-effort: a failure here must never cost
        the Slack post that already went out.

        ``slack_ts`` is the canonical post id ``_post_message`` returned for
        the assessment post itself (F7) — the row's link back to the Slack
        message it summarises. Optional and defaulted to ``None`` so every
        existing direct caller (tests driving this method on a stub) keeps
        working unchanged.

        ``thread_id`` is the phase-4 interview thread this verdict came out of,
        used to enforce the specialist floor below. ``None`` when phase 5 has
        no thread to point to (a top-level assessment post) — the floor treats
        that exactly like a post-restart empty map and fails open.

        The weighted score and band are computed here from the verdict's own
        dimension scores, never taken from the model's ``weighted_score`` field
        — the model is instructed to leave that at 0 but will sometimes fill in
        a flattering number anyway. ``recommendation`` (the model's judgement,
        which can legitimately be "route-to-incubation" — a value band() can
        never produce) and the computed ``band`` are kept in separate columns
        and neither ever overwrites the other. The verdict exactly as emitted
        is kept verbatim in ``raw_verdict`` regardless of what could be parsed
        out of it, so nothing is ever lost to a parsing decision made here.
        """
        gap = self._specialist_floor_gap(verdict)
        if gap:
            subject_hint = verdict.get("subject_agent_id")
            logger.warning(
                "[%s] Assessment REFUSED for %s — recommendation %r requires the "
                "%s specialist(s), which were never consulted during the "
                "interview. Nothing persisted. Consult them in phase 4; the "
                "assessment turn has no tools.",
                agent_id, subject_hint or "?",
                verdict.get("recommendation"), ", ".join(sorted(gap)),
            )
            return

        # The engine can run without a database (see __init__) — in that mode
        # this is a silent no-op, matching every other run-scoped write in
        # this class (e.g. _check_thread_outcome above, :1520 in the class).
        if not self.session_factory or not self.simulation_run_id:
            logger.debug(
                "[%s] Skipping assessment persistence — no database configured",
                agent_id,
            )
            return

        scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
        # An empty/missing `scores` map is "we don't know", not "we scored it
        # a 0.00 pass" — rubric_weighted_score({}) returns 0.0 and that bands
        # as "pass", which is a real, decisive decline the model never made.
        # weighted_score/band are both nullable for exactly this case; leave
        # them unset rather than record a verdict nobody rendered (F6).
        if scores:
            computed_score = rubric_weighted_score(scores)
            computed_band = rubric_band(computed_score)
        else:
            computed_score = None
            computed_band = None
        gating = _normalize_gating(verdict.get("gating"))
        red_flags = verdict.get("red_flags")
        milestones = verdict.get("suggested_derisking_milestones")
        # subject_agent_id/funnel_stage/recommendation/confidence are bounded
        # VARCHAR columns (see src/models/opportunity.py); every other field
        # degrades per-field on a bad value (wrong type -> None), but a
        # too-long *string* in one of these four is still the right type and
        # would sail past an isinstance check straight into a DataError at
        # commit — which the outer except then drops the WHOLE row for. Clip
        # instead of dropping: a truncated recommendation is still useful for
        # triage, an absent one is not (Task 11 fix round 1, Finding 5).
        subject_agent_id = _bounded_str(verdict.get("subject_agent_id"), 50)
        funnel_stage = _bounded_str(verdict.get("funnel_stage"), 20)
        recommendation = _bounded_str(verdict.get("recommendation"), 30)
        confidence = _bounded_str(verdict.get("confidence"), 20)
        try:
            async with self.session_factory() as db:
                db.add(OpportunityAssessment(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    subject_agent_id=subject_agent_id,
                    channel_name=channel,
                    slack_ts=slack_ts,
                    company_or_project=_str_or_none(verdict.get("company_or_project")),
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
                    raw_verdict=verdict,
                ))
                await db.commit()
            logger.info(
                "[%s] Assessment stored: %s -> %s (%s, %s)",
                agent_id, subject_agent_id or "?",
                recommendation or "?", computed_score, computed_band,
            )
        except Exception as exc:  # noqa: BLE001 — never lose a posted assessment
            logger.error("[%s] Failed to persist assessment: %s", agent_id, exc)

    def _strip_disallowed_tags(self, message_text: str | None, agent: Agent) -> str | None:
        """Remove @BotName mentions of non-cohort agents from an outbound message.

        Defense-in-depth for the cohort gate: the receiving agent already filters
        tags from non-cohort senders (Phase 3), but emitting a tag toward an agent
        that will never respond leaves a dangling ask in the channel. No-op when
        the gate is off for this agent (``allowed_sender_ids is None``).

        Applied from ``_post_message``, so it covers **every** outbound path —
        Phase 4 replies, Phase 5 posts, private-channel messages — rather than just
        the one call site Phase 5 used to have.

        Three deliberate behaviours (.notes/cohort-system-v2.md §9):

        - The whole mention is removed and the surrounding whitespace normalised.
          Keeping the bare name ("Great point WisemanBot") reads like an addressed
          message that isn't one.
        - An unknown bot name is left alone and logged at WARNING. A name missing
          from ``_bot_name_to_id`` means the roster is lagging, which is an
          operational problem, not a policy decision — fail open, loudly (§5.1).
        - Self-mentions are never stripped.

        Strips are counted per agent and surfaced in the admin UI: a high rate means
        the cohort topology disagrees with what the agents are trying to do.
        """
        allowed = agent.allowed_sender_ids
        if allowed is None or not message_text:
            return message_text

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
            return message_text

        self._cohort_tags_stripped[agent.agent_id] = (
            self._cohort_tags_stripped.get(agent.agent_id, 0) + stripped
        )
        # Targeted tidy-up only. Deliberately NOT a global whitespace normalisation:
        # stripping leading indentation would mangle the code blocks and bullet lists
        # agents put in messages. Collapse interior runs only after a non-space, and
        # trim end-of-line space; never touch line-leading whitespace.
        cleaned = re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"(?m)[ \t]+$", "", cleaned)
        return cleaned.lstrip(" \t") if cleaned[:1] in (" ", "\t") else cleaned

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
            cached = load_role(role).post_types
            self._role_post_types_cache[role] = cached
        return cached

    def _record_consult(self, pi_agent_id: str, domain: str) -> None:
        """Note a successful consult, keyed on the PI the interview is about.

        Keyed on the PI rather than the interview thread because the reader
        cannot see a thread: an assessment is a NEW TOP-LEVEL post, so
        ``_persist_assessment`` is called with no thread to join on. The thread
        knows the PI as ``other_agent_id`` and the verdict names the same PI as
        ``subject_agent_id`` — that is the only identifier both ends share.
        """
        if not pi_agent_id:
            return
        self._specialist_consults.setdefault(pi_agent_id, set()).add(domain)

    def _consulted_domains(self, pi_agent_id: str) -> frozenset[str]:
        """Domains consulted about this PI; empty for a PI we have no record of."""
        return frozenset(self._specialist_consults.get(pi_agent_id, ()))

    _PANEL_REQUIRED_FOR = frozenset({"advance", "conditional"})

    def _specialist_floor_gap(self, verdict: dict) -> set[str]:
        """Domains this verdict was obliged to consult but did not.

        Empty means the verdict may be persisted. Only ``advance`` and
        ``conditional`` are held to the panel: a ``pass`` costs Blackbird
        nothing, so requiring eight opinions to say no would burn calls on
        exactly the ideas that do not warrant them.

        The record is keyed on the PI (``subject_agent_id``), not on a thread:
        an assessment is a new top-level post, so there is no interview thread
        to join on at this point. An earlier version keyed on ``thread_id``,
        which was always None here — the floor read an empty set every time and
        failed open on every verdict, enforcing nothing while looking enforced.

        FAILS OPEN in two cases, both of which mean "we have no record", never
        "the panel approved":
        - the verdict names no ``subject_agent_id``, so there is nothing to
          join on;
        - the consult map is EMPTY OVERALL, which means this process has never
          recorded a consult for anyone — the post-restart case, where the
          in-memory map was cleared and the interview genuinely happened.

        Note the second condition is about the whole map, not this PI's slot.
        An earlier version failed open whenever the SUBJECT had no consults,
        which quietly excused the commonest failure of all: a hub that simply
        never convenes a panel. If the map holds entries for other PIs, this
        process demonstrably records consults, so an absent PI means the panel
        really was skipped for them — and the floor bites.

        It does not fail open once any consult exists for that PI either: a hub
        that consulted one cheap specialist must not thereby buy an exemption
        from the rest.
        """
        recommendation = verdict.get("recommendation")
        if recommendation not in self._PANEL_REQUIRED_FOR:
            return set()

        subject = verdict.get("subject_agent_id")
        if not isinstance(subject, str) or not subject:
            logger.info(
                "[specialists] verdict names no subject_agent_id — floor fails "
                "open, nothing to join a consult record to",
            )
            return set()

        if not self._specialist_consults:
            logger.info(
                "[specialists] no consult record for ANY PI — floor fails open "
                "(process restarted mid-interview?)",
            )
            return set()

        consulted = self._consulted_domains(subject)
        return set(required_domains_for(verdict) - consulted)

    def _available_post_types(
        self, agent: "Agent", *, restricted: bool
    ) -> tuple[PostTypeSpec, ...]:
        """Layer 1 ∩ layer 2: what this agent may post as a NEW top-level post.

        The SAME tuple is rendered into the prompt and used to judge the
        response, so the menu and the gate cannot disagree.

        ``restricted`` is the caller's ``blocked_for_regular``. It used to be
        distinguished from a narrower ``funding_only`` local (``blocked_for_regular
        and not has_available_non_funding``) that drove a since-removed prompt-
        template surgery in ``build_phase5_prompt`` — that local is gone (Task 5),
        and this set has always used ``blocked_for_regular`` alone.
        """
        return available_for(
            self._post_types_for_role(agent.role),
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
            terminal_only=restricted,
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

        # Resolve retired names on the way in (see LEGACY_POST_TYPE_ALIASES).
        # The rejection message below still quotes what the model actually said.
        spec = by_name.get(resolve_post_type_name(post_type))
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
            # redundant is not wrong. The hub posts its :mag: assessment into
            # the PI's own channel and naming that PI is the natural thing to
            # do; rejecting it would destroy the artifact and the whole
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

    def _client_for_channel(self, channel_id: str, fallback):
        """Return a Slack client that can access ``channel_id``.

        For collab_private channels, picks a connected member bot (tracked in
        ``_private_channel_members``). For any other channel, returns the
        fallback (typically the round-robin poll client).

        Returns None if the channel is private and no connected member is
        available — the caller should skip the channel in that case.
        """
        members = self._private_channel_members.get(channel_id)
        if not members:
            return fallback
        for aid in members:
            client = self.slack_clients.get(aid)
            if client and client.is_connected:
                return client
        return None  # private, but no connected member

    def _next_poll_client(self):
        """Round-robin a connected Slack client for shared-token polling."""
        connected = [
            c for c in self.slack_clients.values() if c and c.is_connected
        ]
        if not connected:
            return None
        client = connected[self._poll_client_cursor % len(connected)]
        self._poll_client_cursor += 1
        return client

    async def _poll_slack_for_pi_messages(self) -> None:
        """
        Poll all channels for new human (non-bot) messages.
        Add them to the message log.
        """
        if not self.slack_clients:
            return

        now = time.time()
        if now - self._last_channel_poll < CHANNEL_POLL_INTERVAL:
            return
        self._last_channel_poll = now

        default_client = self._next_poll_client()
        if not default_client:
            return

        # Poll seeded channels plus any collab_private channels tracked in
        # _channel_visibility. Skipping non-seeded public channels avoids
        # polling archived/stale channels from prior sims.
        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        for ch_name, ch_id in polled_ids.items():
            ch_visibility = self._channel_visibility.get(ch_name, VISIBILITY_PUBLIC)
            # Private channels need a member bot; non-members get channel_not_found.
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.debug(
                    "Skipping poll for private channel #%s — no connected member bot",
                    ch_name,
                )
                continue
            oldest = self._poll_cursors.get(ch_id, "0")
            try:
                messages = client.poll_channel_messages(ch_id, oldest=oldest)
                # `msg["thread_ts"]` arrives normalised: Slack sets thread_ts == ts on
                # a parent once it has replies, and the transport nulls that at ingest
                # (slack_client.normalize_inbound_message). Copying it verbatim, as
                # this loop used to, ingested a root as a reply to itself — and
                # get_new_top_level_posts skips anything with a non-null thread_ts, so
                # the post vanished from Phase 2 and _rebuild_state_from_db made it
                # permanent. The rule now lives in exactly one place.
                for msg in messages:
                    ts = msg.get("ts", "")
                    user_id = msg.get("user", "")
                    is_bot = bool(msg.get("bot_id") or msg.get("subtype") == "bot_message")

                    if not is_bot and user_id:
                        is_bot = client.is_bot_user(user_id)

                    # Add bot messages to the log (so agents can scan them)
                    # but skip PI-specific handling for them
                    if is_bot:
                        bot_name = msg.get("username", "bot")
                        # Resolve agent_id from bot name
                        bot_agent_id = self.message_log._bot_name_to_id.get(
                            bot_name.lower()
                        )
                        entry = LogEntry(
                            ts=ts,
                            channel=ch_name,
                            sender_agent_id=bot_agent_id,
                            sender_name=bot_name,
                            content=msg.get("text", ""),
                            thread_ts=msg.get("thread_ts"),
                            posted_at=float(ts) if ts else 0.0,
                            is_bot=True,
                            visibility=ch_visibility,
                            # This message came *from* Slack, so record the mirror
                            # mapping exactly as the human branch below does. Without
                            # it the entry looks DB-origin, and _slack_parent_ts then
                            # reports "no Slack root" for any thread rooted here —
                            # silently keeping every reply off Slack. The roots this
                            # branch ingests are another workspace bot's posts.
                            # Slack-origin ⇒ canonical id *is* the Slack ts, so the
                            # thread parent needs no translation.
                            slack_ts=ts or None,
                            slack_channel_id=ch_id,
                            slack_thread_ts=msg.get("thread_ts"),
                        )
                        if not self.message_log.get_entry(ts):
                            self.message_log.append(entry)
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue

                    # Human message — resolve PI identity
                    sender_name = client.resolve_user_name(user_id)
                    pi_agent_ids = self._pi_slack_id_to_agent_ids.get(user_id, [])
                    entry = LogEntry(
                        ts=ts,
                        channel=ch_name,
                        sender_agent_id=None,
                        sender_name=sender_name,
                        content=msg.get("text", ""),
                        thread_ts=msg.get("thread_ts"),
                        posted_at=float(ts) if ts else 0.0,
                        is_bot=False,
                        visibility=ch_visibility,
                        slack_ts=ts or None,
                        slack_channel_id=ch_id,
                        # Slack-origin: the canonical id is the Slack ts, so the
                        # thread parent is already a Slack ts.
                        slack_thread_ts=msg.get("thread_ts"),
                    )
                    self.message_log.append(entry)
                    logger.info(
                        "PI message in #%s from %s: %.60s",
                        ch_name, sender_name, msg.get("text", "")[:60],
                    )

                    # Check if PI message references a proposal (clears pending block)
                    self._check_pi_proposal_review(entry)

                    # PI-specific handling — apply to all agents this PI controls
                    for pi_agent_id in pi_agent_ids:
                      agent_obj = self.agents.get(pi_agent_id)
                      if agent_obj:
                          agent_obj.state.has_pi_directive = True
                      if not self._pi_handler:
                          continue
                      thread_ts = msg.get("thread_ts")

                      # PI posted in a closed thread → reopen it
                      if thread_ts and thread_ts in self._closed_thread_ids:
                          await self._reopen_thread(pi_agent_id, thread_ts, entry)

                      # PI posted in an active thread → set pi_context
                      elif thread_ts:
                          agent = self.agents.get(pi_agent_id)
                          if agent and thread_ts in agent.state.active_threads:
                              thread = agent.state.active_threads[thread_ts]
                              thread.pi_context = entry.content
                              thread.has_pending_reply = True
                              logger.info("[%s] PI posted in active thread %s", pi_agent_id, thread_ts)

                      # PI tagged their bot in a top-level post or reply
                      bot_name = self.agents[pi_agent_id].bot_name if pi_agent_id in self.agents else None
                      if bot_name and f"@{bot_name.lower()}" in msg.get("text", "").lower():
                          await self._pi_handler.handle_channel_tag(pi_agent_id, entry)

                    # Update cursor
                    if ts:
                        self._poll_cursors[ch_id] = ts

            except Exception as exc:
                logger.debug("Polling error for #%s: %s", ch_name, exc)

    async def _poll_inbound_from_db(self) -> None:
        """Ingest messages written to the DB by other processes.

        The DB is the primary store, so any message this process hasn't seen —
        PI messages and bot-authored handover posts written by the web app, and
        (later) the Slack mirror's inbound side — must be pulled into the live
        MessageLog. Human/PI messages are additionally routed through PI handling
        (proposal-review clearing, thread reopen, pi_context, @bot tags). Runs
        every tick regardless of Slack. See specs/local-db-conversations.md.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(AgentMessage)
                    .where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        # Cursor over created_at (the DB server's clock), with a
                        # lookback so a row that committed after the cursor
                        # advanced past its stamp is still caught (H2). Re-scanned
                        # rows are free — the log dedup below skips anything
                        # already ingested. See PI_INBOX_LOOKBACK_S (H2 + R3).
                        AgentMessage.created_at > self._pi_inbox_cursor - PI_INBOX_LOOKBACK,
                    )
                    # Ingest in the DB's arrival order; posted_at remains the
                    # ordering key for the conversation content itself.
                    .order_by(AgentMessage.created_at.asc())
                )).scalars().all()
        except Exception as exc:
            logger.warning("Inbound DB poll failed: %s", exc)
            return

        for r in rows:
            if r.created_at and r.created_at > self._pi_inbox_cursor:
                self._pi_inbox_cursor = r.created_at
            if not r.message_ts or self.message_log.get_entry(r.message_ts):
                # Already known (the engine itself appended and flushed it, or a
                # prior poll ingested it) — skip re-processing.
                continue
            entry = LogEntry(
                ts=r.message_ts,
                channel=r.channel_name,
                sender_agent_id=r.agent_id,
                sender_name=r.sender_name or ("PI" if not r.is_bot else r.agent_id or "bot"),
                content=r.content or "",
                thread_ts=r.thread_ts,
                posted_at=r.posted_at or 0.0,
                is_bot=r.is_bot,
                visibility=r.visibility,
            )
            self.message_log.append(entry)
            if r.is_bot:
                logger.info("External bot message in #%s: %.60s", entry.channel, entry.content[:60])
            else:
                logger.info("PI (web) message in #%s: %.60s", entry.channel, entry.content[:60])
                await self._handle_pi_inbound_entry(entry)

    async def _handle_pi_inbound_entry(self, entry: LogEntry) -> None:
        """Apply PI-message side effects, derived from the thread (no Slack map).

        Clears pending-proposal blocks, reopens closed threads, sets pi_context
        on active threads, and honors @bot tags — using the thread's own
        participants rather than a Slack user→agent mapping, so it works with
        Slack off.
        """
        # Clears any pending proposal on this thread (keyed purely by thread id).
        self._check_pi_proposal_review(entry)

        thread_ts = entry.thread_ts
        if thread_ts:
            # Reopen a closed thread for its participants.
            if thread_ts in self._closed_thread_ids:
                # Old closed threads may have been windowed out of the log at
                # startup (B2) — pull the history back so participants resolve.
                await self._hydrate_thread_from_db(thread_ts)
                history = self.message_log.get_thread_history(thread_ts)
                participants = [
                    h.sender_agent_id for h in history
                    if h.sender_agent_id and h.sender_agent_id in self.agents
                ]
                if participants:
                    await self._reopen_thread(participants[0], thread_ts, entry)
            else:
                # Active thread → treat the PI message as authoritative context.
                for agent in self.agents.values():
                    thread = agent.state.active_threads.get(thread_ts)
                    if thread:
                        thread.pi_context = entry.content
                        thread.has_pending_reply = True
                        agent.state.has_pi_directive = True

        # @bot tag → route to the tagged agent (same as the Slack path).
        tagged_id = self.message_log._extract_tagged_agent(entry.content)
        if tagged_id and tagged_id in self.agents and self._pi_handler:
            self.agents[tagged_id].state.has_pi_directive = True
            await self._pi_handler.handle_channel_tag(tagged_id, entry)

    def _check_pi_proposal_review(self, entry: LogEntry) -> None:
        """Check if a PI message clears a pending proposal for any agent."""
        thread_ts = entry.thread_ts
        if not thread_ts:
            return

        for agent in self.agents.values():
            for proposal in agent.state.pending_proposals:
                if proposal.thread_id == thread_ts and not proposal.reviewed:
                    proposal.reviewed = True
                    logger.info(
                        "[%s] Proposal in thread %s reviewed by PI",
                        agent.agent_id, thread_ts,
                    )

    async def _reopen_thread(self, agent_id: str, thread_ts: str, pi_entry: LogEntry) -> None:
        """Reopen a closed thread when a PI posts in it."""
        self._closed_thread_ids.discard(thread_ts)
        agent = self.agents.get(agent_id)
        if not agent:
            return

        # An old closed thread may have been windowed out of the log at startup
        # (B2); pull its history so the other-agent lookup and reply budget below
        # see the real conversation.
        await self._hydrate_thread_from_db(thread_ts)
        # Find the other agent from thread history
        history = self.message_log.get_thread_history(thread_ts)
        other_id = None
        for entry in history:
            if entry.sender_agent_id and entry.sender_agent_id != agent_id:
                other_id = entry.sender_agent_id
                break

        if not other_id:
            logger.warning("[%s] Cannot reopen thread %s — no other agent found", agent_id, thread_ts)
            return

        # Create fresh ThreadState for both agents
        # Set message_count_offset so the bots get a fresh budget of replies
        existing_count = len(self.message_log.get_thread_history(thread_ts))
        agent.state.active_threads[thread_ts] = ThreadState(
            thread_id=thread_ts,
            channel=pi_entry.channel,
            other_agent_id=other_id,
            message_count=0,
            has_pending_reply=True,
            pi_context=pi_entry.content,
            message_count_offset=existing_count,
        )

        other_agent = self.agents.get(other_id)
        if other_agent:
            other_agent.state.active_threads[thread_ts] = ThreadState(
                thread_id=thread_ts,
                channel=pi_entry.channel,
                other_agent_id=agent_id,
                message_count=0,
                has_pending_reply=True,
                message_count_offset=existing_count,
            )

        logger.info("[%s] PI reopened closed thread %s with %s", agent_id, thread_ts, other_id)

    async def _poll_pi_dms(self) -> None:
        """Poll Slack for PI DMs and record them as inbound rows.

        Processing is unified through the DB: this method only persists inbound
        Slack DMs to pi_dm_messages; _poll_pi_dms_from_db is the single place
        that runs them through PIHandler (so Slack and web DMs are handled
        identically and never double-processed). See specs/local-db-conversations.md.
        """
        if not self._pi_slack_id_to_agent_ids or not self.session_factory or not self.simulation_run_id:
            return

        # Default cursor to simulation start time — only process DMs sent after we started
        default_cursor = str(self._start_time.timestamp()) if self._start_time else "0"

        from src.services.pi_inbox import record_pi_dm

        for pi_slack_id, agent_ids in self._pi_slack_id_to_agent_ids.items():
            for agent_id in agent_ids:
                client = self.slack_clients.get(agent_id)
                if not client or not client.is_connected:
                    continue

                oldest = self._dm_poll_cursors.get(agent_id, default_cursor)
                messages = client.poll_dm_messages(pi_slack_id, oldest=oldest)

                for msg in messages:
                    ts = msg.get("ts", "")
                    text = msg.get("text", "").strip()
                    if not text:
                        continue
                    logger.info("[%s] PI DM from %s: %s", agent_id, pi_slack_id, text[:80])
                    try:
                        async with self.session_factory() as db:
                            await record_pi_dm(
                                db, run_id=self.simulation_run_id, agent_id=agent_id,
                                pi_user_id=pi_slack_id, direction="inbound", content=text,
                                sender_name="PI", slack_ts=ts or None,
                            )
                            await db.commit()
                    except Exception as exc:
                        logger.error("[%s] Failed to record PI DM: %s", agent_id, exc)
                    if ts > oldest:
                        self._dm_poll_cursors[agent_id] = ts

    async def _seed_pi_dm_cursor(self) -> None:
        """Start the DM poller past existing inbound DMs (don't replay history).

        Seeds both the cursor (max created_at — the DB server's clock, see R3)
        and the seen-set (ts of inbound DMs within the lookback window), so the
        first poll's lookback re-scan doesn't re-process history through
        handle_dm on restart.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        from src.models import PiDmMessage
        try:
            async with self.session_factory() as db:
                mx = (await db.execute(
                    sa_select(sa_func.max(PiDmMessage.created_at)).where(
                        PiDmMessage.simulation_run_id == self.simulation_run_id,
                        PiDmMessage.direction == "inbound",
                    )
                )).scalar_one_or_none()
                if mx:
                    self._pi_dm_cursor = max(self._pi_dm_cursor, mx)
                    seen = (await db.execute(
                        sa_select(PiDmMessage.ts, PiDmMessage.created_at).where(
                            PiDmMessage.simulation_run_id == self.simulation_run_id,
                            PiDmMessage.direction == "inbound",
                            PiDmMessage.created_at > self._pi_dm_cursor - PI_INBOX_LOOKBACK,
                        )
                    )).all()
                    for ts, created_at in seen:
                        if ts:
                            self._pi_dm_seen[ts] = created_at or EPOCH_UTC
        except Exception as exc:
            logger.warning("PI DM cursor seed failed: %s", exc)

    async def _poll_pi_dms_from_db(self) -> None:
        """Process inbound PI DMs recorded in the DB (Slack or web-originated).

        The single processor for PI DMs: reads new inbound pi_dm_messages rows
        and runs each through PIHandler.handle_dm (classify → standing
        instruction / feedback / question), then flips has_pi_directive so
        Phase 5 runs. Works with Slack off. See specs/local-db-conversations.md.
        """
        if not self._pi_handler or not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select

        from src.models import PiDmMessage
        floor = self._pi_dm_cursor - PI_INBOX_LOOKBACK
        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(PiDmMessage)
                    .where(
                        PiDmMessage.simulation_run_id == self.simulation_run_id,
                        PiDmMessage.direction == "inbound",
                        # Lookback + seen-set dedup below, mirroring the channel
                        # poller, so a late-committing DM row isn't skipped (H2).
                        # created_at, not posted_at, so the window doesn't depend
                        # on the writing process's clock (R3).
                        PiDmMessage.created_at > floor,
                    )
                    .order_by(PiDmMessage.created_at.asc())
                )).scalars().all()
        except Exception as exc:
            logger.warning("PI DM inbox poll failed: %s", exc)
            return

        for r in rows:
            if r.created_at and r.created_at > self._pi_dm_cursor:
                self._pi_dm_cursor = r.created_at
            if r.ts and r.ts in self._pi_dm_seen:
                continue  # already processed (lookback re-scan)
            if r.agent_id not in self.agents:
                continue
            if r.ts:
                self._pi_dm_seen[r.ts] = r.created_at or EPOCH_UTC
            try:
                await self._pi_handler.handle_dm(r.agent_id, r.pi_user_id, r.content)
                self.agents[r.agent_id].state.has_pi_directive = True
            except Exception as exc:
                logger.error("[%s] Failed to handle PI DM (DB): %s", r.agent_id, exc)

        # Prune the seen-set to the lookback window — anything at or below the new
        # floor won't be re-queried, so it no longer needs tracking.
        prune_floor = self._pi_dm_cursor - PI_INBOX_LOOKBACK
        if self._pi_dm_seen:
            self._pi_dm_seen = {
                ts: ca for ts, ca in self._pi_dm_seen.items() if ca > prune_floor
            }

    async def _poll_proposal_threads_for_pi(self) -> None:
        """Poll unreviewed proposal threads for PI replies.

        Thread replies don't appear in channel history, so this checks
        conversations.replies on each unreviewed proposal thread to detect
        PI messages that would trigger a thread reopen.
        """
        if not self._pi_slack_id_to_agent_ids:
            return

        now = time.time()
        if now - self._last_proposal_poll < PROPOSAL_POLL_INTERVAL:
            return
        self._last_proposal_poll = now

        # Collect PI user IDs for quick lookup
        pi_user_ids = set(self._pi_slack_id_to_agent_ids.keys())
        if not pi_user_ids:
            return

        # Find unreviewed proposals from in-memory state
        threads_to_poll: list[tuple[str, str, str]] = []  # (thread_id, channel_name, agent_id)
        seen = set()
        for agent in self.agents.values():
            for proposal in agent.state.pending_proposals:
                if not proposal.reviewed and proposal.thread_id not in seen:
                    seen.add(proposal.thread_id)
                    threads_to_poll.append(
                        (proposal.thread_id, proposal.channel, agent.agent_id)
                    )

        if not threads_to_poll:
            return

        default_client = self._next_poll_client()
        if not default_client:
            return

        default_cursor = str(self._start_time.timestamp()) if self._start_time else "0"

        for thread_id, channel_name, agent_id in threads_to_poll:
            ch_id = self._channel_id_map.get(channel_name)
            if not ch_id:
                continue

            # Route per-channel: collab_private channels need a bot that was
            # invited. A round-robin client will hit channel_not_found on any
            # private channel it isn't a member of.
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.debug(
                    "Skipping proposal-thread poll for private channel #%s — no connected member bot",
                    channel_name,
                )
                continue

            cursor_key = f"proposal_thread:{thread_id}"
            oldest = self._poll_cursors.get(cursor_key, default_cursor)

            try:
                replies = client.get_thread_replies(ch_id, thread_id, oldest=oldest)
            except ThreadNotFound:
                self._evict_dead_thread(thread_id)
                continue
            except Exception as exc:
                logger.debug("Failed to poll proposal thread %s: %s", thread_id, exc)
                continue

            for msg in replies:
                ts = msg.get("ts", "")
                user_id = msg.get("user", "")

                # Skip bot messages and the root message
                if msg.get("bot_id") or ts == thread_id:
                    continue

                # Only process PI messages
                if user_id not in pi_user_ids:
                    continue

                sender_name = client.resolve_user_name(user_id)
                entry = LogEntry(
                    ts=ts,
                    channel=channel_name,
                    sender_agent_id=None,
                    sender_name=sender_name,
                    content=msg.get("text", ""),
                    thread_ts=thread_id,
                    posted_at=float(ts) if ts else 0.0,
                    is_bot=False,
                    slack_ts=ts or None,
                    slack_channel_id=ch_id,
                    # Slack-origin (polled from a Slack proposal thread), so the
                    # canonical thread id is already the Slack parent ts.
                    slack_thread_ts=thread_id,
                )

                # Avoid re-processing messages already in the log
                if self.message_log.get_entry(ts):
                    continue

                self.message_log.append(entry)
                logger.info(
                    "PI message in proposal thread %s (#%s) from %s: %.60s",
                    thread_id, channel_name, sender_name, msg.get("text", "")[:60],
                )

                # Mark proposal as reviewed
                self._check_pi_proposal_review(entry)

                # Reopen the thread for all PI's agents
                pi_agent_ids = self._pi_slack_id_to_agent_ids.get(user_id, [])
                for pi_agent_id in pi_agent_ids:
                    if thread_id in self._closed_thread_ids:
                        await self._reopen_thread(pi_agent_id, thread_id, entry)

                # Update cursor
                if ts > oldest:
                    self._poll_cursors[cursor_key] = ts

    # ------------------------------------------------------------------
    # Message posting
    # ------------------------------------------------------------------

    def mint_ts(self) -> str:
        """Return a monotonic, unique, ts-shaped id (decimal seconds string).

        The canonical message/channel id when there is no Slack ts (Slack-off,
        or a DB-origin message). Monotonicity preserves the posted_at=float(ts)
        ordering the engine relies on; the minter's high-water mark is seeded from
        the rebuild's max(posted_at) so new ids always sort after restored
        history. Uniqueness is what makes the idempotent MessageLog.append safe,
        and it holds across processes too: this minter owns the engine's writer
        slot, disjoint from the web app's and GrantBot's (R1).
        See src/agent/ids.py and specs/local-db-conversations.md.
        """
        return self._ts_minter.mint()

    async def _post_message(
        self,
        agent_id: str,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> str | None:
        """Post a message to Slack and record it in the message log + DB.

        Returns the canonical post id (the root chunk's ``ts`` — a real Slack
        ts when a connected client posted, else a locally-minted one; see
        "Canonical id" below), or ``None`` when nothing was actually recorded:
        the text stripped to nothing, or the reply's parent thread was found
        to be deleted — in either case nothing was posted and no log entry was
        written. Callers that count a turn or persist something derived from
        the post (e.g. the opportunity_assessment verdict sidecar, which
        stores this id as ``slack_ts`` for a link back to the post it
        summarises — F7) must check this before doing either — see Task 11
        fix round 1, Finding 3. The return value is truthy exactly when a post
        was recorded, so existing callers that only did ``if not posted:`` (or
        ignore the return value entirely) are unaffected by the ``bool`` ->
        ``str | None`` change.
        """
        # Final safety: strip any leaked <slack_message> tags, and any
        # <assessment_json> sidecar — that block is for Blackbird staff and the DB,
        # never for the channel. See _strip_assessment_sidecar for why an
        # unclosed tag is handled differently from a well-formed pair.
        text = _strip_assessment_sidecar(text)
        text = re.sub(r"</?slack_message>", "", text).strip()

        # A sidecar-only or truncated response can strip to nothing — e.g. an
        # unclosed <assessment_json> nested as the entire <slack_message> body,
        # with no real text before it (_ASSESSMENT_UNCLOSED_RE then deletes
        # from the very start of the string). Slack rejects empty text anyway,
        # but bailing here also matters for what happens *after* posting:
        # without this guard, _post_message still mints a ts and writes a
        # LogEntry with content="" and slack_ts=None — a DB row with no
        # corresponding Slack message, breaking the row-count-matches-Slack-
        # message-count invariant documented below, and the caller still
        # counts the turn as published (message_count incremented,
        # interesting_posts drained) even though nothing went out. Return
        # before any of that — no Slack call, no minted ts, no log entry.
        if not text:
            logger.warning(
                "[%s] Suppressed a post to #%s: text was empty after "
                "stripping the assessment sidecar/slack_message tags — likely "
                "a sidecar-only or truncated response with no real message body.",
                agent_id, channel,
            )
            return None

        client = self.slack_clients.get(agent_id)
        agent = self.agents.get(agent_id)

        # Cohort gate, outbound side. Placed here rather than in a phase so it
        # covers every caller — Phase 4 replies, Phase 5 posts, private-channel
        # messages — and cannot be bypassed by a new call site. Idempotent, so the
        # extra Phase 5 pass (which needs the cleaned text locally) is harmless.
        # No-op when the gate is off for this agent. See v2 §9.
        if agent is not None:
            text = self._strip_disallowed_tags(text, agent) or text

        # Slack threads on the *root's Slack ts*, which equals the canonical
        # thread_ts only when the root was born on Slack. A thread started
        # Slack-off has a minted root id — passing that to Slack detaches the
        # reply or errors — so such a reply is kept DB-only rather than mirrored.
        slack_parent = self._slack_parent_ts(thread_ts)
        can_mirror = thread_ts is None or slack_parent is not None

        result: dict | None = None
        if client and client.is_connected and not can_mirror:
            logger.warning(
                "[%s] Not mirroring reply to #%s: thread %s has no Slack root "
                "(started with Slack off). The message is still recorded in the DB.",
                agent_id, channel, thread_ts,
            )
        elif client and client.is_connected:
            try:
                result = client.post_message(channel, text, thread_ts=slack_parent)
            except ThreadNotFound:
                # Parent was deleted. post_message already cleaned up the
                # orphan top-level post on Slack. Purge the dead thread_ts
                # from state so no one replies to it again. Keyed by the
                # canonical id, which is what the engine's state uses.
                if thread_ts:
                    self._evict_dead_thread(thread_ts)
                logger.warning(
                    "[%s] Skipped reply to deleted thread %s in #%s",
                    agent_id, thread_ts, channel,
                )
                return None
        else:
            logger.info("[%s] MOCK post to #%s: %s...", agent_id, channel, text[:60])

        # One log entry per message that really exists on the transport. Normally
        # that is one; it is several when the text was over Slack's 4000-character
        # per-message limit and the client split it (see
        # AgentSlackClient.post_message). Recording a single row for a post Slack
        # turned into five messages left four of them in Slack with no row at all,
        # and named the row's slack_ts after the *tail* — so _slack_parent_ts
        # threaded replies onto a fragment, posted_at took the tail's clock, and the
        # next restart's _rebuild_state_from_slack re-ingested the unrecorded head
        # chunks as brand-new inbound messages. The mirror is only in bijection with
        # Slack if the row count matches the message count.
        mirrored = self._mirrored_messages(result, text, slack_parent)

        # Canonical id: the Slack ts when a connected client posted, else a
        # locally-minted ts. Slack ts (when present) is also recorded as the
        # mirror mapping on the entry.
        #
        # `visibility` is stamped from the channel's class. It was previously omitted,
        # so every agent-authored message defaulted to "public" even in a
        # collab_private channel — including the ones written into a PI-created
        # refinement channel. Two readers depend on this field:
        #
        #   - the cohort gate's private-channel exemption (_entry_allowed), which is
        #     how a PI pairing outranks an admin cohort grouping — with the field
        #     unset the exemption never fired, and two agents in different cohorts
        #     could not converse in the channel the PI made for them;
        #   - the G2 memory-synthesis filter, which is meant to keep private-channel
        #     content out of the public memory segment.
        #
        # Found by a real multi-turn run: the private-channel messages persisted with
        # visibility='public' while the AgentChannel row said collab_private.
        # See .notes/cohort-system-v2.md §7.
        visibility = self._resolve_channel_visibility(channel)
        sender_name = agent.bot_name if agent else f"{agent_id}Bot"
        root_ts: str | None = None
        for index, message in enumerate(mirrored or [None]):
            slack_ts = message.get("ts") if message else None
            ts = slack_ts or self.mint_ts()
            try:
                posted_at = float(ts)
            except (TypeError, ValueError):
                posted_at = time.time()
            # Chunk 0 keeps the caller's canonical thread id. A continuation chunk of
            # a *root* post hangs off chunk 0 — one logical post stays one top-level
            # post, so nobody's Phase 2 scan sees N roots where the author wrote one.
            canonical_parent = thread_ts if (thread_ts or index == 0) else root_ts
            entry = LogEntry(
                ts=ts,
                channel=channel,
                sender_agent_id=agent_id,
                sender_name=sender_name,
                content=(message.get("text") if message else None) or text,
                thread_ts=canonical_parent,
                posted_at=posted_at,
                is_bot=True,
                visibility=visibility,
                slack_ts=slack_ts,
                slack_channel_id=(message.get("channel") if message else None),
                # The parent the transport reports, so the row always describes the
                # message the transport actually made rather than the one we asked for.
                slack_thread_ts=(message.get("thread_ts") if message and slack_ts else None),
            )
            if index == 0:
                root_ts = ts
            # Persisted to agent_messages via the MessageLog append callback
            # (_enqueue_persist → _flush_persisted). The DB is the primary store.
            self.message_log.append(entry)
        return root_ts

    @staticmethod
    def _mirrored_messages(
        result: dict | None, text: str, slack_parent: str | None,
    ) -> list[dict]:
        """Normalise a transport's post result into one record per real message.

        ``AgentSlackClient`` reports ``posted_messages``; a Transport backend that
        never splits need not, so a bare ``{"ts": ..., "channel": ...}`` is read as
        the single message it describes. Returns ``[]`` when nothing was posted,
        which is the signal to mint a local canonical id instead.
        See src/agent/transport.py for the declared contract.
        """
        if not result:
            return []
        posted = result.get("posted_messages")
        if posted:
            return list(posted)
        return [{
            "ts": result.get("ts"),
            "channel": result.get("channel"),
            "text": text,
            "thread_ts": slack_parent,
        }]

    def _slack_parent_ts(self, thread_ts: str | None) -> str | None:
        """Resolve a canonical thread id to the Slack ts Slack must thread on.

        Returns None when the thread has no Slack presence (a DB-origin root
        minted while Slack was off), so callers can skip the mirror instead of
        posting against an id Slack has never seen. Falls back to the canonical
        id when the root is not in the log at all (windowed out by the B2 rebuild
        bound), which preserves the pure-Slack-on behaviour where the canonical
        id *is* the Slack ts. The rebuild populates slack_ts on restored entries,
        so this survives a restart. See specs/local-db-conversations.md.
        """
        if not thread_ts:
            return None
        root = self.message_log.get_entry(thread_ts)
        if root is None:
            return thread_ts
        return root.slack_ts

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    async def _load_pi_mappings(self) -> None:
        """Load PI and delegate Slack user ID -> agent ID mappings from AgentRegistry."""
        if not self.session_factory:
            logger.info("No DB session — skipping PI mapping load")
            return
        try:
            from sqlalchemy import select

            from src.models import AgentRegistry
            async with self.session_factory() as db:
                result = await db.execute(
                    select(
                        AgentRegistry.agent_id,
                        AgentRegistry.slack_user_id,
                        AgentRegistry.delegate_slack_ids,
                    )
                    .where(AgentRegistry.slack_user_id.isnot(None))
                    .where(AgentRegistry.status == "active")
                )
                for row in result:
                    # Primary PI
                    self._pi_slack_id_to_agent_ids.setdefault(row.slack_user_id, []).append(row.agent_id)
                    # Delegates
                    for delegate_id in (row.delegate_slack_ids or []):
                        self._pi_slack_id_to_agent_ids.setdefault(delegate_id, []).append(row.agent_id)
            if self._pi_slack_id_to_agent_ids:
                logger.info("Loaded PI mappings: %s", {
                    k[:8] + "...": v for k, v in self._pi_slack_id_to_agent_ids.items()
                })
            else:
                logger.info("No PI Slack accounts linked yet")
        except Exception as exc:
            logger.warning("Failed to load PI mappings: %s", exc)

    def _ensure_seeded_channels(self) -> None:
        """Create any missing seeded channels and join relevant bots."""
        client = next(iter(self.slack_clients.values()), None)
        if not client or not client.is_connected:
            # Slack off — channels are DB-native with stable local: ids that
            # can't collide with Slack C…/G… ids. See specs/local-db-conversations.md.
            self._channel_id_map = {ch: f"local:{ch}" for ch in SEEDED_CHANNELS}
            # All seeded channels are public.
            self._channel_visibility = {ch: VISIBILITY_PUBLIC for ch in SEEDED_CHANNELS}
            return

        # A *complete* listing, or none. list_channels raises rather than hand back a
        # subset that looks whole, because the subset is what made this method
        # re-create channels the workspace already had: conversations.create answers
        # name_taken, create_channel used to return None, and the channel ended up
        # with no id in _channel_id_map at all — after which every post to it was
        # addressed by name and Slack answered not_in_channel. Demonstrated on a real
        # workspace: #all-copi-test exists as C0BM57CG4HJ and the engine mapped it to
        # None. With an incomplete listing we adopt what we saw and create nothing,
        # since "absent from this listing" no longer means "absent from Slack".
        listing_complete = True
        try:
            existing = client.list_channels()
        except SlackListingIncomplete as exc:
            listing_complete = False
            existing = {ch["name"]: ch["id"] for ch in exc.partial}
            logger.error(
                "Channel discovery is incomplete (%s) — adopting the %d channel(s) "
                "seen and creating none, so a channel Slack already has is not "
                "duplicated", exc.reason, len(existing),
            )

        # Create missing seeded channels
        if listing_complete:
            for ch_name in SEEDED_CHANNELS:
                if ch_name not in existing:
                    logger.info("Creating seeded channel #%s", ch_name)
                    ch_data = client.create_channel(ch_name)
                    if ch_data:
                        existing[ch_name] = ch_data.get("id", "")

        self._channel_id_map = dict(existing)
        # Seeded channels are always 'public'. Agent-created channels (including
        # future collab_private channels) populate their own entries when the
        # agent_channels rows are loaded during engine-state rebuild.
        for ch_name in existing:
            self._channel_visibility.setdefault(ch_name, VISIBILITY_PUBLIC)

        # Join the first (polling) client to ALL seeded channels so it can poll them
        for ch_name, ch_id in existing.items():
            if ch_name in SEEDED_CHANNELS:
                client.join_channel(ch_id)

        # Share channel map across all clients
        for c in self.slack_clients.values():
            c.cache_channel_ids(existing)

    async def _persist_seeded_channels(self) -> None:
        """Record seeded channels in agent_channels for this run (idempotent).

        Keeps channel existence in the DB so the workspace is reconstructable
        without Slack (and so the admin UI can count channels). Uses the current
        _channel_id_map (Slack ids when on, local: ids when off).
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import select as sa_select

        from src.agent.channels import record_channel_created
        try:
            async with self.session_factory() as db:
                existing_names = set(
                    (await db.execute(
                        sa_select(AgentChannel.channel_name).where(
                            AgentChannel.simulation_run_id == self.simulation_run_id
                        )
                    )).scalars().all()
                )
                created = 0
                for ch_name in SEEDED_CHANNELS:
                    if ch_name in existing_names:
                        continue
                    await record_channel_created(
                        db,
                        simulation_run_id=self.simulation_run_id,
                        channel_id=self._channel_id_map.get(ch_name, f"local:{ch_name}"),
                        channel_name=ch_name,
                        channel_type="thematic",
                        created_by_agent="system",
                    )
                    created += 1
                if created:
                    await db.commit()
                    logger.info("Persisted %d seeded channels to agent_channels", created)
        except Exception as exc:
            logger.warning("Failed to persist seeded channels: %s", exc)

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

    # Public alias. `_build_lab_directories` is called from three places whose
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
        Slack poll cursors so a later Slack reconcile only fetches newer messages.
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
        # would just bloat RAM and startup. A PI reopening an old closed thread
        # hydrates it on demand (_hydrate_thread_from_db).
        recent_floor = time.time() - REBUILD_WINDOW_S
        closed_thread_ids_subq = sa_select(ThreadDecision.thread_id)
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
            )
            self.message_log.load_entry(entry)
            loaded += 1
            if entry.posted_at > max_posted:
                max_posted = entry.posted_at
            # Track the Slack mapping so the reconcile can dedup, and advance
            # the Slack poll cursor so it only fetches genuinely newer messages.
            if r.slack_ts:
                self._known_slack_ts.add(r.slack_ts)
                if r.slack_channel_id:
                    cur = self._poll_cursors.get(r.slack_channel_id, "0")
                    if r.slack_ts > cur:
                        self._poll_cursors[r.slack_channel_id] = r.slack_ts
        self._ts_minter.seed_floor(max_posted)
        # Start the inbox poller past everything already in the DB so it only
        # picks up genuinely new web-written PI messages. Taken from MAX over the
        # whole run rather than the loaded rows: the rebuild is windowed (B2), and
        # the cursor's job is "don't replay what is already stored", which covers
        # windowed-out rows too (a PI reopening one of those hydrates it instead).
        await self._seed_pi_inbox_cursor()
        logger.info("Rebuilt MessageLog from DB: %d messages", loaded)

    async def _seed_pi_inbox_cursor(self) -> None:
        """Advance the inbound-poll cursor past all stored messages for this run.

        Cursor axis is created_at (the DB server's clock) — see
        PI_INBOX_LOOKBACK_S / R3.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                mx = (await db.execute(
                    sa_select(sa_func.max(AgentMessage.created_at)).where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                    )
                )).scalar_one_or_none()
        except Exception as exc:
            logger.warning("PI inbox cursor seed failed: %s", exc)
            return
        if mx:
            self._pi_inbox_cursor = max(self._pi_inbox_cursor, mx)

    async def _hydrate_thread_from_db(self, thread_ts: str) -> None:
        """Load one thread's messages into the log if not already present.

        The startup rebuild windows out old *closed*-thread bodies (B2), but a PI
        can still reopen such a thread, and the reopen paths derive participants /
        reply budget from the in-memory thread history. This pulls a specific
        thread's full history on demand. Idempotent (load_entry dedups on ts) and
        index-backed (run + message_ts/thread_ts).
        """
        if not self.session_factory or not self.simulation_run_id or not thread_ts:
            return
        from sqlalchemy import or_
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(AgentMessage)
                    .where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        or_(
                            AgentMessage.message_ts == thread_ts,
                            AgentMessage.thread_ts == thread_ts,
                        ),
                    )
                    .order_by(AgentMessage.posted_at.asc())
                )).scalars().all()
        except Exception as exc:
            logger.warning("Thread hydrate failed for %s: %s", thread_ts, exc)
            return
        for r in rows:
            if not r.content or not r.message_ts:
                continue
            self.message_log.load_entry(LogEntry(
                ts=r.message_ts,
                channel=r.channel_name,
                sender_agent_id=r.agent_id,
                sender_name=r.sender_name or "",
                content=r.content,
                thread_ts=r.thread_ts,
                posted_at=r.posted_at or 0.0,
                is_bot=r.is_bot,
                visibility=r.visibility,
                slack_ts=_restored_slack_ts(r),
                slack_channel_id=r.slack_channel_id,
                slack_thread_ts=r.slack_thread_ts,
            ))

    async def _flush_persisted(self, force_stats: bool = False) -> None:
        """Batch-upsert buffered message-log entries into agent_messages.

        Uses ON CONFLICT (simulation_run_id, message_ts) so it is safe to run
        alongside legacy rows, transitional double-writes, and repeated restarts.
        Drops the buffer when there is no DB so it can't grow unbounded.
        """
        if not self._pending_persist:
            return
        if not self.session_factory or not self.simulation_run_id:
            self._pending_persist.clear()
            return
        entries = self._pending_persist
        self._pending_persist = []
        # Dedup by canonical id within the batch — a single ON CONFLICT statement
        # cannot touch the same row twice.
        by_ts: dict[str, dict] = {}
        for e in entries:
            if not e.ts:
                continue
            channel_id = self._channel_id_map.get(e.channel) or f"local:{e.channel}"
            by_ts[e.ts] = {
                "simulation_run_id": self.simulation_run_id,
                "agent_id": e.sender_agent_id,
                "channel_id": channel_id,
                "channel_name": e.channel,
                "message_ts": e.ts,
                "message_length": len(e.content or ""),
                "thread_ts": e.thread_ts,
                "phase": "thread_reply" if e.thread_ts else "new_post",
                "visibility": e.visibility,
                "content": e.content or "",
                "sender_name": e.sender_name or "",
                "is_bot": e.is_bot,
                "posted_at": e.posted_at,
                "slack_ts": e.slack_ts,
                "slack_channel_id": e.slack_channel_id,
                # The root's *Slack* ts, not the canonical thread_ts — they differ
                # whenever the thread started Slack-off. Only meaningful when this
                # entry is itself on Slack. See _slack_parent_ts.
                "slack_thread_ts": e.slack_thread_ts if e.slack_ts else None,
            }
        rows = list(by_ts.values())
        if not rows:
            return
        from sqlalchemy import func as sa_func
        from sqlalchemy import or_
        from sqlalchemy import select as sa_select
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        try:
            async with self.session_factory() as db:
                stmt = pg_insert(AgentMessage.__table__).values(rows)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_agent_messages_run_ts",
                    set_={
                        "content": stmt.excluded.content,
                        "sender_name": stmt.excluded.sender_name,
                        "is_bot": stmt.excluded.is_bot,
                        "posted_at": stmt.excluded.posted_at,
                        "message_length": stmt.excluded.message_length,
                        "visibility": stmt.excluded.visibility,
                        "thread_ts": stmt.excluded.thread_ts,
                        "channel_id": stmt.excluded.channel_id,
                        "channel_name": stmt.excluded.channel_name,
                        "agent_id": stmt.excluded.agent_id,
                        "slack_ts": stmt.excluded.slack_ts,
                        "slack_channel_id": stmt.excluded.slack_channel_id,
                        "slack_thread_ts": stmt.excluded.slack_thread_ts,
                    },
                    # M1a guard: never let a bot message clobber an existing human
                    # (PI) row on a cross-process canonical-id collision. Allow the
                    # update only when the existing row is itself a bot row, or the
                    # incoming row is human (re-flush of an ingested PI message /
                    # slack mirror). A blocked conflict is left untouched, like
                    # DO NOTHING for that row. See PR #19 review M1.
                    where=or_(
                        AgentMessage.__table__.c.is_bot.is_(True),
                        stmt.excluded.is_bot.is_(False),
                    ),
                )
                await db.execute(stmt)
                # Refresh the run's cosmetic counters at most every
                # RUN_STATS_UPDATE_INTERVAL (a full COUNT every flush is wasteful
                # at scale — B1). The bulk upsert can't cheaply tell inserts from
                # updates, so total_messages is a recomputed count; slight
                # staleness between refreshes is fine for a display counter.
                now = time.time()
                if force_stats or now - self._last_run_stats_update >= RUN_STATS_UPDATE_INTERVAL:
                    self._last_run_stats_update = now
                    run = (await db.execute(
                        sa_select(SimulationRun).where(SimulationRun.id == self.simulation_run_id)
                    )).scalar_one_or_none()
                    if run:
                        total = (await db.execute(
                            sa_select(sa_func.count(AgentMessage.id)).where(
                                AgentMessage.simulation_run_id == self.simulation_run_id
                            )
                        )).scalar_one()
                        run.total_messages = total
                        run.total_api_calls = sum(a.api_call_count for a in self.agents.values())
                await db.commit()
        except Exception as exc:
            # Re-queue the failed batch instead of dropping it. The DB is now the
            # source of truth for conversations, so a silently-dropped flush is
            # unrecoverable — a restart rebuilds from the DB and these messages
            # would be gone for good. New entries may have been enqueued while we
            # were awaiting the (failed) commit; put the failed batch back in
            # front to preserve chronological order for the next flush attempt.
            self._pending_persist[0:0] = entries
            logger.warning(
                "Failed to flush %d messages, re-queued for retry: %s",
                len(rows), exc,
            )

    def _enqueue_persist(self, entry: LogEntry) -> None:
        """MessageLog persist callback — buffer a new entry for the next flush."""
        self._pending_persist.append(entry)

    async def _rebuild_state_from_slack(self) -> None:
        """Reconcile the MessageLog with Slack history (Slack-on only).

        The DB is the primary store (_rebuild_state_from_db); this pass only
        adds messages that exist on Slack but not yet in the log — via the
        idempotent append, which also persists them to the DB.
        """
        default_client = next(iter(self.slack_clients.values()), None)
        if not default_client or not default_client.is_connected:
            logger.info("No Slack client available — skipping Slack reconcile")
            return

        # Build a mapping of bot_user_id -> agent_id
        bot_uid_to_agent: dict[str, str] = {}
        for aid, c in self.slack_clients.items():
            if c.bot_user_id:
                bot_uid_to_agent[c.bot_user_id] = aid

        # 1. Poll full Slack history for seeded channels + any known
        # collab_private channels. Same filter as the live-poll loop.
        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        total_messages = 0
        total_threads = 0
        for ch_name, ch_id in polled_ids.items():
            ch_visibility = self._channel_visibility.get(ch_name, VISIBILITY_PUBLIC)
            # Route per-channel: private channels need a member bot.
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.debug(
                    "Skipping rebuild for private channel #%s — no connected member bot",
                    ch_name,
                )
                continue
            messages = client.get_full_channel_history(ch_id)
            for msg in messages:
                ts = msg.get("ts", "")
                user_id = msg.get("user", "")
                bot_id = msg.get("bot_id")
                is_bot = bool(bot_id) or msg.get("subtype") == "bot_message"

                # Determine sender agent ID for bot messages
                sender_agent_id = None
                if is_bot and user_id:
                    sender_agent_id = bot_uid_to_agent.get(user_id)

                # Skip messages already represented in the DB (dedup a message
                # that was DB-origin then mirrored to Slack, whose canonical id
                # differs from this Slack ts).
                if ts and ts in self._known_slack_ts:
                    if ts:
                        self._poll_cursors[ch_id] = ts
                    continue
                sender_name = msg.get("username", "") or user_id
                # `thread_ts` is already normalised: Slack marks a parent that has
                # replies with thread_ts == ts, and the transport nulls that at ingest
                # for every inbound path (see slack_client.normalize_inbound_message).
                # The rule used to live here and *only* here, which is why the live
                # poller ingested roots as replies to themselves.
                entry = LogEntry(
                    ts=ts,
                    channel=ch_name,
                    sender_agent_id=sender_agent_id,
                    sender_name=sender_name,
                    content=msg.get("text", ""),
                    thread_ts=msg.get("thread_ts"),
                    posted_at=float(ts) if ts else 0.0,
                    is_bot=is_bot,
                    visibility=ch_visibility,
                    slack_ts=ts or None,
                    slack_channel_id=ch_id,
                    # Slack-origin: canonical id == Slack ts, so the thread
                    # parent needs no translation.
                    slack_thread_ts=msg.get("thread_ts"),
                )
                if self.message_log.append(entry):
                    total_messages += 1
                if ts:
                    self._known_slack_ts.add(ts)

                # Update poll cursor to latest
                if ts:
                    self._poll_cursors[ch_id] = ts

                # If this message has thread replies, fetch them
                reply_count = msg.get("reply_count", 0)
                if reply_count > 0:
                    try:
                        replies = client.get_all_thread_replies(ch_id, ts)
                    except ThreadNotFound:
                        continue
                    total_threads += 1
                    for reply in replies:
                        rts = reply.get("ts", "")
                        if rts == ts:
                            continue  # skip parent (already added)
                        if rts and rts in self._known_slack_ts:
                            continue
                        r_user_id = reply.get("user", "")
                        r_is_bot = bool(reply.get("bot_id")) or reply.get("subtype") == "bot_message"
                        r_agent_id = bot_uid_to_agent.get(r_user_id) if r_is_bot else None
                        r_entry = LogEntry(
                            ts=rts,
                            channel=ch_name,
                            sender_agent_id=r_agent_id,
                            sender_name=reply.get("username", "") or r_user_id,
                            content=reply.get("text", ""),
                            thread_ts=ts,
                            posted_at=float(rts) if rts else 0.0,
                            is_bot=r_is_bot,
                            visibility=ch_visibility,
                            slack_ts=rts or None,
                            slack_channel_id=ch_id,
                            slack_thread_ts=ts,  # Slack-origin: canonical == Slack ts
                        )
                        if self.message_log.append(r_entry):
                            total_messages += 1
                        if rts:
                            self._known_slack_ts.add(rts)

        logger.info(
            "Slack reconcile: appended %d messages across %d channels, %d threads",
            total_messages, len(polled_ids), total_threads,
        )

    async def _rebuild_agent_state(self) -> None:
        """Reconstruct per-agent state from the message log + DB.

        Runs after both the DB rebuild and the optional Slack reconcile, so it
        behaves identically with Slack on or off. Reads only self.message_log,
        thread_decisions, proposal_reviews and llm_call_logs — no Slack calls.
        """
        # Rebuild active_threads per agent.
        # Get all closed thread IDs and prior thread summaries from thread_decisions
        closed_thread_ids: set[str] = set()
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(sa_select(ThreadDecision))
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
                    self._closed_thread_ids.update(closed_thread_ids)
            except Exception as exc:
                logger.warning("Failed to load thread decisions: %s", exc)

        for agent in self.agents.values():
            aid = agent.agent_id
            # Find threads where this agent participated
            for entry in self.message_log._entries:
                if entry.sender_agent_id != aid:
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
                )

        # 3. Rebuild pending_proposals per agent
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    proposals_result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.outcome == "proposal"
                        )
                    )
                    proposals = proposals_result.scalars().all()

                    reviewed_result = await db.execute(
                        sa_select(
                            ProposalReview.thread_decision_id,
                            ProposalReview.agent_id,
                        )
                    )
                    reviewed_set = {
                        (r.thread_decision_id, r.agent_id) for r in reviewed_result
                    }

                # Keep only the latest ThreadDecision per (agent_id, thread_id).
                # Older rows represent prior propose/reopen cycles and their
                # reviews are stale — the most recent re-proposal is the only
                # one whose review status affects the agent's current block.
                latest_by_key: dict[tuple[str, str], ThreadDecision] = {}
                for td in proposals:
                    for aid in (td.agent_a, td.agent_b):
                        if aid not in self.agents:
                            continue
                        key = (aid, td.thread_id)
                        existing = latest_by_key.get(key)
                        if existing is None:
                            latest_by_key[key] = td
                            continue
                        td_ts = td.decided_at.timestamp() if td.decided_at else 0.0
                        ex_ts = existing.decided_at.timestamp() if existing.decided_at else 0.0
                        if td_ts > ex_ts:
                            latest_by_key[key] = td

                # A recorded collab_private proposal means that channel's
                # refinement already converged — mark it finalized so bots don't
                # re-open the discussion after a restart.
                for td in proposals:
                    if td.origin_visibility == VISIBILITY_COLLAB_PRIVATE and td.channel:
                        self._finalized_private_channels.add(td.channel)

                for (aid, _tid), td in latest_by_key.items():
                    agent = self.agents[aid]
                    is_reviewed = (td.id, aid) in reviewed_set
                    other = td.agent_b if aid == td.agent_a else td.agent_a
                    ref = ProposalRef(
                        thread_id=td.thread_id,
                        channel=td.channel,
                        other_agent_id=other,
                        summary_text=td.summary_text or "",
                        proposed_at=td.decided_at.timestamp() if td.decided_at else 0.0,
                        reviewed=is_reviewed,
                    )
                    # pending_proposals is a list, and an unreviewed entry blocks
                    # its agent. A plain append is therefore not idempotent in a
                    # way that matters: a second rebuild would give the agent two
                    # copies of one proposal, and reviewing it pops one — leaving
                    # the agent blocked on a phantom for the rest of the run.
                    # Replace in place instead; latest_by_key already holds exactly
                    # one (latest) decision per thread and the DB is authoritative,
                    # so this also refreshes a stale `reviewed` flag.
                    idx = next(
                        (i for i, p in enumerate(agent.state.pending_proposals)
                         if p.thread_id == ref.thread_id),
                        None,
                    )
                    if idx is None:
                        agent.state.pending_proposals.append(ref)
                    else:
                        agent.state.pending_proposals[idx] = ref
            except Exception as exc:
                logger.warning("Failed to rebuild proposals: %s", exc)

        # 4. Rebuild api_call_count per agent from DB
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import func as sa_func
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(
                            LlmCallLog.agent_id,
                            sa_func.count(LlmCallLog.id).label("count"),
                        )
                        .where(LlmCallLog.simulation_run_id == self.simulation_run_id)
                        .group_by(LlmCallLog.agent_id)
                    )
                    for r in result:
                        agent = self.agents.get(r.agent_id)
                        if agent:
                            agent.api_call_count = r.count
            except Exception as exc:
                logger.warning("Failed to rebuild api_call_count: %s", exc)

        # 4b. Rebuild the sliding-window call ledger from the same table.
        #
        # Deliberately SEPARATE from step 4, which stays an all-time COUNT(*):
        # api_call_count is lifetime accounting (run summary,
        # SimulationRun.total_api_calls) while call_times is the live throttle.
        # Folding these together is the bug — it is what made an over-budget
        # agent over-budget again on every restart, forever. See design §4.2.
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select

                # datetime, UTC and timedelta are already module-level imports
                # (simulation.py:10) — do not re-import them here.
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=get_settings().llm_rate_window_seconds
                )
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(LlmCallLog.agent_id, LlmCallLog.created_at)
                        .where(
                            LlmCallLog.simulation_run_id == self.simulation_run_id,
                            LlmCallLog.created_at >= cutoff,
                        )
                        .order_by(LlmCallLog.created_at)
                    )
                    rows = result.all()
                # call_times is a deque that record_api_call appends to, same
                # shape as pending_proposals above — so a plain append here is
                # not idempotent either: a second rebuild call would duplicate
                # every in-window entry and could throttle an agent that isn't
                # actually over its allowance. Unlike pending_proposals, this
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
                        agent.state.call_times.append(r.created_at.timestamp())
            except Exception as exc:
                logger.warning("Failed to rebuild call_times: %s", exc)

        # 5. Set last_seen_cursor per agent to latest message time
        if self._reset_cursors:
            logger.info("--reset-cursors: agents will re-scan all posts")
            for agent in self.agents.values():
                agent.state.last_seen_cursor = 0
        elif self.message_log._entries:
            latest_ts = max(e.posted_at for e in self.message_log._entries)
            for agent in self.agents.values():
                agent.state.last_seen_cursor = latest_ts

        # Log rebuild summary
        for agent in self.agents.values():
            at = len(agent.state.active_threads)
            pp = len(agent.state.pending_proposals)
            unrev = sum(1 for p in agent.state.pending_proposals if not p.reviewed)
            if at or pp:
                logger.info(
                    "[%s] Restored: %d active threads, %d proposals (%d unreviewed), %d API calls",
                    agent.agent_id, at, pp, unrev, agent.api_call_count,
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

    def _on_llm_call(self, data: dict) -> None:
        """Callback fired after each LLM API call."""
        self._llm_log_buffer.append(data)
        if len(self._llm_log_buffer) >= self._llm_log_flush_size:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._flush_llm_logs())
                task.add_done_callback(self._on_flush_done)
            except RuntimeError:
                pass

    @staticmethod
    def _on_flush_done(task: asyncio.Task) -> None:
        if task.exception():
            logger.error("LLM log flush failed: %s", task.exception())

    async def _flush_llm_logs(self) -> None:
        """Write buffered LLM call logs to the database."""
        if not self._llm_log_buffer or not self.session_factory or not self.simulation_run_id:
            return
        batch = self._llm_log_buffer[:]
        self._llm_log_buffer.clear()
        try:
            async with self.session_factory() as db:
                for entry in batch:
                    record = LlmCallLog(
                        simulation_run_id=self.simulation_run_id,
                        agent_id=entry.get("agent_id", "unknown"),
                        phase=entry.get("phase", "unknown"),
                        channel=entry.get("channel"),
                        model=entry.get("model", ""),
                        system_prompt=entry.get("system_prompt", ""),
                        messages_json=entry.get("messages", []),
                        response_text=entry.get("response_text", ""),
                        input_tokens=entry.get("input_tokens", 0),
                        output_tokens=entry.get("output_tokens", 0),
                        latency_ms=entry.get("latency_ms", 0.0),
                        created_at=entry.get("completed_at"),
                    )
                    db.add(record)
                await db.commit()
            logger.debug("Flushed %d LLM call logs to DB", len(batch))
        except Exception as exc:
            logger.warning("Failed to flush LLM call logs: %s", exc)

    def _sync_profiles_from_disk(self) -> None:
        """Reload any agent whose profile files changed on disk since last turn.

        Private and public profiles can be edited from the web app, which runs
        in a separate process and writes profiles/{private,public}/{id}.md on a
        shared mounted volume. Each Agent caches its profile content in memory
        and otherwise only invalidates that cache for in-process edits (the
        Slack-DM path via Agent.update_private_profile). Without this check, a
        web edit would not reach the running simulation until a restart.

        Detection is by file mtime: cheap (two stat() calls per agent, no DB
        round-trip) and tied to exactly what the agent reads. Re-reading the
        same content after an in-process Slack-DM edit is harmless.
        """
        for agent in self.agents.values():
            mtime = 0.0
            for sub in ("private", "public"):
                path = PROFILES_DIR / sub / f"{agent.agent_id}.md"
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
        """Re-sync the live agent roster from AgentRegistry (status=='active').

        Adds agents that have just been activated (and have a usable token) and
        removes agents that have been inactivated/suspended — all without a
        process restart. Tokens are read from the DB row (falling back to .env),
        so a freshly provisioned token is picked up on the next tick too.

        Mutates self.agents / self.slack_clients IN PLACE: PIHandler holds those
        dicts by reference, so they must never be reassigned.
        """
        if not self.session_factory:
            return
        now = time.time()
        if now - self._last_roster_poll < ROSTER_POLL_INTERVAL:
            return
        self._last_roster_poll = now

        try:
            from sqlalchemy import select as sa_select

            from src.agent.slack_client import AgentSlackClient
            from src.models import AgentRegistry
            from src.services.slack_tokens import env_token, is_valid_token

            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(
                        AgentRegistry.agent_id,
                        AgentRegistry.bot_name,
                        AgentRegistry.pi_name,
                        AgentRegistry.slack_bot_token,
                        AgentRegistry.role,
                    ).where(AgentRegistry.status == "active")
                )).all()

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
                    if not client.connect():
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
                self._dm_poll_cursors.pop(aid, None)
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
                    if not client.connect():
                        logger.warning("[roster] Slack connect failed for new agent %s — skipping", aid)
                        continue
                else:
                    # Slack off: admit the agent with a no-op transport (never
                    # gate on a token/connection that doesn't apply in DB-only mode).
                    from src.agent.transport import NullTransport
                    client = NullTransport(agent_id=aid)
                agent = Agent(agent_id=aid, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
                # In-place inserts (PIHandler shares these dicts by reference).
                self.agents[aid] = agent
                self.slack_clients[aid] = client
                self._bot_name_to_id[agent.bot_name.lower()] = aid
                logger.info("[roster] Added newly-active agent %s to live roster", aid)

            # Rebuild cross-agent derived structures after any membership change.
            self.message_log.set_bot_name_map(self._bot_name_to_id)
            # Rebuild PI mappings from scratch (clear in place — PIHandler shares
            # this dict by reference; _load_pi_mappings appends, so it must start
            # empty to avoid accumulating duplicates).
            self._pi_slack_id_to_agent_ids.clear()
            await self._load_pi_mappings()

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
        settings = get_settings()
        if not settings.cohort_isolation_enabled:
            self._cohort_preflight_error = None
            self._disable_all_gates()
            self.refresh_lab_directories()
            self._cohort_gate_active = False
            self._cohort_log_signature = None
            # Reconcile state even on the disabled path: turning isolation off must
            # clear grandfathered flags, or threads stay permanently deprioritised
            # after the gate that demoted them is gone.
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

    def _apply_cohort_gate_to_state(self) -> None:
        """Reconcile in-memory agent state with the freshly computed gate.

        Two jobs, both required because the gate is a *read-time* filter and state
        outlives a membership change:

        1. **Grandfather** active threads whose partner is no longer permitted
           (v2 §8). They still get Phase 4 replies — an open conversation is
           entitled to conclude rather than waste the calls already spent — but
           they are barred from the reactive-priority tier so they cannot outrank
           gate-compliant work. This is also the path that marks a *resumed* run's
           threads: the DB rebuild runs before the first recompute, so every
           restart reconstructs its open partnerships gate-blind.
        2. **Prune** banked ``interesting_posts`` whose author is no longer
           permitted (v2 §6.1). Read-time filtering never removes posts that were
           already accepted, so without this a membership change leaves stale posts
           driving Phase 5 forever.
        """
        newly_grandfathered = 0
        pruned_total = 0
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
                        "outside the cohort; it may conclude but loses reactive "
                        "priority",
                        agent.agent_id, thread.thread_id, other,
                    )

            before = len(agent.state.interesting_posts)
            if before:
                agent.state.interesting_posts = [
                    p for p in agent.state.interesting_posts
                    if not p.sender_agent_id or p.sender_agent_id in allowed
                ]
                dropped = before - len(agent.state.interesting_posts)
                if dropped:
                    pruned_total += dropped
                    logger.debug(
                        "[cohort] %s: pruned %d banked interesting_posts from "
                        "non-cohort senders", agent.agent_id, dropped,
                    )

        if newly_grandfathered or pruned_total:
            logger.info(
                "[cohort] state reconciled: %d threads grandfathered, %d stale posts pruned",
                newly_grandfathered, pruned_total,
            )

    def cohort_topology_snapshot(self) -> dict[str, Any]:
        """Serialise the gate configuration and its observed effects.

        Written to cohort_audit_events at run start and on every mid-run topology
        change, so a finished run stays attributable to every configuration it
        actually ran under (v2 §13.1). Derived from the live in-memory gate rather
        than re-querying, so it records what the engine actually applied — including
        a preflight override.

        Also carries the counters the admin UI cannot otherwise see: they live in
        this process's memory, and the web app is a different process (v2 §9.4/§13).
        """
        settings = get_settings()
        grandfathered = sorted(
            f"{aid}:{t.thread_id}"
            for aid, a in self.agents.items()
            for t in a.state.active_threads.values()
            if t.grandfathered
        )
        return {
            "cohort_isolation_enabled": settings.cohort_isolation_enabled,
            "cohort_default_policy": settings.cohort_default_policy,
            "max_consecutive_reactive_turns": settings.max_consecutive_reactive_turns,
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
                "reactive_selections": self._reactive_selections,
                "proactive_selections": self._proactive_selections,
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

    async def _sync_proposal_reviews_from_db(self) -> None:
        """Check DB for web-app proposal reviews and mark in-memory proposals as reviewed.

        For rating=0 reviews (reopened with PI guidance), also reopen the thread
        so both agents resume discussion incorporating the PI's direction.
        """
        if not self.session_factory:
            return
        try:
            async with self.session_factory() as db:
                from sqlalchemy import select as sa_select
                # Get all reviews with rating and guidance info, plus the
                # ThreadDecision's refined_in_channel marker (set when a
                # reopen migrated the refinement into a collab_private
                # channel — in that case the legacy "reopen the public
                # thread" path must NOT fire, or we'd drop the PI's
                # guidance back into the public thread and leak it).
                result = await db.execute(
                    sa_select(
                        ProposalReview.agent_id,
                        ProposalReview.rating,
                        ProposalReview.comment,
                        ThreadDecision.thread_id,
                        ThreadDecision.channel,
                        ThreadDecision.refined_in_channel,
                    )
                    .join(ThreadDecision, ProposalReview.thread_decision_id == ThreadDecision.id)
                )
                rows = list(result)

            reviewed_set = {(r.agent_id, r.thread_id) for r in rows}
            # Thread IDs of proposals that have been migrated to a private
            # channel. Any agent with a pending proposal on such a thread is
            # unblocked: the proposal is under active refinement, not
            # awaiting first review. Without this, only the PI who triggered
            # the reopen (and whose ProposalReview row exists) would be
            # unblocked — the other agent would stay blocked and silently
            # skip Phase 5 in the private channel.
            migrated_threads = {
                r.thread_id for r in rows if r.refined_in_channel is not None
            }
            if not reviewed_set and not migrated_threads:
                return

            # Build lookup for rating=0 (reopened with guidance) reviews.
            # Rows with refined_in_channel set are skipped entirely — those
            # reopens were handled by the private-channel migration flow;
            # resurrecting the legacy public-thread reopen would undo the
            # privacy guarantee.
            reopen_guidance: dict[tuple[str, str], tuple[str, str]] = {}
            for r in rows:
                if r.rating == 0 and r.comment and r.refined_in_channel is None:
                    reopen_guidance[(r.agent_id, r.thread_id)] = (
                        _strip_reopen_prefix(r.comment), r.channel,
                    )

            # Mark matching in-memory proposals as reviewed. A proposal is
            # considered reviewed for unblocking purposes if EITHER this agent
            # has a ProposalReview row OR the proposal has been migrated to a
            # private channel (refinement supersedes review).
            newly_reviewed: list[tuple[Agent, str]] = []
            for agent in self.agents.values():
                for proposal in agent.state.pending_proposals:
                    if not proposal.reviewed:
                        unblock = (
                            (agent.agent_id, proposal.thread_id) in reviewed_set
                            or proposal.thread_id in migrated_threads
                        )
                        if unblock:
                            proposal.reviewed = True
                            newly_reviewed.append((agent, proposal.other_agent_id))
                            logger.info(
                                "[%s] Proposal for thread %s marked reviewed via web app",
                                agent.agent_id, proposal.thread_id,
                            )

            # Detect rating=0 reviews that need thread reopening, independent of
            # the reviewed flag (which may already be True from a prior sync).
            # Dedupe by thread_id within this pass so that if the PI reviewed
            # both sides of the pair, we only reopen the thread once.
            newly_reopened: list[tuple[Agent, str, str, str]] = []  # agent, other_id, thread_id, guidance
            seen_reopens: set[str] = set()
            for agent in self.agents.values():
                for proposal in agent.state.pending_proposals:
                    if proposal.thread_id in seen_reopens:
                        continue
                    if proposal.thread_id in self._db_reopened_thread_ids:
                        continue
                    key = (agent.agent_id, proposal.thread_id)
                    if key in reopen_guidance:
                        guidance, _channel = reopen_guidance[key]
                        seen_reopens.add(proposal.thread_id)
                        newly_reopened.append(
                            (agent, proposal.other_agent_id, proposal.thread_id, guidance)
                        )

            # Update memory for agents whose proposals were just reviewed
            for agent, other_id in newly_reviewed:
                event = f"PI reviewed proposal with {other_id} — agent is now unblocked for new posts"
                await self._update_agent_memory(agent, event)

            # Reopen threads where PI provided guidance (rating=0)
            for agent, other_id, thread_id, guidance in newly_reopened:
                channel = None
                for p in agent.state.pending_proposals:
                    if p.thread_id == thread_id:
                        channel = p.channel
                        break
                if not channel:
                    continue

                # Old closed threads may have been windowed out of the log at
                # startup (B2); hydrate so the reply-budget offset below counts
                # the real prior history rather than 0.
                await self._hydrate_thread_from_db(thread_id)

                # Create a synthetic log entry for the PI guidance so it appears
                # in thread history and the agents can see it
                minted = self.mint_ts()
                pi_entry = LogEntry(
                    ts=minted,
                    channel=channel,
                    sender_agent_id=None,
                    sender_name="PI (via web)",
                    content=guidance,
                    thread_ts=thread_id,
                    posted_at=float(minted),
                    is_bot=False,
                )
                self.message_log.append(pi_entry)

                # Reopen the thread for both agents
                self._closed_thread_ids.discard(thread_id)
                existing_count = len(self.message_log.get_thread_history(thread_id))

                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=channel,
                    other_agent_id=other_id,
                    message_count=0,
                    has_pending_reply=True,
                    pi_context=guidance,
                    message_count_offset=existing_count,
                )

                other_agent = self.agents.get(other_id)
                if other_agent:
                    other_agent.state.active_threads[thread_id] = ThreadState(
                        thread_id=thread_id,
                        channel=channel,
                        other_agent_id=agent.agent_id,
                        message_count=0,
                        has_pending_reply=True,
                        message_count_offset=existing_count,
                    )

                self._db_reopened_thread_ids.add(thread_id)
                logger.info(
                    "[%s] PI guidance via web reopened thread %s with %s: %.60s",
                    agent.agent_id, thread_id, other_id, guidance[:60],
                )

        except Exception as exc:
            logger.debug("Proposal review sync failed: %s", exc)

    # ------------------------------------------------------------------
    # Post-simulation
    # ------------------------------------------------------------------

    async def _update_agent_memory(
        self,
        agent: Agent,
        event: str,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> None:
        """Incrementally update an agent's working memory after a significant event.

        Triggered by: thread closure, PI DM, or proposal review — not batched at
        simulation end.

        visibility/channel_id: controls which memory segment is updated and
            which subset of the message log is used as synthesis context, per
            G2. v1 callers always pass public (the default); the
            thread-closure path will pass the thread's visibility once
            private-channel migration lands.
        """
        try:
            # Gather recent activity for context — filter the message log to
            # entries with matching visibility. Public syntheses never see
            # private-channel messages, and vice-versa. See §G2.
            agent_entries = [
                e for e in self.message_log._entries
                if e.sender_agent_id == agent.agent_id
                and e.visibility == visibility
            ]
            messages_text = "\n".join(
                f"[#{e.channel}] {e.content[:200]}"
                for e in agent_entries[-20:]
            ) if agent_entries else "(no recent messages)"

            system_prompt = agent.build_thread_reply_system_prompt(
                visibility=visibility, channel_id=channel_id,
            )
            messages = [
                {
                    "role": "user",
                    "content": f"""Update your working memory. The event that triggered this update:
{event}

Your recent messages for context:
{messages_text}

Your current working memory:
{agent.working_memory or "(empty)"}

Write the complete updated working memory. Incorporate the new event, keep existing
entries that are still relevant, and remove anything outdated. Summarize:
(a) Collaboration opportunities and their status
(b) Feedback or directions from your PI (if any)
(c) Current priorities

Keep it concise — under 300 words.""",
                }
            ]

            agent.record_api_call()
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=800,
                log_meta={"agent_id": agent.agent_id, "phase": "memory"},
                on_retry=agent.record_api_call,
            )
            if not response or not response.strip():
                logger.warning("[%s] Memory update: empty response", agent.agent_id)
                return
            agent.update_working_memory_file(
                response, visibility=visibility, channel_id=channel_id,
            )
            logger.info(
                "[%s] Working memory updated (visibility=%s, trigger: %s)",
                agent.agent_id, visibility, event[:60],
            )

            # Record revision
            if self.session_factory:
                try:
                    from sqlalchemy import select as sa_sel

                    from src.models import AgentRegistry
                    from src.services.profile_versioning import create_revision
                    async with self.session_factory() as db:
                        agent_reg = (await db.execute(
                            sa_sel(AgentRegistry)
                            .where(AgentRegistry.agent_id == agent.agent_id)
                        )).scalar_one_or_none()
                        if agent_reg:
                            await create_revision(
                                db,
                                agent_registry_id=agent_reg.id,
                                profile_type="memory",
                                content=response,
                                mechanism="agent",
                                change_summary=event[:200],
                            )
                            await db.commit()
                except Exception as rev_exc:
                    logger.warning("[%s] Profile revision failed: %s", agent.agent_id, rev_exc)
        except Exception as exc:
            logger.error("[%s] Working memory update failed: %s", agent.agent_id, exc)


def _extract_slack_message(text: str) -> str:
    """Extract the message from <slack_message> tags if present, else fall back to preamble stripping.

    Uses the LAST opening tag before the LAST closing tag so that a prior
    mention of ``<slack_message>`` inside the LLM's reasoning (e.g.
    "my output is a single `<slack_message>` block") does not anchor the
    match and pull preceding reasoning into the captured body.
    """
    last_close = text.rfind("</slack_message>")
    if last_close >= 0:
        last_open = text.rfind("<slack_message>", 0, last_close)
        if last_open >= 0:
            return text[last_open + len("<slack_message>"):last_close].strip()
    # Fallback: strip preamble heuristically
    return _strip_llm_preamble(text)


# Case-insensitive and tolerant of stray whitespace inside the delimiters
# (e.g. `<ASSESSMENT_JSON>`, `<assessment_json >`) — a model is not guaranteed
# to reproduce the tag verbatim, and a tag variant that slips past these
# regexes is a verdict that leaks straight into Slack.
_ASSESSMENT_RE = re.compile(
    r"<\s*assessment_json\s*>\s*(.*?)\s*<\s*/\s*assessment_json\s*>",
    re.DOTALL | re.IGNORECASE,
)

# An opening tag with no matching close — e.g. the LLM response got truncated
# mid-sidecar (Phase 5's max_tokens budget plus an 11-section body ahead of a
# ~15-line sidecar makes this a realistic outcome, and the retry path does not
# re-check stop_reason). `_ASSESSMENT_RE` requires a literal closing tag, so it
# does not match an unclosed one and would leave the raw verdict JSON — scores,
# red flags, recommendation — sitting in the text. Strip everything from the
# orphaned opening tag to the end of the response instead.
#
# This can discard trailing legitimate prose that happened to follow the
# sidecar. That is an accepted, deliberate trade-off: losing a sentence of
# prose is strictly better than leaking dimension scores and red flags into a
# channel the assessed scientist reads. Do not "optimize" this into a lazy
# match that stops short of end-of-string — the whole point is to consume
# unconditionally to the end once an unclosed opening tag is found.
_ASSESSMENT_UNCLOSED_RE = re.compile(
    r"<\s*assessment_json\s*>.*", re.DOTALL | re.IGNORECASE
)

# Mop-up for any stray tag markup neither of the above removed (e.g. an
# orphaned closing tag with no opening).
_ASSESSMENT_ORPHAN_TAG_RE = re.compile(
    r"<\s*/?\s*assessment_json\s*>", re.IGNORECASE
)


def _strip_assessment_sidecar(text: str) -> str:
    """Remove the <assessment_json> sidecar from ``text`` before it reaches Slack.

    That block is for Blackbird staff and the DB, never for the channel. Order
    matters:
      1. Remove well-formed pairs whole (tags + contents).
      2. Anything left starting with an opening tag has no matching close —
         truncated mid-sidecar — so drop from there to the end of the text
         rather than leave the verdict JSON exposed.
      3. Mop up any remaining stray tag markup neither step removed.
    """
    text = _ASSESSMENT_RE.sub("", text)
    text = _ASSESSMENT_UNCLOSED_RE.sub("", text)
    text = _ASSESSMENT_ORPHAN_TAG_RE.sub("", text)
    return text


# A model routinely wraps the sidecar's JSON in a ```json``` fence despite the
# prompt asking for bare JSON (see _parse_phase5_response, which now strips
# the whole <assessment_json>...</assessment_json> span — fence included —
# before it ever looks for the action). That strip protects the action parse
# unconditionally, but it would be a shame to also throw the verdict away
# just because it arrived fenced: tolerate one optional wrapping fence here
# too, so the verdict itself still comes through.
_SIDECAR_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _unfence_sidecar(raw: str) -> str:
    """Strip one optional ```json```/``` fence wrapping ``raw``, if present."""
    match = _SIDECAR_FENCE_RE.match(raw.strip())
    return match.group(1) if match else raw


def _extract_assessment_json(text: str) -> dict | None:
    """Parse the scout hub's machine-readable verdict sidecar, or None.

    The sidecar is deliberately BARE JSON, not a ```json``` fence:
    _parse_phase5_response strips this whole tagged span before it ever looks
    for the action fence, so a fenced sidecar would otherwise hijack the
    action data and silently no-op every assessment post. A fenced sidecar is
    still tolerated here (see _unfence_sidecar) — the action is already
    protected regardless, so there is no reason to also lose the verdict over
    a fence the model added despite the instruction not to.

    Walks blocks newest-first and returns the first one that parses to a JSON
    object — "the newest verdict that is actually usable", not "the newest
    block, or nothing": a model that emits a good verdict and then a broken
    revision (invalid JSON, or valid JSON that isn't an object, e.g. an array)
    must not lose the good one just because it revised afterward. Last-wins is
    still right when a revision *does* parse — it should supersede the
    earlier verdict, which is exactly what returning on the first hit here
    does. Returns None only when no block parses to a dict; never raises.
    """
    matches = _ASSESSMENT_RE.findall(text or "")
    if not matches:
        return None
    last_index = len(matches) - 1
    for index in range(last_index, -1, -1):
        try:
            parsed = json.loads(_unfence_sidecar(matches[index]))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "[assessment] sidecar block %d/%d unparseable: %s",
                index + 1, len(matches), exc,
            )
            continue
        if not isinstance(parsed, dict):
            logger.warning(
                "[assessment] sidecar block %d/%d parsed but was not a JSON "
                "object (got %s)",
                index + 1, len(matches), type(parsed).__name__,
            )
            continue
        if index != last_index:
            logger.warning(
                "[assessment] using sidecar block %d/%d — %d later block(s) "
                "were present but unusable, so an earlier valid verdict was "
                "used instead of losing it",
                index + 1, len(matches), last_index - index,
            )
        return parsed
    return None


def _sidecar_has_valid_json_block(text: str) -> bool:
    """True if any <assessment_json> block in ``text`` is syntactically valid
    JSON, whatever its shape.

    Lets a caller distinguish "no block ever parsed" from "a block parsed
    fine but wasn't an object" (Finding A3) — the two outcomes both leave
    ``_extract_assessment_json`` returning None, but only the first is
    actually "unparseable".
    """
    for raw in _ASSESSMENT_RE.findall(text or ""):
        try:
            json.loads(_unfence_sidecar(raw))
        except (json.JSONDecodeError, ValueError):
            continue
        return True
    return False


# The prompt's tri-state gating contract (see prompts/roles/scout_hub/phase5-new-post.md):
# every gating.* value must be exactly one of these three strings, never a bare
# boolean — "the PI declined" (not_met) and "we never asked" (unconfirmed) are
# different facts, and a boolean can express only the first two of these three
# outcomes.
_VALID_GATING_STATES = frozenset({"met", "not_met", "unconfirmed"})


def _normalize_gating(raw: object) -> dict | None:
    """Filter a verdict's ``gating`` map down to the keys already conforming to
    the tri-state contract; drop only the keys that don't.

    The ``gating`` column is plain JSONB, so nothing at the database layer
    stops a pre-tri-state boolean value — or a genuinely malformed one — from
    being written verbatim. A key that sometimes holds ``true``/``false`` and
    sometimes holds ``"met"``/``"not_met"``/``"unconfirmed"`` is worse than one
    that is occasionally absent: a consumer cannot tell which convention a
    given key uses without inspecting its value, which defeats the point of a
    structured column. So each key is kept only when its own value already
    conforms; anything else is dropped for that key alone.

    Filtering per key rather than dropping the whole map (Task 11 fix round 1,
    Finding 4) matches how every sibling field on this row already degrades —
    ``red_flags``/``derisking_milestones`` null only when THEY are the wrong
    type, never because some unrelated field was also bad. Wholesale-dropping
    a map with three good gates over one bad one denied the (now-shipped)
    triage page three gates it could have shown, for no correctness benefit:
    the reason to refuse the bad key stands on its own (see below) and has
    nothing to do with its siblings.

    Booleans are deliberately NOT coerced (``True`` -> "met", ``False`` ->
    "not_met"): under the old boolean-only contract there was no way to say
    "unconfirmed" at all, so a legacy ``False`` is genuinely ambiguous between
    "not_met" and "unconfirmed" — guessing would fabricate a certainty the
    original verdict never had, so that key is omitted rather than guessed.
    This never loses information regardless: ``raw_verdict`` keeps the
    original ``gating`` value verbatim no matter what survives here.

    Returns ``None`` when ``raw`` isn't a dict at all, or when no key survives
    filtering — an empty structured map is no more useful than a missing one.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    kept = {
        key: value for key, value in raw.items()
        if isinstance(value, str) and value in _VALID_GATING_STATES
    }
    return kept or None


def _bounded_str(value: object, max_len: int) -> str | None:
    """Coerce a verdict field expected to be a short string into one that
    fits its column, or drop it if it isn't a (non-empty) string at all.

    Every other field on this row degrades per-field on a bad value via an
    isinstance check ahead of the insert; a short VARCHAR column is the one
    place a value of the *right* type can still blow up the write, since an
    oversized string is a perfectly good Python str right up until Postgres
    raises DataError at commit — which takes the whole row down with it, not
    just the one field (Task 11 fix round 1, Finding 5). Truncating instead of
    dropping is deliberate: a clipped recommendation is still useful for
    triage, an absent one is not. ``raw_verdict`` keeps the untruncated
    original regardless.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:max_len]


def _str_or_none(value: object) -> str | None:
    """Coerce a verdict field expected to be a string, dropping it if it
    isn't a (non-empty) string at all.

    ``company_or_project``/``rationale`` are Text columns, not bounded
    VARCHARs like ``_bounded_str`` guards, so there is no length to truncate
    to — but they are exactly as exposed to a wrong-typed value. A model that
    emits a structured (dict/list) ``rationale`` instead of prose is still a
    plain Python object of the wrong type for this column, and passing it
    straight to the ORM raises at commit — which takes the whole row down
    with it, the same failure ``_bounded_str`` exists to prevent for the
    VARCHAR columns (F9).
    """
    return value if isinstance(value, str) and value else None


def _strip_llm_preamble(text: str) -> str:
    """Remove LLM internal reasoning that leaks before the actual Slack message.

    Strategy: split into paragraphs, identify the first paragraph that looks like
    an actual Slack message (not meta-commentary), and discard everything before it.
    """
    # If there's a --- separator, take everything after the last one
    if "\n---\n" in text:
        parts = text.split("\n---\n")
        candidate = parts[-1].strip()
        if candidate:
            text = candidate

    # Split into paragraphs (separated by blank lines)
    paragraphs = re.split(r"\n\s*\n", text.strip())
    if len(paragraphs) <= 1:
        return text

    # Patterns that indicate internal reasoning / meta-commentary
    _PREAMBLE_RE = re.compile(
        r"^("
        r"(That('s| is) (not|exactly|interesting))"
        r"|Let me"
        r"|I('ll| should| need| couldn't| didn't| can't| wasn't| don't| have| want)"
        r"|Now I (have|can|know|need|should)"
        r"|These |The (search|result|profile|paper|abstract|tool|API|PubMed|query)"
        r"|My (search|query|tool|approach)"
        r"|Based on|After (review|search|look)|Since (the|I|my)"
        r"|Looking at|It seems|Ok[,.]|Okay[,.]|Hmm"
        r"|This (is|gives|shows|confirms|doesn't|isn't)"
        r"|None of|No (relevant|useful|results)"
        r"|Unfortunately"
        r")",
        re.IGNORECASE,
    )

    # Find the first non-preamble paragraph
    for i, para in enumerate(paragraphs):
        first_line = para.strip().split("\n")[0]
        if not _PREAMBLE_RE.match(first_line):
            if i > 0:
                stripped = "\n\n".join(paragraphs[i:]).strip()
                logger.info(
                    "Stripped %d preamble paragraph(s): %.120s",
                    i, " | ".join(p.strip()[:50] for p in paragraphs[:i]),
                )
                return stripped
            break

    return text


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from LLM response text."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract JSON from response: {text[:200]}")
