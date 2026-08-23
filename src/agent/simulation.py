"""Turn-based simulation engine — coordinates all agents across all channels."""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import func
from sqlalchemy.exc import DataError, IntegrityError

from src.agent.agent import PROFILES_DIR, Agent
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.agent.ids import WRITER_ENGINE, TsMinter
from src.agent.locks import LockRegistry
from src.agent.message_log import PHASE_PANEL_NOTE, LogEntry, MessageLog, is_panel_note
from src.agent.post_types import (
    PostTypeSpec,
    available_for,
    eligible_targets,
    render_menu,
    resolve_post_type_name,
)
from src.agent.prompt_safety import delimit
from src.agent.roles import load_role
from src.agent.slack_client import SlackListingIncomplete, ThreadNotFound
from src.agent.specialists import (
    clear_rate_warning,
    format_panel_note,
    panel_is_owed,
    required_domains_for,
)
from src.agent.state import ProposalRef, ThreadState
from src.agent.thread_guidance import CONCLUDE, phase4_guidance
from src.agent.tools import execute_tool, tools_for_role
from src.config import get_settings
from src.models import (
    AgentChannel,
    AgentMessage,
    AssessmentDrop,
    LlmCallLog,
    OpportunityAssessment,
    ProposalReview,
    SimulationRun,
    SpecialistConsult,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score
from src.services.cohorts import compute_gates, summarise_gates
from src.services.llm import (
    generate_agent_response,
    generate_with_tools,
    is_truncated_stop,
    set_call_log_callback,
)

logger = logging.getLogger(__name__)


def _was_truncated(stop_reasons: list[str]) -> bool:
    """Did the reply this turn is holding stop BEFORE the model finished it?

    ``src/services/llm.py`` reports the terminating ``stop_reason`` through an
    ``on_stop_reason`` callback and still RETURNS the partial text, because
    whether a partial answer may be posted, persisted or credited differs per
    call site. Every engine call site therefore collects the reason into a list
    (``on_stop_reason=stop_reasons.append``, the idiom ``src/agent/tools.py``
    already uses) and asks this.

    ``is_truncated_stop`` rather than a ``refusal``-only test, and deliberately
    not re-derived here: ``refusal`` is the classifier cutting the generation and
    ``max_tokens`` is the ceiling doing it, the text in hand is equally partial,
    and a reply that truncated, retried and truncated again reports
    ``max_tokens`` — as does a fallthrough from the retry path whose first pass
    was refused. Until 2026-08-22 ``on_stop_reason`` had NO reader in this module
    at all, so a truncated hub reply was posted to Slack as complete and a
    truncated synthesis overwrote a good working memory.

    ``any`` rather than "the last one": the contract says the callback fires
    exactly once, and a guard about incomplete text should not become a no-op if
    that ever changes.
    """
    return any(is_truncated_stop(reason) for reason in stop_reasons)


#: Appended to a hub/lab reply whose generation was cut off, so the PI reading
#: the thread is not left to guess why it stops mid-sentence.
#:
#: The partial text is KEPT and posted. Discarding it looks safer and is not:
#: `_reply_to_thread`'s next guard is "empty or unparseable", which increments
#: `empty_response_count` and, on a second occurrence, backs off the thread and
#: records an `empty_reply` drop — the interview abandoned, no verdict, no later
#: turn. That is how a real interview died. A marked partial reply keeps the
#: conversation alive and tells the truth about itself.
TRUNCATION_NOTICE = (
    "\n\n_(This reply was cut off before it finished — treat it as incomplete.)_"
)


#: The DB errors that condemn ONE ROW rather than the connection, the session or
#: the pool — and therefore the only ones a per-row retry can possibly help with.
#:
#: The gate matters more than the recovery. The commonest failure on every flush
#: path here is the pool-checkout timeout `_persist_assessment`'s own comment
#: names, and it is a property of the POOL: retrying the batch one row at a time
#: issues N more sequential checkouts against a pool that is already exhausted.
#: At ~15 rows and a 30 s checkout timeout that is 450 s inside `stop()`, which
#: overruns the documented 420 s `docker stop` grace, gets the process SIGKILLed,
#: and loses the batch PLUS everything not yet flushed. So: never `except
#: Exception` into the fallback. Anything not in this tuple is transient by
#: assumption and the batch is re-queued whole, exactly as before.
_ROW_LEVEL_DB_ERRORS = (IntegrityError, DataError)

#: Wall-clock ceiling on ONE per-row recovery pass, for the same reason: even a
#: genuinely row-level error can arrive with a slow database behind it, and this
#: code runs inside a bounded stop grace. Rows the deadline stops us attempting
#: are re-queued, not dropped.
PER_ROW_RECOVERY_DEADLINE_S = 30.0


#: How many REAL API calls one ``llm_call_logs`` row represents.
#:
#: A row is one TURN; `call_stats` has one entry per billed API call, and 78.6%
#: of stored `thread_reply` rows are 2+ calls. Live booking counts calls
#: (`Agent.record_api_call` plus `SimulationEngine._unbooked_calls` for the tool
#: rounds), so the restart rebuild has to as well or every restart silently
#: loosens the throttle by the calls-to-turns ratio.
#:
#: The COALESCE is not defensive tidiness: 4,650 of the 5,771 stored rows have
#: `call_stats IS NULL` (the column arrived in migration 0032), and NULL
#: propagates through SUM — a bare `jsonb_array_length` collapses the lifetime
#: rebuild to NULL and loosens the throttle in the OTHER direction. A row that
#: recorded nothing is worth exactly the one call we know it made.
_CALLS_PER_LOG_ROW = func.coalesce(
    func.jsonb_array_length(LlmCallLog.call_stats), 1
)


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

# Slack poll throttles. Human channel messages are rare, so sub-turn latency is
# unnecessary; polling every turn was saturating one bot token's rate limit.
CHANNEL_POLL_INTERVAL = 15.0   # seconds between conversations.history sweeps
ROSTER_POLL_INTERVAL = 30.0    # seconds between AgentRegistry roster re-syncs

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

# How many queued working-memory updates stop() will still run. Each is a
# real LLM call (seconds); the container's stop grace period (-t 420) was
# sized for ONE 16k call, so an unbounded shutdown drain can outlive it and
# get SIGKILLed mid-flush. Anything beyond this bound is dropped LOUDLY.
MEMORY_EVENTS_MAX_AT_SHUTDOWN = 10

# Closed-thread summaries kept in memory per agent pair, for the Phase-5
# dedup context. The DB's thread_decisions table remains the full record;
# this bounds only what a process accumulates (audit finding 5: one dict per
# close, forever). Must be >= agent.PRIOR_THREADS_RENDERED_PER_PAIR.
PRIOR_THREADS_KEPT_PER_PAIR = 50

# Startup rebuild window (B2): the MessageLog is hydrated with messages from the
# last REBUILD_WINDOW_S plus the full history of any still-undecided thread, so
# RAM/startup cost grows with recent + live volume rather than all-time history.
# Old *closed* threads are left in the DB and hydrated on demand if a PI reopens
# one (see _hydrate_thread_from_db). Sized to comfortably cover any active
# conversation's lifetime.
REBUILD_WINDOW_S = 14 * 24 * 3600  # 14 days


class _HeldVerdict(NamedTuple):
    """The verdict an interview thread already holds, and the turn it came from.

    Recorded per thread in ``SimulationEngine._assessed_threads`` so a second
    ``<assessment_json>`` sidecar on the same thread can be JUDGED rather than
    merely counted: a re-capture of the same turn is a duplicate and is refused,
    while a strictly later reply that concludes or closes the interview is the
    better-informed verdict and supersedes this one. See ``_sidecar_refusal``.

    ``ordinal`` is the message ordinal of the reply that carried it
    (``thread.message_count + 1`` as read at capture time). ``final`` means that
    reply CLOSED the thread, so no later turn exists and nothing may supersede
    it. ``slack_ts`` is the stored row's own link back to that reply, and the
    only handle ``_retire_superseded_verdict`` has for finding the row again —
    ``opportunity_assessments`` carries no thread id of its own.

    ``announced`` records whether this verdict already produced an
    ``#assessments-summary`` headline. Deliberately separate from ``final``: a
    CONCLUDE-ordinal reply is terminal enough to ANNOUNCE, but not enough to
    freeze the thread, because ``thread_guidance`` renders CONCLUDE for every
    ordinal above 11 — so a longer interview gets a run of concluding turns and
    the last of them is still the verdict of record. Conflating the two blocks
    that supersession. And because a headline is a public Slack post that cannot
    be retracted, a superseded verdict that was already announced does not get
    announced again: the row changes, the channel keeps the first word.
    """

    ordinal: int
    final: bool
    slack_ts: str | None
    announced: bool = False


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
    ):
        self.agents = {a.agent_id: a for a in agents}
        self.slack_clients = slack_clients
        self.max_runtime_minutes = max_runtime_minutes
        self.budget_cap = budget_cap
        self.session_factory = session_factory
        self.simulation_run_id = simulation_run_id
        self._reset_cursors = reset_cursors
        # True only for `--fresh`, which has just deleted this run's
        # agent_messages/agent_channels rows. The engine has to KNOW that,
        # because the DB wipe is only one of the ways prior state gets back in:
        # the Slack transport still holds every message the workspace ever saw,
        # and both the startup reconcile and the live poller will happily
        # re-import it. See _restore_slack_state.
        self._fresh_start = fresh_start
        # When False, the local DB is the sole conversation store and no Slack
        # API calls are made (transports are NullTransport). Drives the roster
        # gate and the DB inbox poller. See specs/local-db-conversations.md.
        self.slack_enabled = slack_enabled

        # role name -> calls_per_load_per_window override (or None). See _calls_per_load.
        self._role_rate_cache: dict[str, int | None] = {}

        # role name -> declared post_types. Same reason as _role_rate_cache
        # above: load_role() hits the disk on every call.
        self._role_post_types_cache: dict[str, tuple[PostTypeSpec, ...]] = {}

        # (pi_agent_id, thread_id) -> the specialist domains consulted during
        # that interview. Keyed per INTERVIEW, not per PI: a PI's second
        # interview must convene its own panel rather than inherit the first
        # one's. `huganir` was assessed 4 times in run 1787010946 and every
        # assessment after the first rode on the first interview's consults.
        # `thread_id` is None for direct callers that have no interview.
        # In-memory on purpose: it is read by _persist_assessment one LLM call
        # later, in the SAME process. A restart clears it, and the floor then
        # fails OPEN for threads that predate the restart — see
        # _persist_assessment.
        #
        # Why fail open, now that a gap no longer costs the verdict: flagging
        # instead would mark every thread that survived a restart as
        # panel_incomplete, including the ones whose panel genuinely WAS
        # convened before the restart cleared this map. That is a false
        # accusation on a real number. What failing open costs is subtler and
        # is what `_floor_verifiable` exists to stop: an unverifiable verdict
        # used to be stored as `panel_incomplete=False, missing_domains=NULL`,
        # indistinguishable from a verified-complete panel, so every
        # post-restart verdict silently inflated the clean-panel count. It is
        # now recorded as the third state, `missing_domains=[]` — unverified.
        self._specialist_consults: dict[tuple[str, str | None], set[str]] = {}

        # verdict_signal -> count, for the whole run. The panel returned caution
        # or blocking on 142/142 consults in run 1787010946 and never once
        # cleared anything; a signal with no variance carries no information,
        # and it took an audit to notice. Tallied so the run says so itself.
        self._consult_signal_counts: dict[str, int] = {}

        self._start_time: datetime | None = None
        self._running = False
        self.message_log = MessageLog()

        # Agent name lookups
        self._bot_name_to_id: dict[str, str] = {
            a.bot_name.lower(): a.agent_id for a in agents
        }
        self.message_log.set_bot_name_map(self._bot_name_to_id)

        # LLM call log buffer
        self._llm_log_buffer: list[dict] = []
        self._llm_log_flush_size = 10
        # Every fire-and-forget flush task `_on_llm_call` has spawned and that
        # has not finished yet. `stop()` gathers these before its own final
        # flush; without that, `asyncio.run` cancelled them at interpreter
        # shutdown, and because `_flush_llm_logs` takes its batch OUT of the
        # buffer before awaiting the commit, a cancelled task loses those rows
        # from the buffer AND the database. Entries are discarded in
        # `_on_flush_done`, so the set does not grow for the run's life.
        self._flush_tasks: set[asyncio.Task] = set()

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

        # Assessments-summary channel ID (hub-only, created separately from SEEDED_CHANNELS)
        self._assessments_summary_channel_id: str | None = None

        # Slack poll cursor: channel_id -> latest ts seen
        self._poll_cursors: dict[str, str] = {}

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
        # prevents (every observed one came from a single process), and the only
        # durable alternative is a join back through `agent_messages`, because
        # `opportunity_assessments.slack_ts` is the REPLY's ts and the table
        # carries no thread_id of its own. A restart mid-interview can therefore
        # still let a second verdict through — but `max_thread_messages` closes a
        # thread the turn after it concludes, so there is normally no second
        # concluding turn to come back to.
        self._assessed_threads: dict[str, _HeldVerdict] = {}

        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}

        # Names of collab_private channels whose refinement had converged on a
        # recorded revised proposal (outcome='proposal', origin_visibility=
        # collab_private). The live handshake that finalized these was retired
        # by the pitch-only reconciliation — this set is now populated only at
        # startup/rebuild from legacy ThreadDecision rows, but is still read so
        # a legacy-finalized channel stays closed for further discussion.
        self._finalized_private_channels: set[str] = set()

        # Last-seen mtime of each agent's on-disk public profile file, keyed by
        # agent_id. The web editor runs in a separate process and writes
        # profiles/public/{id}.md on a shared volume; this process caches
        # profile content per Agent, so a per-turn mtime check tells us when an
        # external edit happened and the cache must be invalidated. See
        # _sync_profiles_from_disk.
        self._profile_mtimes: dict[str, float] = {}

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
        self._poll_client_cursor: int = 0
        # Last wall-clock time the AgentRegistry roster was re-synced (live
        # add/remove of agents as their status flips). See _sync_roster_from_db.
        self._last_roster_poll: float = 0.0

        # DB persistence buffer for the message log. MessageLog.append fires a
        # sync callback that enqueues here; _flush_persisted() batch-writes to
        # agent_messages once per main-loop tick. This makes the DB the primary
        # conversation store. See specs/local-db-conversations.md.
        self._pending_persist: list[LogEntry] = []
        # DB persistence buffer for OpportunityAssessment rows that failed
        # their first write attempt (e.g. a pool-checkout timeout) — queued
        # here by _persist_assessment instead of being dropped, and drained by
        # _flush_pending_assessments on the SAME per-turn cadence as
        # _pending_persist/_llm_log_buffer above (see _run_main_loop and
        # stop()), so the shutdown flush covers the last assessment of a run
        # too. This table is the actual product of the screening pipeline.
        self._pending_assessments: list[dict] = []
        # Working-memory events deferred from _close_thread. The close used
        # to run its two _update_agent_memory LLM calls inside the thread
        # lock + BOTH agents' locks + a reply-lane semaphore slot; in the
        # star topology every close shares the hub's agent lock, so closes
        # serialized on two LLM calls each and blocked semaphore slots for
        # the duration (docs/audits/2026-08-21-perf-memory-race, finding 1).
        # Queued here and drained OUTSIDE the dispatch fan-out, one event at
        # a time — sequential draining is what preserves the lost-update
        # guarantee the agent lock used to provide for these calls: no two
        # updates for one agent can interleave, and each reads the memory
        # text its predecessor wrote. Entries are (agent_id, event,
        # visibility, channel_id); agent_id (not the Agent object) because a
        # roster sync can rebuild the object between enqueue and drain.
        self._pending_memory_events: list[tuple[str, str, str, str | None]] = []
        # Guards against two drains running at once (main loop vs stop(), or
        # a future second call site): concurrent drains would pop same-agent
        # events into overlapping LLM calls — exactly the lost update this
        # queue exists to prevent.
        self._memory_drain_lock = asyncio.Lock()
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
        # Wall-clock of the last cosmetic run-stats refresh (total_messages /
        # total_api_calls), throttled to RUN_STATS_UPDATE_INTERVAL. See
        # _flush_persisted (B1).
        self._last_run_stats_update: float = 0.0
        # Set by request_stop() (the signal handler's sync entry point) to both
        # end the main loop and cut short an in-progress idle-backoff sleep, so
        # the final flush happens well inside the container's stop grace period.
        # See _sleep / request_stop (R2).
        self._stop_event = asyncio.Event()

        # Two-lane concurrent scheduler (docs/specs/2026-08-14-two-lane-
        # concurrent-scheduler-design.md §3). Per-key lock registries, keyed
        # by thread_id and agent_id respectively — disjoint namespaces, so a
        # single coroutine holding one can never re-acquire the other under
        # the same key and deadlock on itself. That disjointness does NOT by
        # itself rule out a cross-coroutine deadlock, though: two coroutines
        # acquiring the SAME TWO registries in opposite order still can. The
        # one global rule that prevents it, enforced by convention (no
        # compiler-checked guarantee exists — see
        # test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller
        # and test_no_call_site_bypasses_acquire_all, both in
        # tests/unit/test_reply_lane.py, for the empirical regression):
        #
        #   Acquire the thread lock before the agent lock. Never the reverse.
        #
        # Every call site that takes both follows this: the reply lane
        # (_dispatch_reply_lane's _run) holds a thread lock for the whole
        # servicing span, and everything nested under it that also needs an
        # agent lock (_close_thread, _evict_dead_thread) acquires the agent
        # lock WHILE the thread lock is already held. _phase5_new_post takes
        # only the agent lock and never a thread lock (its own _post_message
        # call never carries a thread_ts, so it can never reach
        # _evict_dead_thread), so it can't invert the order.
        self._thread_locks = LockRegistry()
        self._agent_locks = LockRegistry()
        # Bounds concurrent reply-lane tasks PROCESS-WIDE. Constructed once,
        # here, and never re-constructed per call/per turn — the whole reason
        # the OLD Phase-4 fan-out semaphore (`_llm_fanout_sem`, deleted here)
        # failed at this: it bounded one turn's own fan-out, so N concurrent
        # turns gave N x cap concurrent requests (spec §6.3, ported test
        # `test_the_fanout_bound_is_global_not_per_turn` below).
        self._reply_sem = asyncio.Semaphore(
            max(1, get_settings().reply_lane_max_in_flight)
        )
        # Dispatcher-level in-flight dedup (spec §4.3): a (agent, thread) pair
        # already being serviced — by this dispatch call's own sub-tasks, or
        # by an earlier dispatch call that is still draining when the next
        # tick's call starts — must not be spawned a second time.
        self._reply_in_flight: set[tuple[str, str]] = set()

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

    def _allowance_for(self, agent: Agent) -> int:
        """Window allowance for one agent. The hub is on its own ceiling.

        A ``scout_hub`` sits on an unpaced lane (Task 9's reservation reply
        path fires without the per-turn fan-out cap re-checking it), so the
        per-load allowance that bounds every ``pi_lab`` no longer applies to
        it — it gets ``hub_llm_calls_per_window`` instead, a brake against
        runaway rather than a load-scaled budget. Every other role keeps the
        existing formula. Shared by both ``_within_rate_limit`` (selection)
        and ``Agent.try_reserve`` (spend) so the two checks cannot disagree
        about what the hub deserves — that disagreement is exactly what
        benched the hub for 161 turns in run 4f1e8395.
        """
        settings = get_settings()
        if agent.role == "scout_hub":
            return settings.hub_llm_calls_per_window
        return self._calls_per_load(agent) * self._agent_load(agent)

    def _within_rate_limit(self, agent: Agent, now: float) -> bool:
        """Sliding-window LLM rate check — the LIVE throttle.

        allowance = ``self._allowance_for(agent)``: ``_calls_per_load(agent) *
        _agent_load(agent)`` for a pi_lab, or ``hub_llm_calls_per_window`` for
        the scout_hub, over llm_rate_window_seconds. Unlike the cumulative cap
        this replaces, it self-heals: entries age out, so an agent throttled
        now is eligible later. See design §4.2, §5.

        This is the SELECTION-time check (consulted by ``_turn_eligible``).
        The SPEND-time check is ``Agent.try_reserve``, called immediately
        before each LLM call in ``_reply_to_thread`` / ``_phase5_new_post`` —
        a selection-time-only check cannot bound concurrent spend once several
        calls are in flight for one agent.
        """
        allowance = self._allowance_for(agent)
        window_start = now - get_settings().llm_rate_window_seconds
        times = agent.state.call_times
        while times and times[0] < window_start:
            times.popleft()
        ok = len(times) < allowance
        if not ok and not agent.state.throttled:
            logger.warning(
                "[%s] throttled: %d LLM calls in the last %ds (allowance %d). "
                "Eligible again as the window slides.",
                agent.agent_id, len(times),
                get_settings().llm_rate_window_seconds, allowance,
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
        self._ensure_assessments_summary_channel()
        await self._persist_seeded_channels()
        # Load any collab_private channels created via the web-UI reopen flow
        # BEFORE rebuilding state so the rebuild's history-fetch loop covers
        # them too — otherwise the handover message wouldn't land in the
        # message log until the first per-turn poll tick.
        await self._sync_private_channels_from_db()
        # The DB is the primary conversation store. Register the persist hook,
        # hydrate the log from the DB, then (only when Slack is connected)
        # reconcile with Slack history, and finally reconstruct per-agent state
        # from the combined log. This whole sequence runs with Slack fully off.
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
        # Rebuild advanced last_seen_cursor to max(all_messages), which can
        # overshoot messages in private channels (typically older than the
        # latest public chatter). Rewind member-bot cursors so later phases can
        # still see the handover and any subsequent private-channel activity.
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

        # NOTE: every agent's staleness clock is anchored at CONSTRUCTION, by
        # `AgentState` itself — see the comment on that field. This used to be a
        # one-shot loop here, which covered the startup roster and nothing else:
        # `_sync_roster_from_db`'s add path builds an `Agent(...)` mid-run and
        # never reached it, so a mid-run addition monopolised the scheduler.
        # Do not re-add the loop; it would imply the anchor is a startup concern,
        # which is the belief that let the roster-add path ship without one.

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

                # DB-native inbound path: messages written by other processes
                # (private-channel handover, and legacy human-authored rows). Runs
                # regardless of Slack.
                await self._poll_inbound_from_db()

                # Sync any newly-created private channels from the web app.
                # DB-driven, so a single tick picks it up.
                await self._sync_private_channels_from_db()

                # Pick up active/inactive flips (and newly-provisioned tokens) from
                # the DB so the roster changes live, without a process restart.
                await self._sync_roster_from_db()

                # Pick up profile edits made from the web app (separate process).
                self._sync_profiles_from_disk()

                # Reply lane: service every (agent, thread) pair owing a reply,
                # every tick, with no pacing at all — before the post lane's
                # weighted draw runs. See docs/specs/2026-08-14-two-lane-
                # concurrent-scheduler-design.md §2.1.
                #
                # Fix round 2 (task review): the blanket try/except that used to
                # wrap this whole call is gone — `_dispatch_reply_lane` now
                # isolates both one pair's servicing failure (fix round 1, C2)
                # AND one agent's Phase-3-activation failure (fix round 2) from
                # their siblings; what is left unguarded is a genuine failure in
                # pair *selection* itself, which is a real bug that should
                # surface rather than repeat one swallowed ERROR per tick forever
                # while no interview progresses.
                #
                # "Did work" for the backoff below must reflect actual SPEND, not
                # attempts (fix round 2, Critical): `_dispatch_reply_lane`'s
                # return counts pairs ATTEMPTED, including ones the reservation
                # limiter deferred with zero LLM calls and `has_pending_reply`
                # left True — so the identical pair recurs every tick. Driving
                # the backoff off the attempt count alone spun the main loop at
                # native tick speed (measured ~2,800 iterations/s) whenever a
                # pending pair was rate-limited: never sleeping, never yielding
                # to _flush_persisted/_flush_llm_logs/_flush_pending_assessments,
                # and hammering _poll_inbound_from_db /
                # _sync_private_channels_from_db every iteration. Comparing the
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
                        break
                    if reply_lane_did_work:
                        # The reply lane made a real LLM call this tick even
                        # though no post-lane agent was eligible right now. This
                        # is not an idle tick — sleeping the idle backoff here
                        # would pace the "unpaced" lane, delaying the next reply
                        # sweep by up to 30s (fix round 1, I2).
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

                # Update last_selected
                agent.state.last_selected = time.time()
                turn_count += 1

                # Idle backoff: if no LLM calls were made in EITHER lane this
                # tick, delay before next turn. Reply-lane SPEND counts too (fix
                # round 1, I2 / fix round 2, Critical) — the hub in particular
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
                # See .notes/cohort-system-v2.md §10.3.
            finally:
                await self._drain_and_flush()

        logger.info("Main loop exited after %d turns", turn_count)

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
        """
        if self._pending_memory_events:
            await self._drain_memory_events()

        # Flush buffered message-log entries + LLM logs + any assessment
        # rows that failed their first write.
        await self._flush_persisted()
        if self._llm_log_buffer:
            await self._flush_llm_logs()
        if self._pending_assessments:
            await self._flush_pending_assessments()

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

        # A RATE test, not a zero test. The old `not counts.get("clear")` form
        # was silenced by a single outlier, and run 8b64a0e0 was the first run to
        # silence it: 141 caution / 26 blocking / 1 clear out of 168, and that
        # one `clear` is the only one in the database's entire history. The alarm
        # existed precisely for that distribution and could not fire.
        alarm = clear_rate_warning(self._consult_signal_counts)
        if alarm:
            logger.warning("%s", alarm)

        logger.info("Simulation stopping...")

    # ------------------------------------------------------------------
    # Agent selection (weighted random)
    # ------------------------------------------------------------------

    def _owes_reply(self, agent: Agent) -> bool:
        """True if the agent has an active thread with a new reply from the other
        party that it hasn't answered yet.

        Historical note (Task 11 — two-lane scheduler): this used to be the
        scheduler-visible signal driving the post lane's reactive-priority
        tier, but that tier is gone — replies leave the paced pool entirely
        (see `_dispatch_reply_lane` / `_pending_reply_pairs`, which are
        deliberately ungated and carry no such distinction). This method is
        kept for the GATED cohort-gate distinction it still encodes, which
        nothing else in the engine computes:

        Two cohort rules apply here and nowhere else (v2 §8):

        - **Grandfathered threads are skipped.** A thread whose partner has left the
          cohort still gets answered by Phase 4 so it can conclude, but this method
          reports it as no longer "owed" in the gated sense.
        - **The remaining threads are read through the agent's gate.** An
          untagged thread with fewer than 2 posters is still open
          (``get_thread_allowed_agents`` returns None) — so a non-cohort third
          party posting into an otherwise legal thread would otherwise
          manufacture a false positive for a sender the agent is not supposed
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
        - not already ``in_flight``: a post-lane turn for this agent is
          currently running. A no-op today (the post lane is strictly
          sequential — the previous turn always finishes before the next
          selection), but load-bearing once loop iterations can overlap.
        """
        if agent.state.in_flight:
            return False
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
        """Select the next agent for a post-lane turn (sequential — one at a time).

        Staleness-weighted random, scaled by load:
        P(agent) ∝ (now - last_selected) * _agent_load(agent), with a penalty
        for agents that have repeatedly skipped Phase 5
        (weight /= 2^(skips-2) once skips >= 3). The load factor is what makes
        a star's hub — one endpoint of every conversation — draw a share that
        tracks the edges it actually sits on, instead of the 1/N a uniform
        weighting gave it. See design §4.3.

        There is no reactive tier here any more: replies leave the paced pool
        entirely (see `_dispatch_reply_lane`), so this is pure proactive
        selection over the eligibility pool (`_turn_eligible`) — budget, the
        sliding-window rate limit, the per-agent `turn_delay_seconds`
        cooldown, and not already `in_flight`.
        """
        now = time.time()
        candidates = [a for a in self.agents.values() if self._turn_eligible(a, now)]
        if not candidates:
            return None

        weights = []
        for a in candidates:
            w = max(now - a.state.last_selected, 1.0) * self._agent_load(a)
            skips = a.state.consecutive_phase5_skips
            if skips >= 3:
                w /= 2 ** (skips - 2)
            weights.append(w)
        return random.choices(candidates, weights=weights, k=1)[0]

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
        v2 §8 and `_owes_reply`, which stays gated for the callers that still
        care about that distinction (the cohort tests).

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

        Fix round 1 (task review, Important I4): guards against a thread that
        closed *mid-sweep* — `pairs` is snapshotted once by
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
        replying with posting is exactly the cross-lane coupling Task 10
        removed. Does NOT touch ``consecutive_phase5_skips`` either (fix
        round 2, Ruling R10): the reply lane briefly owned a once-per-tick
        reset here (fix round 1, I3), but the reset is idempotent, so
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
        ``reply_lane_max_in_flight`` (Task 13). Default is 1, which keeps
        this behaviourally sequential — one pair fully serviced (thread lock
        released, semaphore released) before the next one's own lock/
        semaphore acquisition can succeed. Task 14 raises the default once
        the adversarial concurrency tests exist.

        Phase 3 (thread activation) runs first for every agent, each guarded
        by its own try/except (fix round 2 — see below): nothing else calls
        it now that `_run_post_turn` is Phase 1 + 5 only, so without this a
        brand-new @-mention or reply-to-a-post would never open a thread at
        all.

        Cursor advancement mirrors Task 6's snapshot-then-assign idempotent
        pattern, applied per agent: `_phase3_activate_threads` and
        `_pending_reply_pairs`'s `has_new_reply_from_other` check both read
        `last_seen_cursor` to bound their "since cursor" scans (linear scans
        over the whole log — see message_log.py), and nothing else advances
        it for an agent the post lane does not happen to pick this tick.
        Without this, such an agent would rescan the entire message log from
        turn zero on every single main-loop tick, forever. The snapshot is
        taken BEFORE Phase 3 runs and only assigned AFTER `_pending_reply_
        pairs` has read the (still old) cursor — advancing early would hide
        the very replies this pass exists to find, exactly as it would in
        `_run_post_turn`. Nothing in `_run_post_turn` writes this cursor any
        more (Fix round 1, Critical C1) — this function is its sole owner.

        Fix round 1 (task review):
        - **C2**: every pair's `has_pending_reply` is promoted for the WHOLE
          batch, immediately after `_pending_reply_pairs()` returns and
          before the cursor advances or any servicing `await` runs. A pair
          found only via `has_new` (not yet durable) whose SIBLING later in
          this same sequential loop raises must still carry a retry signal —
          the cursor is about to move past whatever made it "new", and
          `has_pending_reply` is the only thing that survives that (see
          `_pending_reply_pairs`'s docstring). Each pair is then serviced
          inside its own try/except so one failing reply can never abort the
          rest of the sweep (spec §8) or propagate out of this call.
        - **I5**: checks `self._running` so a shutdown request is honoured
          within roughly one reply's worth of latency instead of first
          draining the whole sweep — worst case today is ~12 sequential Opus
          calls in a 12-interview star, well past the documented `docker stop
          -t 30` grace period.

        Fix round 2 (task review):
        - **Ruling R10**: this function no longer touches
          `consecutive_phase5_skips` at all. Fix round 1 moved the reset here
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
        - **New Important, prologue failures**: the blanket try/except that
          used to wrap the ENTIRE call to this function (in `_run_main_loop`)
          is gone — it swallowed a deterministic failure in
          `_pending_reply_pairs` or the cursor-advance step just as silently
          and permanently as the two Criticals this same review round fixed
          (one ERROR per tick, forever, no interview ever progressing, while
          the post lane kept posting as if nothing were wrong). What's left
          guarded, narrowly, is `_phase3_activate_threads`: it runs once per
          agent, so one agent's activation bug must not stop every OTHER
          agent's from running too. `_pending_reply_pairs()` itself, and
          anything below it, is now genuinely unguarded — a bug there
          surfaces (crashes the run) rather than being swallowed wholesale.

        Final review fix: catching that per-agent Phase 3 failure is not
        enough on its own — the cursor-advance loop below used to run
        unconditionally for every agent, so a caught-and-logged exception
        for agent X still marked X's own unprocessed messages "seen" for
        good, the exact permanent, silent loss this whole guard exists to
        avoid. `failed_agent_ids` is collected in the Phase 3 loop and
        consulted in the advance loop so a failed agent's cursor holds where
        it was; its unactivated messages are retried on the next dispatch
        rather than lost.

        Fix round 3 (task review, Critical):
        - **I5 regressed by the concurrent rewrite**: `_run` originally
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
        # Final review fix: an agent whose Phase 3 pass raises must NOT have
        # its cursor advanced below — that would mark this exact agent's
        # unprocessed messages "seen" on the strength of a pass that never
        # actually ran, a permanent silent loss of the same shape C2 was
        # added to prevent (see test_dispatch_isolates_one_agents_phase3_
        # failure_from_the_others above, which only pins that OTHER agents
        # keep working — it says nothing about the failed agent's own
        # cursor). Collected here, consulted in the advance loop below.
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
        # and before any servicing await runs. See "C2" above.
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

        Fix round 1 (task review, Critical C1): this function does NOT touch
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
        settings = get_settings()
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
            since_last_action = time.time() - agent.state.last_phase5_action_time
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
            new_posts = self.message_log.get_new_top_level_posts(
                since=cursor,
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
                    logger.info(
                        "[%s] Phase 3: Auto-activated interview thread %s (lab post by %s)",
                        agent.agent_id, thread_id, other_id,
                    )

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

        settings = get_settings()

        # Get thread history from message log
        history_entries = self.message_log.get_thread_history(thread.thread_id)
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
        # reachable at all — exactly the failure mode this fix round removed
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
            self._allowance_for(agent), get_settings().llm_rate_window_seconds
        ):
            logger.warning(
                "[%s] rate-limited; deferring this reply", agent.agent_id,
            )
            return
        # already_reserved=True: try_reserve just appended this exact call to
        # call_times — appending again here would double-book it (Ruling R5).
        agent.record_api_call(already_reserved=True)
        # Filled by `on_stop_reason` below, then read once the reply is
        # extracted. A list rather than a scalar for the same reason
        # src/agent/tools.py uses one: the callback is a plain `append`, so the
        # collection needs no closure and no nonlocal.
        stop_reasons: list[str] = []
        try:
            raw_response = await generate_with_tools(
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
                # looking complete") and sits at 2500; when Option A moved the
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
                # engine (measured max 134s: up to max_tool_rounds real API
                # calls), so without this a `docker stop` expired mid-turn and
                # SIGKILLed before the flush, losing the in-flight turn's
                # buffered log rows. Polling the flag here lets a stopping turn
                # finish the round it already started and skip the rest.
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
        still a valid ThreadDecision.outcome value for legacy rows and is
        still handled by _close_thread/admin routes/ProposalReview, but
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
        `active_threads` (this ruling — Task 12 review Finding B / Ruling
        R11 — is one of the two motivating cases for `_agent_locks` at all,
        alongside `_evict_dead_thread` below) is additionally guarded by the
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

    async def _evict_dead_thread(self, thread_id: str) -> None:
        """Remove a thread_id from every agent's in-memory state.

        Fires when Slack reports the parent message no longer exists (via
        ThreadNotFound from conversations.replies or a silent thread_ts drop
        on chat.postMessage). Without eviction the same dead thread gets
        re-polled and replied-to forever, producing noisy error logs and —
        worse — cascading top-level posts.

        Called from `_post_message`'s ThreadNotFound handling, itself called
        from `_reply_to_thread` — i.e. from inside the reply lane's THREAD
        lock for this exact `thread_id` (see `_dispatch_reply_lane`). This
        loops over EVERY agent's `active_threads`, so — Task 12 review
        Finding B / Ruling R11 — it needs the AGENT lock for every agent, not
        just the two `_close_thread` above locks: without it, `_close_thread`
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
                removed = False
                if thread_id in ag.state.active_threads:
                    ag.state.active_threads.pop(thread_id, None)
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
            # Eviction removes per-agent state but must NEVER un-close a thread.
            # If another caller is racing a _close_thread add() against this eviction,
            # the discard would remove the closed marker, and Phase 3 would re-activate
            # the finished interview. _closed_thread_ids is insert-only.
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
          (resolved from ``private_channel_members``), so Phase 4/5 can act
          in it.
        - Seeds a poll cursor so the first poll picks up the handover message.

        Cheap to call every main-loop tick — a single query returning a handful
        of rows. Idempotent: channels already known are skipped.

        RUN-SCOPED, and that filter is load-bearing rather than tidy. It used to
        be absent while the sibling ``AgentChannel`` read in
        ``_persist_seeded_channels`` had one, so this query returned EVERY run's
        private channels. That was survivable only because ``--fresh`` truncated
        ``agent_channels`` outright; now that it deletes nothing
        (``main._open_fresh_run``), an unfiltered select would hand a brand-new
        run every previous run's private channels, write them into
        ``_channel_id_map``/``_channel_visibility``, join its bots to them, and —
        because ``_seed_slack_cursors_without_ingest`` and
        ``_poll_slack_for_bot_messages`` both poll whatever is in those maps —
        re-ingest their entire Slack back catalogue into this run.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            from sqlalchemy import select as sa_select

            from src.models import AgentChannel, PrivateChannelMember

            async with self.session_factory() as db:
                priv_rows = (await db.execute(
                    sa_select(AgentChannel).where(
                        AgentChannel.simulation_run_id == self.simulation_run_id,
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
          ~2 months, burying a fresh handover under a huge stale-message backlog).
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
            if is_panel_note(entry):
                # A panel note is not traffic to catch up on. Counted here it
                # would do both halves of the wrong thing at once: make the hub
                # look "caught up" on a channel it has not answered in, and
                # make the note itself an unacted message the OTHER member bot
                # rewinds its cursor to go and read.
                continue
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
            settings = get_settings()

            # Stamp the spontaneous-post timer up front: consulting Phase 5 consumes
            # the opportunity regardless of whether we end up posting, skipping, or
            # bailing out early. Without this, a "skip" leaves the timer stale and
            # every subsequent turn re-fires Phase 5, burning an LLM call per turn.
            agent.state.last_phase5_action_time = time.time()

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
                self._allowance_for(agent), get_settings().llm_rate_window_seconds
            ):
                logger.warning(
                    "[%s] rate-limited; deferring this post", agent.agent_id,
                )
                return
            # already_reserved=True: try_reserve just appended this exact call to
            # call_times — appending again here would double-book it (Ruling R5).
            agent.record_api_call(already_reserved=True)
            # See `_was_truncated`; same collection idiom as the Phase-4 site.
            stop_reasons: list[str] = []
            try:
                response = await generate_agent_response(
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
                # the just-reset 0, to feed _select_next_agent's damping
                # (`skips >= 3`). Every remaining rejection path (unsupported
                # action, post-type rejection, body-mention rejection) restores it
                # correctly via `previous_skips + 1`.
                previous_skips = agent.state.consecutive_phase5_skips
                agent.state.consecutive_phase5_skips = 0
                agent.state.last_phase5_action_time = time.time()

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
                    # be a phantom record with no corresponding Slack message
                    # (Task 11 fix round 1, Finding 3).
                    logger.info(
                        "[%s] Phase 5: New post in #%s suppressed — not counted, "
                        "nothing persisted",
                        agent.agent_id, channel,
                    )
                else:
                    agent.message_count += 1

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
                held = await self._persist_assessment(
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
                    # Announce only a verdict that ends the interview. Since a
                    # provisional sidecar is now STORED rather than refused, a
                    # single interview can hold several in turn — and a headline
                    # is a public Slack post that cannot be retracted when the
                    # row it described is superseded moments later. Design D14
                    # says a verdict that is not held never posts; the same logic
                    # says a verdict that is not final does not post YET.
                    if announce:
                        await self._post_assessment_summary(
                            agent, thread, verdict, slack_ts,
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
                    # actually HELD — never leave the interview with neither.
                    if superseded is not None:
                        await self._retire_superseded_verdict(
                            agent.agent_id, thread, superseded,
                            replacement_ordinal=thread.message_count + 1,
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
    ) -> None:
        """Post a headline-only summary of a concluded interview to the
        assessments-summary channel (design D12/D13/D14/D16). Called from
        _capture_hub_assessment right after a verdict is HELD — covers both
        the immediate fail (closes_thread) path and the pass path
        symmetrically, since both funnel through that one call site.

        Deliberately duplicates two PURE function calls
        (rubric_weighted_score/rubric_band) rather than changing
        _persist_assessment's return signature to hand back its computed
        values — that would risk breaking existing direct unit-test callers
        of _persist_assessment that assert a plain bool return. The
        weighting LOGIC itself is not duplicated, only these two calls.

        Never raises: a Slack failure here must not affect anything the
        caller already did (the assessment row's persistence, or the reply
        already posted to Slack). Two levels of that, deliberately: the
        permalink lookup has its own inner guard so a link failure DEGRADES
        (design D16 — "(link unavailable)", never a dropped post), while the
        outer one is the last resort for the post itself.

        Only the headline's five fields are ever rendered — PI/lab name,
        project, recommendation, band/score, permalink (design D12). The
        verdict's `rationale`, `red_flags`, `gating` and `raw_verdict` are
        never read here at all, which is what keeps this post from saying more
        than the manager read-only detail view already shows staff. Pinned by
        `tests/unit/test_assessments_summary_post.py`'s sentinel test —
        widening this to interpolate `verdict` wholesale, or to add a
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
                return

            subject_agent_id = thread.other_agent_id
            pi = self.agents.get(subject_agent_id) if subject_agent_id else None
            pi_label = pi.pi_name if pi else (subject_agent_id or "Unknown lab")

            scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
            if scores:
                stage = verdict.get("funnel_stage")
                score = rubric_weighted_score(scores, stage)
                band = rubric_band(score, stage)
                score_part = f" (band: {band}, score: {score:.1f})"
            else:
                score_part = ""

            # Both fields come straight from the model and land in a PUBLIC
            # channel, so they get the same treatment `_persist_assessment`
            # gives them before they reach a bounded column: `_bounded_str`
            # drops a non-string (a model that answers `company_or_project`
            # with an object would otherwise have a Python `repr` posted to
            # Slack) and clips an over-long one, which is what keeps this a
            # HEADLINE (design D12) instead of an unbounded wall of model text
            # that `split_for_slack` would then cut into several messages.
            # `recommendation`'s 30 is its column's own width, so the post and
            # the stored row can never disagree about it; `company_or_project`
            # is a `Text` column with no width, so its 120 is this post's own
            # display bound — the full title is always in the row and on the
            # `/admin/assessments` detail page the permalink's reader can reach.
            project = _bounded_str(verdict.get("company_or_project"), 120) or "(untitled)"
            recommendation = _bounded_str(verdict.get("recommendation"), 30) or "unknown"

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
            link_part = f" — <{permalink}|View interview>" if permalink else " (link unavailable)"

            text = (
                f":mag: {pi_label} — {project} → *{recommendation}*{score_part}{link_part}"
            )
            await client.apost_message(ASSESSMENTS_SUMMARY_CHANNEL, text)
            logger.info(
                "[%s] Posted #assessments-summary headline for %s (%s)",
                agent.agent_id, subject_agent_id or "?", recommendation,
            )
        except Exception:
            logger.exception(
                "[%s] Failed to post assessments-summary headline for thread %s",
                agent.agent_id, thread.thread_id,
            )

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
    ) -> bool:
        """Store a scouting verdict. Best-effort: a failure here must never cost
        the Slack post that already went out.

        Returns whether the verdict is HELD — committed, or queued on
        ``_pending_assessments`` for a retry that will still land it. False means
        nothing was stored and nothing will be: the engine has no database (see
        ``__init__``). ``_capture_hub_assessment`` uses this to decide whether the
        thread has had its one verdict; a queued row counts, because letting a
        second verdict through while the first is still queued lands BOTH.

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
        # this class (e.g. _check_thread_outcome above, :1520 in the class).
        if not self.session_factory or not self.simulation_run_id:
            logger.debug(
                "[%s] Skipping assessment persistence — no database configured",
                agent_id,
            )
            return False

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
        # subject_agent_id/funnel_stage/recommendation/confidence are bounded
        # VARCHAR columns (see src/models/opportunity.py); every other field
        # degrades per-field on a bad value (wrong type -> None), but a
        # too-long *string* in one of these four is still the right type and
        # would sail past an isinstance check straight into a DataError at
        # commit — which the outer except then drops the WHOLE row for. Clip
        # instead of dropping: a truncated recommendation is still useful for
        # triage, an absent one is not (Task 11 fix round 1, Finding 5).
        subject_agent_id = _bounded_str(subject_view.get("subject_agent_id"), 50)
        funnel_stage = _bounded_str(verdict.get("funnel_stage"), 20)
        recommendation = _bounded_str(verdict.get("recommendation"), 30)
        confidence = _bounded_str(verdict.get("confidence"), 20)
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
            #   NULL    the panel was VERIFIED complete (or none was owed at all)
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
        )
        try:
            async with self.session_factory() as db:
                db.add(OpportunityAssessment(**assessment_kwargs))
                await db.commit()
            logger.info(
                "[%s] Assessment stored: %s -> %s (%s, %s)",
                agent_id, subject_agent_id or "?",
                recommendation or "?", computed_score, computed_band,
            )
            return True
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
            return True

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

        ``concluding`` still matters, just not for admission: the caller uses it
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
        *, replacement_ordinal: int,
    ) -> None:
        """Remove the provisional verdict a later concluding reply just replaced.

        Last-write-wins needs both halves: without this, the later verdict lands
        and the earlier one STAYS, which is precisely the duplication production
        showed (three rows, 2.51/2.66/2.69, for one pearce interview). The
        interview keeps exactly one row, and the one it keeps is the
        better-informed one.

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

            async with self.session_factory() as db:
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
                "[%s] Failed to remove the superseded assessment row for thread "
                "%s (slack_ts=%s): %s — the interview now has TWO assessments, "
                "the drop row above says which is which",
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

        ``None`` when there is nothing to read — no ``slack_ts`` to find the row
        by, no database, no matching row, or a failed SELECT. Never raises: the
        concluding reply is already in Slack, and a lookup that cannot answer
        must cost the copy, not the supersession.

        Those four cases all produce a drop row with ``raw_verdict IS NULL``, so
        the LOG is the only thing that can tell them apart — and on the one path
        whose whole purpose is "never lose the retired verdict", "there was no
        row to copy" must not be indistinguishable from "the copy was never
        attempted". The not-found branch therefore warns explicitly; the two
        early returns are ordinary, expected states with their own callers'
        logging (the no-``slack_ts`` case is already reported loudly by the
        caller, and a DB-less engine is a documented silent no-op everywhere).
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
        * ``announced=False``. ``True`` would suppress the
          ``#assessments-summary`` headline for a verdict stored provisionally
          before the restart — a silent D12 breach. ``False`` merely preserves
          the behaviour of a process that never knew about the row at all.
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
        for thread_id, slack_ts in rows:
            self._assessed_threads[thread_id] = _HeldVerdict(
                ordinal=0,
                final=thread_id in self._closed_thread_ids,
                slack_ts=slack_ts,
                announced=False,
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

    async def _record_specialist_consult(
        self,
        agent_id: str,
        *,
        subject_agent_id: str | None,
        thread_id: str | None,
        channel_name: str | None,
        domain: str,
        question: str,
        context_excerpt: str | None,
        verdict_signal: str,
        confidence: str,
        concerns: list | None,
        questions_to_ask: list | None,
        raw_opinion: str,
        truncated: bool | None = None,
    ) -> None:
        """Write one successful consult to ``specialist_consults``.

        Best-effort in exactly the same sense as ``_record_assessment_drop``:
        never raises, a DB-less engine is a silent no-op, and a failure is an
        ERROR with a traceback and nothing else. The consult itself has already
        happened and has already been credited to the floor in memory
        (``_note_consult``, fired first by ``_execute_consult_specialist``) —
        losing this row costs visibility and post-restart verifiability, never
        the opinion the hub is about to act on.

        Called from the ``on_consult_record`` closure in ``_reply_to_thread``,
        which is fired on a WIDER path than ``on_consult``: a refused domain, a
        missing persona file, a failed call or an empty reply all write nothing,
        but a reply the API cut off mid-sentence DOES write a row (it is the only
        evidence the attempt happened) while not counting toward the floor. So
        "a row here means the domain counts as consulted" holds only for
        ``truncated`` in ``(False, None)`` — the qualification
        ``src/models/specialist_consult.py`` states and the one a reader of these
        rows as evidence of a convened panel (``_seed_consults_from_db``) has to
        apply.

        ``truncated`` defaults to ``None`` — "not stated" — so a caller written
        before the column existed keeps its old meaning rather than asserting
        completeness it never checked. ``src/agent/tools.py`` always sends a
        real boolean.

        Awaited inline by the tool call rather than dispatched as a background
        task: an orphaned task would outlive the turn, and the engine's
        shutdown path flushes its own buffers only — a `docker stop` landing
        between the consult and the write would lose the row it was created to
        keep.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                db.add(SpecialistConsult(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    subject_agent_id=(subject_agent_id or None),
                    thread_id=(thread_id or None),
                    channel_name=(channel_name or None),
                    domain=domain,
                    question=question,
                    context_excerpt=context_excerpt,
                    verdict_signal=verdict_signal,
                    confidence=confidence,
                    concerns=concerns,
                    questions_to_ask=questions_to_ask,
                    raw_opinion=raw_opinion,
                    truncated=truncated,
                ))
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — a record must not cost the opinion
            logger.error(
                "[%s] Failed to record the %s consult for %r (thread %s): %s — "
                "the opinion still stands and still counts for the floor "
                "in-process, but this run's panel is no longer reconstructable "
                "after a restart",
                agent_id, domain, subject_agent_id or "?", thread_id or "?", exc,
                exc_info=True,
            )

    async def _post_panel_note(
        self,
        agent_id: str,
        *,
        channel: str | None,
        thread_ts: str | None,
        domain: str,
        question: str,
        verdict_signal: str,
        truncated: bool | None = None,
        **_withheld,
    ) -> None:
        """Post the one-line, signal-level trace of a successful consult into
        the interview thread.

        Why at all: the evaluation panel was previously invisible in Slack. A
        human watching an interview saw the hub go quiet for 30-40 seconds per
        consult and then produce a verdict shaped by opinions nobody in the
        workspace could see. The note makes the panel legible AT THE MOMENT it
        is engaged — posted from inside the turn's tool rounds, so it lands
        before the hub's eventual reply and the thread reads in the order things
        actually happened.

        Why so thin: an interview thread is visible to every lab in the
        workspace. A specialist's opinion paraphrases the PI's confidential
        statements back at them and quotes Blackbird's internal rubric, so
        ``concerns``, ``questions_to_ask``, ``confidence`` and the opinion body
        are NOT published. ``**_withheld`` is where they land — named for what
        it does, and load-bearing in two directions: it lets this be called
        with the same ``**fields`` the durable writer takes (one closure, one
        contract), and it means a field added to that contract later is
        withheld by DEFAULT rather than leaking the first time someone forgets.
        ``format_panel_note`` then takes only the three publishable values, so
        there is no parameter through which the rest could reach Slack. That
        three-argument signature IS the enforcement and is deliberately not
        widened.

        ``truncated`` is the one field pulled back out of ``**_withheld``, and
        for the opposite reason to the rest: it is not withheld from the note,
        it CANCELS the note. A consult the API cut off mid-sentence parsed to
        nothing, so ``verdict_signal`` is the schema's DEFAULT ``caution`` and
        no specialist ever said it — and this note goes into the PI's own
        interview thread, which every lab in the workspace can read. Publishing
        a parse failure as `` caution`` states a panel opinion that does not
        exist, and ``src/agent/tools.py`` has already refused to credit that
        domain to the floor for exactly this reason; the note must agree with
        the floor. It was absorbed silently by ``**_withheld`` until 2026-08-22,
        which is why it is spelled out as a parameter rather than read out of
        the catch-all: a named parameter is visible in the signature, a dict key
        is not.

        The DURABLE row is still written either way — it is the only evidence
        the attempt happened, and it now carries ``truncated=True`` so the floor
        keeps refusing it across a restart. Only the workspace-visible claim is
        skipped.

        Best-effort, in exactly the sense ``_record_specialist_consult`` is:
        never raises, so it cannot cost the consult, the turn or the reply. It
        runs SECOND, after the durable record — if only one of the two can
        happen it must be the artifact a verdict is audited against, not the
        courtesy note.

        `phase=PHASE_PANEL_NOTE` is the whole reason no prompt file had to
        change: the row exists, it is in the thread, and every agent-facing
        read of the message log skips it (see src/agent/message_log.py). The
        flag is read HERE rather than cached at startup so an operator can
        disable notes with a `.env` edit + container recreate and no rebuild.
        """
        if not channel:
            # No channel, nowhere to post. A consult made outside a thread
            # (a direct tool call, a test) has no interview to annotate.
            return
        if truncated:
            # See the docstring: the signal on a truncated consult is the
            # schema default, not an opinion. Logged rather than silent — a note
            # that does not appear is otherwise indistinguishable from
            # `panel_notes_in_thread=false`.
            logger.info(
                "[%s] Panel note skipped for the %s consult (thread %s): the "
                "opinion was truncated, so its signal is a parse default and "
                "not something a specialist said. The durable row still stands "
                "and the domain still does not count toward the floor.",
                agent_id, domain, thread_ts or "?",
            )
            return
        try:
            if not get_settings().panel_notes_in_thread:
                return
            await self._post_message(
                agent_id,
                channel,
                format_panel_note(
                    domain=domain,
                    verdict_signal=verdict_signal,
                    question=question,
                ),
                thread_ts=thread_ts,
                phase=PHASE_PANEL_NOTE,
            )
        except Exception as exc:  # noqa: BLE001 — a note must not cost the opinion
            logger.error(
                "[%s] Failed to post the %s panel note to #%s (thread %s): %s — "
                "the consult itself stands, is recorded, and still counts for "
                "the floor; only the in-thread trace of it is missing",
                agent_id, domain, channel, thread_ts or "?", exc, exc_info=True,
            )

    async def _seed_consults_from_db(
        self, verdict: dict, thread: ThreadState | None,
    ) -> None:
        """Rehydrate this interview's consult record from ``specialist_consults``
        when memory holds nothing for it.

        The floor's in-memory map dies with the process, so before this every
        verdict written after a restart was UNVERIFIABLE — stored with
        ``missing_domains=[]`` no matter how thorough the panel had been (see
        ``_floor_verifiable``). Production's normal exit is a SIGKILL, so that
        was the ordinary case, not a corner one. The table now outlives the
        process, so the record can be read back.

        Deliberately ADDITIVE and narrow:

        * Only when ``self._consulted_domains(subject, thread)`` is EMPTY. A
          process that recorded anything for this interview stays authoritative
          for it — memory is written on the success path itself and cannot be
          behind a committed row that path also wrote.
        * Only for a verdict that owes a panel at all, asked through
          ``panel_is_owed`` — the SAME question ``_specialist_floor_gap`` asks,
          on the same two inputs (the model's recommendation and the COMPUTED
          band). This used to test ``recommendation not in
          _PANEL_REQUIRED_FOR``, the recommendation-only rule the floor
          abandoned, so it skipped rehydration for exactly the verdicts the new
          floor holds to the panel: a verdict written ``pass`` that scores into
          the ``conditional`` band is owed a panel, and the seed refused to look
          for its consults. Production stamped such a verdict
          ``panel_incomplete=true`` naming four domains, THREE of which were
          recorded as consulted on that very thread.
        * Keyed on ``(run, subject, thread)``, the same triple the in-memory map
          uses. A different run's rows, or the same PI's OTHER interview, must
          not satisfy this interview's panel — the exact hole
          ``_specialist_floor_gap``'s docstring records.
        * TRUNCATED consults are excluded. A row exists for every consult that
          produced text, including one the API cut off mid-sentence — that row is
          the only evidence the attempt happened, and ``src/agent/tools.py``
          deliberately writes it while NOT crediting the domain in memory. Left
          in this SELECT it would undo that refusal on the next restart, turning
          an unread specialist into a consulted one. The filter is
          ``truncated IS NOT TRUE``, never ``= False``: NULL is a third state
          ("written before migration 0036"), and reading it as truncated would
          invalidate every pre-migration row's credit on no evidence at all.
          Rows written before 0036 therefore keep counting, which means three
          known-truncated production consults still credit the floor —
          unrecoverable from the table, and cheaper than the alternative.
          Subject to that, this can only ever turn "we have no record" into
          "here is the record".

        Arming the floor off these rows does not weaken
        ``ThreadState.floor_armed``'s latch. The latch exists to stop a
        DIFFERENT interview's consult, landing mid-await in another task, from
        retroactively arming this verdict; these rows are this interview's own,
        already committed before this turn began, and the seed only ever moves
        the latch False -> True. After the seed the global map really is
        non-empty, which is precisely what ``floor_armed`` asserts.

        Never raises: this runs inside ``_persist_assessment``, ahead of the
        write, and a failed SELECT must cost the fallback, not the verdict.
        """
        if not panel_is_owed(
            verdict.get("recommendation"), self._computed_score_and_band(verdict)[1]
        ):
            return
        subject = verdict.get("subject_agent_id")
        if not isinstance(subject, str) or not subject:
            return
        if not self.session_factory or not self.simulation_run_id:
            return
        thread_id = thread.thread_id if thread is not None else None
        if self._consulted_domains(subject, thread_id):
            return
        from sqlalchemy import select as sa_select

        try:
            async with self.session_factory() as db:
                domains = (await db.execute(
                    sa_select(SpecialistConsult.domain).where(
                        SpecialistConsult.simulation_run_id == self.simulation_run_id,
                        SpecialistConsult.subject_agent_id == subject,
                        SpecialistConsult.thread_id == thread_id,
                        # IS NOT TRUE, not `== False`: NULL is "written before
                        # 0036", and those rows must keep crediting the floor
                        # exactly as they do today. See the docstring.
                        SpecialistConsult.truncated.is_not(True),
                    )
                )).scalars().all()
        except Exception as exc:  # noqa: BLE001 — never lose a verdict over a lookup
            logger.error(
                "Failed to read back the consult record for %r (thread %s): %s "
                "— this verdict's panel will be stored as UNVERIFIED",
                subject, thread_id or "?", exc, exc_info=True,
            )
            return
        if not domains:
            return
        self._specialist_consults.setdefault((subject, thread_id), set()).update(domains)
        if thread is not None:
            # The latch's own rule (`floor_armed or bool(_specialist_consults)`)
            # re-applied to the map as the seed above just left it — not a live
            # read of anything another task may be doing. See the docstring.
            thread.floor_armed = True
        logger.info(
            "[specialists] floor rehydrated %d recorded consult(s) for %r "
            "(thread %s) from specialist_consults — this verdict is checkable "
            "even though the map was empty (restarted mid-interview?)",
            len(set(domains)), subject, thread_id or "?",
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
            cached = load_role(role).post_types
            self._role_post_types_cache[role] = cached
        return cached

    def _record_consult(
        self, pi_agent_id: str, domain: str, thread_id: str | None = None,
    ) -> None:
        """Note a successful consult, keyed on the interview it happened in.

        Keyed on ``(pi, thread)`` rather than the PI alone. One PI's consults
        are NOT cumulative across interviews: a second interview is a second
        idea and owes its own panel. ``thread_id`` is None for direct callers
        that have no interview to name.
        """
        if not pi_agent_id:
            return
        self._specialist_consults.setdefault((pi_agent_id, thread_id), set()).add(domain)

    def _note_consult(
        self, pi_agent_id: str, domain: str, signal: str, thread_id: str | None = None,
    ) -> None:
        """Record a consult AND tally its signal.

        Two concerns, deliberately kept apart: `_record_consult` answers "does
        the floor consider this domain covered", which is per-interview; the
        tally answers "is this panel discriminating at all", which is per-run.
        """
        self._record_consult(pi_agent_id, domain, thread_id)
        self._consult_signal_counts[signal] = (
            self._consult_signal_counts.get(signal, 0) + 1
        )

    def _consulted_domains(
        self, pi_agent_id: str, thread_id: str | None = None,
    ) -> frozenset[str]:
        """Domains consulted about this PI in this interview; empty for an
        interview we have no record of."""
        return frozenset(self._specialist_consults.get((pi_agent_id, thread_id), ()))

    # `_PANEL_REQUIRED_FOR` used to alias `specialists.PANEL_REQUIRED_FOR` here,
    # for call sites testing `recommendation in _PANEL_REQUIRED_FOR` directly.
    # All of them now ask `panel_is_owed`, which weighs the COMPUTED band as
    # well — the set alone answers a question this class no longer asks, and
    # leaving the alias in place would let a future call site quietly re-adopt
    # the abandoned rule. Removed 2026-08-22 with the last such site
    # (`_seed_consults_from_db`).

    @staticmethod
    def _computed_score_and_band(verdict: dict) -> tuple[float | None, str | None]:
        """The weighted score and band this verdict's own scores imply.

        One definition, because two callers now need it and they must agree: the
        row writer (`_persist_assessment`) and the specialist floor, which gates
        on the COMPUTED band as well as the model's written recommendation.

        An empty/missing ``scores`` map is "we don't know", not "we scored it a
        0.00 pass" — ``rubric_weighted_score({})`` returns 0.0 and that bands as
        ``pass``, a real and decisive decline the model never made. Both columns
        are nullable for exactly this case; leave them unset rather than record a
        verdict nobody rendered.

        Stage-aware since rubric v2.0.0: an incubation-stage verdict is scored on
        the incubation weights and banded on the incubation lines, every other
        stage (and a missing one) on the investment scale. The RAW ``funnel_stage``
        goes in — normalizing it is ``blackbird_rubric``'s job. Both calls take
        the same stage: a score computed on one scale and banded against the
        other's lines is meaningless.
        """
        scores = verdict.get("scores") if isinstance(verdict.get("scores"), dict) else {}
        if not scores:
            return None, None
        stage = verdict.get("funnel_stage")
        score = rubric_weighted_score(scores, stage)
        return score, rubric_band(score, stage)

    def _specialist_floor_gap(
        self, verdict: dict, *, thread: ThreadState | None = None,
    ) -> set[str]:
        """Domains this verdict was obliged to consult but did not.

        Empty means the verdict may be persisted. Whether a panel is owed at all
        is ``specialists.panel_is_owed``'s question, not this method's, and it
        weighs BOTH the model's written recommendation and the COMPUTED band:
        either can pull a verdict into the panel and neither can pull it out,
        and anything unreadable fails CLOSED.

        This docstring used to state the abandoned rule — "only ``advance`` and
        ``conditional`` are held to the panel: a ``pass`` costs Blackbird
        nothing". Two things were wrong with it. A verdict that SCORES into
        advance/conditional owed a panel however the hub chose to label it (3 of
        the 4 conditional bands in the stored corpus are written ``pass``), and
        ``route-to-incubation`` — the incubation grant Blackbird exists to award,
        and the one recommendation that commits real money — was exempted as
        though it were a decline. It is the last verdict that should go
        unreviewed.

        ``thread``, when given, supplies ``floor_armed`` — whether
        ``_specialist_consults`` has been seen non-empty at any point in this
        thread's life, latched once per turn at the top of
        ``_reply_to_thread`` (see that latch's comment, and
        ``ThreadState.floor_armed``'s own comment, for the full history: a
        plain live read here was the original concurrency bug, and freezing
        the value forever at activation was a second bug fixed in a later
        round). It is consulted INSTEAD OF a live global map-emptiness check
        at persist time, because persist happens even later in the same turn
        as the latch, after this turn's own tool calls and after any `await` —
        long enough for a DIFFERENT interview's consult, landing in another
        task, to have changed the live map. ``thread=None`` (every direct
        caller with no thread to offer, and all pre-existing tests) falls back
        to a live global read, matching this method's behavior before
        ``floor_armed`` existed at all.

        The record is keyed on ``(subject, thread)``, not on the PI alone. An
        earlier version keyed on the PI (``subject_agent_id``) only, back when
        the artifact was a standalone Phase-5 post with no interview thread of
        its own, and that keying survived Option A's move into the Phase-4
        CONCLUDE reply even though a real thread existed at persist time by
        then — one PI's specialist consults were treated as cumulative across
        however many interview threads that PI had open. That let a PI's
        SECOND interview inherit the FIRST interview's consults and never
        convene its own panel: ``huganir`` was assessed 4 times in one run and
        ``hart`` 4, and only the first of each ever faced a panel. Keying on
        the thread as well as the PI gives each interview its own empty slot
        to start from. ``thread=None`` (every direct caller with no thread to
        offer, and all pre-existing tests) reads the ``None``-keyed slot for
        that PI — the same slot ``_record_consult`` writes to when it, too, is
        called with no ``thread_id``.

        FAILS OPEN in the two cases ``_floor_verifiable`` names, both of which
        mean "we have no record", never "the panel approved". An empty return
        is therefore ambiguous ON ITS OWN — "no gap" and "no way to tell" look
        identical here — which is why ``_persist_assessment`` asks
        ``_floor_verifiable`` as well and records the difference on the row
        (``missing_domains`` NULL vs ``[]``). Do not read an empty set as a
        clean bill of health without asking that question too.

        Note the second fail-open condition is about the whole map, not this
        PI's slot. An earlier version failed open whenever the SUBJECT had no
        consults, which quietly excused the commonest failure of all: a hub
        that simply never convenes a panel. If the map holds entries for other
        PIs, this process demonstrably records consults, so an absent PI means
        the panel really was skipped for them — and the floor bites.

        It does not fail open once any consult exists for that PI either: a hub
        that consulted one cheap specialist must not thereby buy an exemption
        from the rest.
        """
        # Gate on the COMPUTED band as well as the model's written
        # recommendation, and stop exempting `route-to-incubation`. Keying on
        # `recommendation` alone let a verdict that scores into `conditional`
        # exempt itself by writing `pass` — 3 of the 4 conditional bands in the
        # v2 corpus do exactly that — and it exempted Blackbird's own POSITIVE
        # outcome on the reasoning that "a decline costs Blackbird nothing".
        # `route-to-incubation` is the grant Blackbird exists to award; it is the
        # last verdict that should go unreviewed. See `panel_is_owed`.
        _, band = self._computed_score_and_band(verdict)
        if not panel_is_owed(verdict.get("recommendation"), band):
            return set()

        unverifiable = self._floor_unverifiable_reason(verdict, thread)
        if unverifiable is not None:
            # Both fail-open branches log the same way, at INFO, naming the
            # reason and the consequence — an operator reading this line needs
            # to know the row it produced says "unverified", not "clean".
            logger.info(
                "[specialists] floor fails open for subject %r: %s. The verdict "
                "is stored with missing_domains=[] — panel UNVERIFIED, which is "
                "not the same as verified complete (NULL).",
                verdict.get("subject_agent_id") or "?", unverifiable,
            )
            return set()

        # Guaranteed a non-empty str by the check above.
        subject = verdict.get("subject_agent_id")
        consulted = self._consulted_domains(
            subject, thread.thread_id if thread is not None else None
        )
        return set(required_domains_for(verdict, band=band) - consulted)

    def _floor_unverifiable_reason(
        self, verdict: dict, thread: ThreadState | None,
    ) -> str | None:
        """Why this verdict's panel cannot be checked at all, or ``None``.

        The single definition of ``_specialist_floor_gap``'s two fail-open
        conditions, so the gap computation and the "was this even checkable"
        question asked by ``_persist_assessment`` can never drift apart. The
        string is human-facing: it is logged, and it is the reason the row is
        written with ``missing_domains=[]``.
        """
        _, band = self._computed_score_and_band(verdict)
        if not panel_is_owed(verdict.get("recommendation"), band):
            # No panel was owed, so there is nothing to be unable to verify.
            return None
        subject = verdict.get("subject_agent_id")
        if not isinstance(subject, str) or not subject:
            return "it names no subject_agent_id, so there is no consult record to join to"
        armed = thread.floor_armed if thread is not None else bool(self._specialist_consults)
        if not armed:
            return (
                "this process has recorded no consult for ANY PI (restarted "
                "mid-interview?), so an absent record proves nothing"
            )
        return None

    def _floor_verifiable(
        self, verdict: dict, *, thread: ThreadState | None = None,
    ) -> bool:
        """Whether an empty ``_specialist_floor_gap`` means anything.

        ``_specialist_floor_gap`` returns an empty set both when the panel was
        genuinely complete and when there was no record to check it against —
        and the second case is the NORMAL state right after a restart, which
        production reaches by SIGKILL. Storing both as
        ``panel_incomplete=False, missing_domains=NULL`` counted every
        unverifiable verdict as a verified-complete panel and silently
        under-reported the one number the whole instrumentation exists to
        produce (spec §10's panel-gap surface).

        False here means "we could not check", never "the panel failed" — the
        row still stores ``panel_incomplete=False``, because we have no
        evidence of a gap either. It is recorded as the third state the column
        already anticipated: ``missing_domains=[]``.

        True for a verdict no panel was owed for (a ``pass``): nothing to
        verify is not the same as failing to verify.
        """
        return self._floor_unverifiable_reason(verdict, thread) is None

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

    async def _poll_slack_for_bot_messages(self) -> None:
        """Poll all channels for new bot-authored messages; mirror them into the log.

        Renamed from ``_poll_slack_for_human_messages`` (2026-08-12
        PI-interaction removal cycle): a human-authored channel message is no
        longer ingested via Slack at all — there is no PI-bot interaction
        surface left for it to feed (no reopen, no @-tag routing, no directive
        flag), so keeping a human branch here would only have grown the log
        with entries nothing downstream may act on. The remaining job is
        exactly what the name says: mirror another bot's Slack-native post (a
        message this process did not itself write) into the shared
        ``MessageLog``, recording the Slack-mirror mapping so a reply to it
        can still be threaded. See the removal cycle's PI-interaction audit
        map and ``MessageLog``'s GATED-method inventory (human rows are
        filtered there too, independent of this poller).
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
                messages = await client.apoll_channel_messages(ch_id, oldest=oldest)
                # `msg["thread_ts"]` arrives normalised: Slack sets thread_ts == ts on
                # a parent once it has replies, and the transport nulls that at ingest
                # (slack_client.normalize_inbound_message). Copying it verbatim, as
                # this loop used to, ingested a root as a reply to itself — and
                # get_new_top_level_posts skips anything with a non-null thread_ts, so
                # the post vanished from every reader of that method (e.g. the hub's
                # Phase 3 auto-activation scan) and _rebuild_state_from_db made it
                # permanent. The rule now lives in exactly one place.
                for msg in messages:
                    ts = msg.get("ts", "")
                    user_id = msg.get("user", "")
                    is_bot = bool(msg.get("bot_id") or msg.get("subtype") == "bot_message")

                    if not is_bot and user_id:
                        is_bot = await client.ais_bot_user(user_id)

                    # Bot messages are mirrored into the log so agents can scan
                    # them; a human message is dropped outright — advance the
                    # cursor past it (so it is not re-fetched every tick) but
                    # never append it. There is no PI-bot interaction surface
                    # left for a human channel post to feed.
                    if not is_bot:
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue

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
                        # mapping. Without it the entry looks DB-origin, and
                        # _slack_parent_ts then reports "no Slack root" for any
                        # thread rooted here — silently keeping every reply off
                        # Slack. The roots this branch ingests are another
                        # workspace bot's posts. Slack-origin ⇒ canonical id
                        # *is* the Slack ts, so the thread parent needs no
                        # translation.
                        slack_ts=ts or None,
                        slack_channel_id=ch_id,
                        slack_thread_ts=msg.get("thread_ts"),
                    )
                    if not self.message_log.get_entry(ts):
                        self.message_log.append(entry)
                    if ts:
                        self._poll_cursors[ch_id] = ts

            except Exception as exc:
                logger.debug("Polling error for #%s: %s", ch_name, exc)

    async def _poll_inbound_from_db(self) -> None:
        """Ingest messages written to the DB by other processes.

        The DB is the primary store, so any message this process hasn't seen —
        bot-authored handover posts written by the web app, and (later) the
        Slack mirror's inbound side, plus any human-authored row (today, only
        ``reopen_proposal``'s recorded guidance) — must be pulled into the
        live MessageLog. Bot-authored rows are the live path (design §8); a
        human-authored (``is_bot=False``) row is ingested too, but purely for
        history/observability (decision 5) — it can still be *read back* by
        the general-purpose GATED reads (``get_new_top_level_posts``/
        ``get_replies_to_agent_posts``/``get_tags_for_agent``), but
        ``has_new_reply_from_other`` filters ``is_bot=False`` unconditionally
        (so appending one here can never set a bot's ``has_pending_reply`` or
        make it a pending reply-lane pair), and ``_phase3_activate_threads`` filters
        ``is_bot`` before acting on any entry it reads (so it can never
        activate a new thread either). There is no PI-interaction handling
        left to route it into
        on top of that. Runs every tick regardless of Slack. See
        specs/local-db-conversations.md.
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
                # Another process's panel note stays a panel note here too. The
                # engine's own notes never reach this branch (the `get_entry`
                # dedup above catches them), but a second writer's would, and
                # ingesting one as an ordinary bot reply is precisely how a note
                # would become "an external bot message" this roster acts on.
                phase=r.phase,
            )
            self.message_log.append(entry)
            if r.is_bot:
                logger.info("External bot message in #%s: %.60s", entry.channel, entry.content[:60])
            else:
                logger.info(
                    "Human-origin DB message in #%s: %.60s (no action taken)",
                    entry.channel, entry.content[:60],
                )

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
        phase: str | None = None,
    ) -> str | None:
        """Post a message to Slack and record it in the message log + DB.

        ``phase`` overrides the KIND stamped on the resulting rows. None (every
        pre-existing caller) keeps the derived value ``_flush_persisted`` has
        always written — 'thread_reply' with a thread_ts, 'new_post' without —
        so this parameter changes nothing for a reply or a post. It is passed
        only by ``_post_panel_note``, as PHASE_PANEL_NOTE, which is what makes
        the resulting rows invisible to every agent-facing MessageLog read (see
        src/agent/message_log.py). Carried on the LogEntry, not applied at
        flush time, so it survives the round trip through the log and back out
        of the DB on the next rebuild.

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
        # counts the turn as published (message_count incremented) even
        # though nothing went out. Return before any of that — no Slack
        # call, no minted ts, no log entry.
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
            cleaned_text, _ = self._strip_disallowed_tags(text, agent)
            text = cleaned_text or text

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
                result = await client.apost_message(channel, text, thread_ts=slack_parent)
            except ThreadNotFound:
                # Parent was deleted. post_message already cleaned up the
                # orphan top-level post on Slack. Purge the dead thread_ts
                # from state so no one replies to it again. Keyed by the
                # canonical id, which is what the engine's state uses.
                if thread_ts:
                    await self._evict_dead_thread(thread_ts)
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
            # post, so the hub's Phase 3 auto-activation scan doesn't see N roots
            # where the author wrote one.
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
                # None for every caller but the panel note — see the docstring.
                # Stamped on EVERY chunk: a split message is several rows for
                # one logical post, and a continuation chunk that lost the
                # phase would be readable by agents while its head was not.
                phase=phase,
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

    def _ensure_assessments_summary_channel(self) -> None:
        """Create (or adopt) the hub's one-way assessments-summary channel
        and join only the hub to it — never added to SEEDED_CHANNELS, so it
        never enters Phase-1 discovery or the poller's scope (design D11).
        """
        hub = next(
            (a for a in self.agents.values() if a.role == "scout_hub"), None
        )
        if hub is None:
            return
        client = self.slack_clients.get(hub.agent_id)
        if not client or not client.is_connected:
            self._assessments_summary_channel_id = f"local:{ASSESSMENTS_SUMMARY_CHANNEL}"
            self._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = self._assessments_summary_channel_id
            return

        try:
            existing = client.list_channels()
        except SlackListingIncomplete:
            # Same caution as _ensure_seeded_channels: an incomplete listing
            # must not risk creating a duplicate channel.
            return

        ch_id = existing.get(ASSESSMENTS_SUMMARY_CHANNEL)
        if ch_id is None:
            ch_data = client.create_channel(ASSESSMENTS_SUMMARY_CHANNEL)
            ch_id = ch_data.get("id") if ch_data else None
        if not ch_id:
            return

        self._assessments_summary_channel_id = ch_id
        self._channel_id_map[ASSESSMENTS_SUMMARY_CHANNEL] = ch_id
        client.join_channel(ch_id)

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
                # As in _rebuild_state_from_db: a hydrated panel note must come
                # back as a panel note, not as a reply the reopened thread's
                # participants can suddenly read.
                phase=r.phase,
            ))

    async def _recover_rows_individually(
        self, rows: list, apply_one, *, what: str,
    ) -> tuple[int, list, list]:
        """Re-attempt a failed batch ONE ROW AT A TIME, isolating the poison row.

        Returns ``(written, lost, unattempted)``:

        - ``written``   — rows now durably in the database;
        - ``lost``      — rows that failed on their own, with nothing else to
          blame; retrying these forever would fail the whole batch forever, so
          the caller drops them (loudly) rather than re-queueing;
        - ``unattempted`` — rows the deadline (or a failure of the recovery pass
          itself) stopped us writing; the caller re-queues these.

        Two things here are not incidental:

        - **A NEW session.** In all three flushers the ``except`` sits OUTSIDE
          ``async with self.session_factory() as db:``, so by the time we get
          here that session is closed and rolled back. Reusing it would raise
          ``PendingRollbackError`` on the first row and lose the remainder — the
          exact loss this is supposed to prevent.
        - **A savepoint per row.** ``begin_nested`` means one row's failure rolls
          back to the savepoint instead of poisoning the whole transaction, so
          the rows after it still commit.

        Callers must gate entry on ``_ROW_LEVEL_DB_ERRORS`` — see that constant.
        """
        lost: list = []
        ok: list = []
        unattempted: list = []
        deadline = time.monotonic() + PER_ROW_RECOVERY_DEADLINE_S
        try:
            async with self.session_factory() as db:
                for i, row in enumerate(rows):
                    if time.monotonic() >= deadline:
                        unattempted = list(rows[i:])
                        logger.error(
                            "Per-row recovery of %s hit its %.0fs deadline; "
                            "re-queueing the %d row(s) not attempted",
                            what, PER_ROW_RECOVERY_DEADLINE_S, len(unattempted),
                        )
                        break
                    try:
                        async with db.begin_nested():
                            await apply_one(db, row)
                        ok.append(row)
                    except Exception as row_exc:  # noqa: BLE001
                        lost.append(row)
                        logger.error(
                            "DROPPING one un-writable %s row: %s", what, row_exc,
                        )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            # The recovery pass itself died, so nothing it "accepted" is durable.
            logger.error(
                "Per-row recovery of %s failed outright (%s); re-queueing "
                "%d row(s)", what, exc, len(ok) + len(unattempted),
            )
            return 0, lost, ok + unattempted
        if ok:
            logger.warning(
                "Per-row recovery of %s salvaged %d of %d row(s)",
                what, len(ok), len(rows),
            )
        return len(ok), lost, unattempted

    def _report_flush_failure(
        self, *, what: str, requeue: list, exc: object, final: bool, log,
    ) -> bool:
        """Say what actually happens to ``requeue`` — and say LOST when it is lost.

        ``stop()`` makes exactly ONE final attempt at each buffer, so "re-queued
        for retry" is a false statement on that path: nothing will ever drain the
        buffer again. Returns True when the caller should re-queue.
        """
        if not requeue:
            return False
        if final:
            logger.error(
                "SHUTDOWN FLUSH FAILED: %d %s row(s) LOST — this was the final "
                "attempt and nothing will retry them: %s",
                len(requeue), what, exc,
            )
            return False
        log(
            "Failed to flush %d %s row(s), re-queued for retry: %s",
            len(requeue), what, exc,
        )
        return True

    async def _flush_persisted(
        self, force_stats: bool = False, *, final: bool = False,
    ) -> None:
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
        # The LogEntry each row came from, so a per-row recovery can re-queue the
        # ENTRIES (which is what `_pending_persist` holds) for the rows it did
        # not manage to write.
        by_entry: dict[str, LogEntry] = {}
        for e in entries:
            if not e.ts:
                continue
            channel_id = self._channel_id_map.get(e.channel) or f"local:{e.channel}"
            by_entry[e.ts] = e
            by_ts[e.ts] = {
                "simulation_run_id": self.simulation_run_id,
                "agent_id": e.sender_agent_id,
                "channel_id": channel_id,
                "channel_name": e.channel,
                "message_ts": e.ts,
                "message_length": len(e.content or ""),
                "thread_ts": e.thread_ts,
                # The entry's own phase wins when it has one; otherwise the
                # shape decides, exactly as it always has. Only a panel note
                # sets it (PHASE_PANEL_NOTE — see _post_panel_note), and this is
                # the write that makes the staff pages agree with the engine
                # for free: src/services/directory.py's discussions listing
                # already keys its roots on phase == 'new_post' and its reply
                # counts on phase == 'thread_reply', so a third value is
                # excluded from both with no query change.
                "phase": e.phase or ("thread_reply" if e.thread_ts else "new_post"),
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

        def _upsert(batch: list[dict]):
            stmt = pg_insert(AgentMessage.__table__).values(batch)
            return stmt.on_conflict_do_update(
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

        try:
            async with self.session_factory() as db:
                await db.execute(_upsert(rows))
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
            #
            # ONE bad row used to take every good row beside it, forever: the
            # re-queued batch fails identically on the next attempt. If (and only
            # if) the error names a ROW rather than the pool or the connection,
            # retry them individually so the poison row is the only casualty.
            requeue = rows
            if isinstance(exc, _ROW_LEVEL_DB_ERRORS):
                async def _one(db, row):
                    await db.execute(_upsert([row]))

                _written, _lost, requeue = await self._recover_rows_individually(
                    rows, _one, what="message",
                )
            if self._report_flush_failure(
                what="message", requeue=requeue, exc=exc, final=final,
                log=logger.warning,
            ):
                self._pending_persist[0:0] = [
                    by_entry[r["message_ts"]] for r in requeue
                ]

    async def _flush_pending_assessments(self, *, final: bool = False) -> None:
        """Retry OpportunityAssessment rows queued by _persist_assessment.

        _persist_assessment attempts an immediate write; a failure there
        (most commonly the pool-checkout timeout Task 2 sized the pool for)
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

                _written, _lost, requeue = await self._recover_rows_individually(
                    rows, _one, what="assessment",
                )
            if self._report_flush_failure(
                what="assessment", requeue=requeue, exc=exc, final=final,
                log=logger.error,
            ):
                self._pending_assessments[0:0] = requeue

    def _enqueue_persist(self, entry: LogEntry) -> None:
        """MessageLog persist callback — buffer a new entry for the next flush."""
        self._pending_persist.append(entry)

    async def _restore_slack_state(self) -> None:
        """Decide what a startup does with the history already on Slack.

        A resumed run reconciles it (that is how a restart recovers its own
        in-flight interviews). A `--fresh` run must NOT: `main.py` has just
        opened a NEW `simulation_run_id`, so this run's `agent_messages` are
        empty by construction (it deletes nothing — see `main._open_fresh_run`),
        and re-importing the same conversations from the transport files every
        one of them under the new run id — measured on
        run 8b64a0e0, where `--fresh` wiped the tables and then appended 914
        messages across 86 threads, so 916 of that run's 1354 rows were
        actually posted before it began (oldest eight days earlier). Three of
        the seven hub interviews that resurrected refused twice on their first
        turn and were abandoned. See
        docs/audits/2026-08-22-run-8b64a0e0/README.md finding M2.

        Skipping the reconcile is NOT sufficient on its own, which is why this
        is a branch and not an early return in the caller. The live poller
        bounds itself with a DIFFERENT cursor map (`_poll_cursors`, defaulting
        to "0"), and on a fresh run nothing populates it —
        `_rebuild_state_from_db` seeds it per stored row and there are no
        stored rows. Left alone it would re-ingest the identical history on the
        first poll tick, just less visibly. So a fresh start still has to walk
        Slack once to establish a baseline; it simply records where history
        ENDS instead of what it contains.

        `agent.state.last_seen_cursor` deliberately needs no equivalent
        treatment: it bounds scans over the in-memory MessageLog, which a fresh
        start leaves empty, and every entry appended during the run carries a
        current timestamp well above its 0.0 default.
        """
        if self._fresh_start:
            await self._seed_slack_cursors_without_ingest()
        else:
            await self._rebuild_state_from_slack()

    async def _seed_slack_cursors_without_ingest(self) -> None:
        """Advance the Slack poll cursors past all existing history, ingesting none.

        The fresh-start half of `_restore_slack_state`. Reads the same channels
        the reconcile and the live poller read, and for each one moves
        `_poll_cursors` to the newest timestamp present, so the first poll tick
        asks Slack only for messages this run itself produced.

        The cursor is the ONLY thing standing between a fresh run and the whole
        back catalogue: `_poll_slack_for_bot_messages` dedups against
        `message_log.get_entry(ts)`, which a fresh start leaves empty, so it
        would re-append every message it fetched. `_known_slack_ts` is
        deliberately NOT seeded here — its only readers are inside
        `_rebuild_state_from_slack`, which this branch exists to skip.

        Channel history is top-level-only, so a pre-run THREAD REPLY can carry a
        ts above the cursor this leaves. That is safe rather than lucky: the
        live poller reads the same top-level-only endpoint, and the only code
        that fetches replies works from a thread the run is already tracking —
        of which a fresh start has none.

        **No channel this pass touches may end with a "0" cursor**, and there are
        three ways it used to:

        1. `AgentSlackClient.get_full_channel_history` CATCHES `SlackApiError`
           and returns `[]`, so the `try/except` below never fires for the
           commonest failure there is. The channel looked empty, the cursor
           stayed "0", and the live poller — a different endpoint, which does
           NOT swallow — re-imported the whole back catalogue on the first tick
           (harness: 30 messages).
        2. A genuinely empty read, indistinguishable from (1) from here.
        3. `_client_for_channel(...) is None`: a private channel with no
           connected member bot, previously a bare `continue`.

        All three now fall back to a WALL-CLOCK ts. That is a deliberate, small
        trade: it is derived from this process's clock rather than Slack's, so a
        clock skew could hide a message posted in the same second as the seed.
        Weighed against re-importing an entire channel's history into a run that
        asked to start clean, and against the fact that this branch only runs
        when we could not read the channel at all, that is the better failure.
        """
        # `_next_poll_client()`, not `next(iter(self.slack_clients.values()))`:
        # the latter picks whatever client happens to be first in the dict, and
        # if THAT one is disconnected the whole seed was skipped — while the live
        # poller, which does use `_next_poll_client`, kept polling happily.
        default_client = self._next_poll_client()
        if not default_client:
            logger.info("No Slack client available — skipping fresh-start cursor seed")
            return

        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        # One wall clock for the whole pass, so every unreadable channel gets the
        # same baseline and the number is not a per-channel accident.
        now_ts = f"{time.time():.6f}"
        channels = 0
        skipped = 0
        unreadable = 0

        def _fallback(ch_id: str) -> None:
            if self._poll_cursors.get(ch_id, "0") == "0":
                self._poll_cursors[ch_id] = now_ts

        for ch_name, ch_id in polled_ids.items():
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.warning(
                    "Fresh-start cursor seed: no connected member bot for "
                    "private channel #%s — parking its cursor at the wall clock "
                    "rather than 0", ch_name,
                )
                _fallback(ch_id)
                unreadable += 1
                continue
            try:
                messages = await client.aget_full_channel_history(ch_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Fresh-start cursor seed failed for #%s: %s", ch_name, exc,
                )
                _fallback(ch_id)
                unreadable += 1
                continue
            newest = ""
            for msg in messages:
                ts = msg.get("ts", "")
                if not ts:
                    continue
                skipped += 1
                if ts > newest:
                    newest = ts
            if newest:
                cur = self._poll_cursors.get(ch_id, "0")
                if newest > cur:
                    self._poll_cursors[ch_id] = newest
                channels += 1
            else:
                # Empty, or an error the client swallowed into an empty list —
                # from here they are the same observation, and only one of them
                # is safe to leave at "0".
                _fallback(ch_id)
                unreadable += 1
        logger.info(
            "--fresh: ignoring %d pre-existing Slack message(s) across %d "
            "channel(s); poll cursors advanced to the current head "
            "(%d channel(s) unreadable or empty, parked at the wall clock)",
            skipped, channels, unreadable,
        )

    async def _rebuild_state_from_slack(self) -> None:
        """Reconcile the MessageLog with Slack history (Slack-on only).

        The DB is the primary store (_rebuild_state_from_db); this pass only
        adds messages that exist on Slack but not yet in the log — via the
        idempotent append, which also persists them to the DB.

        Reached via `_restore_slack_state`, which is what decides a resumed run
        wants this and a `--fresh` run does not.
        """
        # `_next_poll_client()` for the same reason as the fresh-start seed: the
        # old `next(iter(...))` skipped the whole reconcile whenever the first
        # client in the dict happened to be disconnected, and a restart that
        # skips the reconcile cannot recover its own in-flight interviews.
        default_client = self._next_poll_client()
        if not default_client:
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
            messages = await client.aget_full_channel_history(ch_id)
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
                        replies = await client.aget_all_thread_replies(ch_id, ts)
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
                    # that `get_thread_history` (used three lines down for the
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
                # call_times is a deque that try_reserve appends to (Task 9)
                # AND that record_api_call's default (already_reserved=False)
                # path also appends to (Ruling R5 — see that method's
                # docstring; the six call sites that rely on this default are
                # never separately reserved, so record_api_call is the only
                # place they are booked into the window at all), same
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

    @staticmethod
    def _unbooked_calls(call_stats: object) -> int:
        """How many REAL API calls in this turn nothing has booked yet.

        Every caller already books its own terminating call (the two reserved
        sites via ``try_reserve`` + ``record_api_call(already_reserved=True)``,
        consults via ``on_api_call``, the memory update directly) and every
        truncation retry already books itself via ``on_retry``. What no site
        books is the extra TOOL ROUNDS inside ``generate_with_tools``: a turn
        that used three rounds before its final text call made four real billed
        calls and was metered as one.

        So this counts ``kind == "round"`` entries and nothing else. Counting
        ``len(call_stats)`` instead — the obvious fix — double-books every retry
        AND the reservation at the two reserved sites.

        Defensive throughout: this runs inside a logging callback, where raising
        would take a turn down over bookkeeping. A missing or malformed
        ``call_stats`` books nothing extra, which is also exactly right for the
        4,650 of 5,771 stored rows that predate the column.
        """
        if not isinstance(call_stats, list):
            return 0
        return sum(
            1 for c in call_stats
            if isinstance(c, dict) and c.get("kind") == "round"
        )

    def _on_llm_call(self, data: dict) -> None:
        """Callback fired after each LLM API call."""
        # Book the calls this turn made that nothing else booked, BEFORE the
        # buffer append: the flush below can hand control to another coroutine,
        # and the throttle should see the spend as soon as it is known.
        extra = self._unbooked_calls(data.get("call_stats"))
        if extra:
            agent = self.agents.get(data.get("agent_id"))
            if agent is not None:
                for _ in range(extra):
                    agent.record_api_call()
        self._llm_log_buffer.append(data)
        if len(self._llm_log_buffer) >= self._llm_log_flush_size:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._flush_llm_logs())
                # Held in `_flush_tasks` for two reasons: `stop()` has to be able
                # to await it (see there), and a bare `create_task` reference is
                # otherwise only weakly held by the loop, so the task can be
                # garbage-collected mid-flight.
                self._flush_tasks.add(task)
                task.add_done_callback(self._on_flush_done)
            except RuntimeError:
                pass

    def _on_flush_done(self, task: asyncio.Task) -> None:
        """Done-callback for a spawned `_flush_llm_logs`.

        The cancelled-task guard is not defensive tidiness: ``task.exception()``
        RE-RAISES ``CancelledError`` for a cancelled task, so the pre-fix
        callback answered a batch lost to shutdown cancellation with a traceback
        out of the done-callback that said nothing about the rows.
        """
        self._flush_tasks.discard(task)
        if task.cancelled():
            return
        if task.exception():
            logger.error("LLM log flush failed: %s", task.exception())

    def _llm_log_record(self, entry: dict) -> LlmCallLog:
        """One ``llm_call_logs`` row from one buffered callback payload.

        Extracted so the batch write and the per-row recovery build the row the
        same way — a recovery that mapped the fields differently would silently
        store a different row than the one that failed.
        """
        return LlmCallLog(
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
            # The turn's real wall time, which `latency_ms` above is not — it
            # carries only the LAST API call's latency, so summing that column
            # understated true LLM wait by 25% on run 8b64a0e0. Default None
            # rather than 0.0: a producer that supplied nothing means "not
            # recorded", and 0.0 would read as an instantaneous turn.
            wall_ms=entry.get("wall_ms"),
            # Per-API-call breakdown (stop_reason, the requested max_tokens
            # ceiling, thinking/text split) that the three cumulative columns
            # above cannot carry. Default None, not [] — a producer that
            # supplied nothing means "not recorded", and an empty array would
            # read as "recorded, zero calls", which never happens.
            call_stats=entry.get("call_stats"),
            created_at=entry.get("completed_at"),
        )

    async def _flush_llm_logs(self, *, final: bool = False) -> None:
        """Write buffered LLM call logs to the database."""
        if not self._llm_log_buffer or not self.session_factory or not self.simulation_run_id:
            return
        batch = self._llm_log_buffer[:]
        self._llm_log_buffer.clear()
        try:
            async with self.session_factory() as db:
                for entry in batch:
                    db.add(self._llm_log_record(entry))
                await db.commit()
            logger.debug("Flushed %d LLM call logs to DB", len(batch))
        except Exception as exc:
            # Re-queue the failed batch instead of dropping it, exactly like
            # _flush_persisted does for its own buffer: new entries may have
            # been appended to _llm_log_buffer while we were awaiting the
            # (failed) commit, so put the failed batch back in front to
            # preserve chronological order for the next flush attempt. On a
            # ROW-level error only, isolate the poison row first.
            requeue = batch
            if isinstance(exc, _ROW_LEVEL_DB_ERRORS):
                async def _one(db, entry):
                    db.add(self._llm_log_record(entry))

                _written, _lost, requeue = await self._recover_rows_individually(
                    batch, _one, what="LLM call log",
                )
            if self._report_flush_failure(
                what="LLM call log", requeue=requeue, exc=exc, final=final,
                log=logger.warning,
            ):
                self._llm_log_buffer[0:0] = requeue

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

        Mutates self.agents / self.slack_clients IN PLACE — never reassigned —
        in case anything else in the engine has taken a reference to either dict.
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
        calls already spent), but ``_owes_reply`` still reports them as
        not-owed in the gated sense, so a caller that cares about that
        distinction cannot let them outrank gate-compliant work. This is also
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
                        "outside the cohort; it may conclude but is no longer "
                        "treated as owed by _owes_reply",
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

    # ------------------------------------------------------------------
    # Post-simulation
    # ------------------------------------------------------------------

    async def _drain_memory_events(self, limit: int | None = None) -> int:
        """Run queued working-memory updates, strictly FIFO, one at a time.

        Called from the main loop after the reply-lane dispatch and from
        stop() (bounded). Sequential draining under the drain lock is what
        preserves the lost-update guarantee for same-agent updates: each one
        reads the memory text its predecessor wrote. Agents are resolved by
        id at drain time — the roster can change (or an Agent object be
        rebuilt by _sync_roster_from_db) between enqueue and drain, and a
        stale reference would write memory for an object the engine no
        longer owns. _update_agent_memory never raises, so one bad event
        cannot wedge the queue.
        """
        drained = 0
        async with self._memory_drain_lock:
            while self._pending_memory_events:
                if limit is not None and drained >= limit:
                    break
                agent_id, event, visibility, channel_id = (
                    self._pending_memory_events.pop(0)
                )
                agent = self.agents.get(agent_id)
                if agent is None:
                    logger.info(
                        "[memory] dropping queued memory event for %s — no "
                        "longer on the roster", agent_id,
                    )
                    drained += 1
                    continue
                await self._update_agent_memory(
                    agent, event, visibility, channel_id
                )
                drained += 1
        return drained

    async def _update_agent_memory(
        self,
        agent: Agent,
        event: str,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> None:
        """Incrementally update an agent's working memory after a significant event.

        Triggered by thread closure, via the _pending_memory_events queue
        (_close_thread enqueues; _drain_memory_events is the only caller).

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
                # A panel note is the hub's own bookkeeping, not something it
                # said. Left in, it would feed its own consult log back into
                # its working memory — the one place a synthesis could quietly
                # re-derive panel opinion into text every later prompt reads.
                and not is_panel_note(e)
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
(a) Ideas pitched and their screening status (what the hub asked for, conditions
    it named)
(b) Feedback or directions from your PI (if any)
(c) Current priorities

Keep it concise — under 300 words.""",
                }
            ]

            agent.record_api_call()
            # See `_was_truncated`; same collection idiom as the two sites above.
            stop_reasons: list[str] = []
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                # 1800. Was 800, then 1100 on a tokenizer estimate for the
                # Sonnet 5 migration — but measured output on Sonnet 5 is
                # 715-1295 tokens (run 2026-08-19 14:45), so 1100 truncated.
                # Thinking is disabled here, so this is tokenizer growth plus
                # a more verbose model, not thinking sharing the budget.
                #
                # 2600, up from 1800, on 2026-08-21. `call_stats` (migration
                # 0032) makes per-call output measurable, and over run 076e80b6
                # the largest memory update returned 1646 output tokens — 91% of
                # 1800, against the 715-1295 band this ceiling was sized to. A
                # truncated memory write is quiet damage: the only guard below
                # is "empty or blank", so a half-written summary is stored as
                # the working memory and carried into every later turn with
                # nothing in the logs to say the file is short. A ceiling is not
                # a spend — the prompt still asks for under 300 words.
                max_tokens=2600,
                log_meta={"agent_id": agent.agent_id, "phase": "memory"},
                on_retry=agent.record_api_call,
                on_stop_reason=stop_reasons.append,
            )
            if _was_truncated(stop_reasons):
                # REFUSED outright — the strictest of the three answers, because
                # this is the only site with something GOOD already in place. The
                # guard below is "empty or blank", which a half-sentence sails
                # past, so a truncated synthesis replaced the agent's working
                # memory and was then carried into every later prompt with
                # nothing in the logs to say the file had shrunk. Measured in run
                # 8b64a0e0: a complete 1,977-character memory replaced by a
                # 1,437-character one, twice (the file on disk and a
                # `profile_revisions` row). A stale memory is strictly better
                # than a truncated one; the next trigger writes a fresh one.
                logger.warning(
                    "[%s] Memory update: response was TRUNCATED (%s) — keeping "
                    "the existing working memory rather than overwriting it "
                    "with a partial synthesis",
                    agent.agent_id, ", ".join(stop_reasons) or "?",
                )
                return
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


def _reply_closes_thread(text: str) -> bool:
    """True if this reply ENDS the interview — the one definition of the ⏸️
    no-viable-collaboration close.

    ``_check_thread_outcome`` acts on it (``_close_thread(..., "no_proposal")``)
    and ``_capture_hub_assessment`` reads it a few lines earlier, via the
    ``closes_thread`` argument ``_reply_to_thread`` hoists. Those two MUST agree:
    the prompts tell the hub to deliver a negative verdict by opening with ⏸️,
    so a sidecar on a closing reply is the interview's real, final verdict and
    there will be no later turn to supply another. When the gate refused those
    (they are not on ordinal 12) the verdict was destroyed — measured in run
    076e80b6, where 4 of 5 ``premature_sidecar`` refusals were the thread's
    terminal message.

    Deliberately permissive about WHERE the marker appears, matching what this
    check has always done: the thread really is closed by any ⏸️ in the reply,
    so the capture gate has to treat any ⏸️ as terminal too. The stricter
    front-of-string variant is ``_reply_opens_with_pause``.
    """
    body = text or ""
    return "⏸️" in body or ":pause_button:" in body


def _reply_opens_with_pause(text: str) -> bool:
    """True if ``text`` follows the ⏸️ no-viable-collaboration convention
    thread_guidance.py's DECIDE/CONCLUDE instructions ask for verbatim
    ("start your reply with ⏸️") — checked at the front of the (stripped)
    string, not merely present anywhere in it.

    Deliberately stricter than ``_reply_closes_thread`` just above: that one
    exists to actually close the thread (and to tell the capture gate that this
    reply is the interview's last) and is intentionally permissive about where
    the marker appears, while this one is asking "did the model follow the
    documented opening convention" for
    ``_warn_if_hub_conclude_missing_assessment``'s absent-sidecar detection — a
    marker buried mid-reply would not have been the ⏸️-only decline
    thread_guidance describes.
    """
    stripped = (text or "").strip()
    return stripped.startswith("⏸️") or stripped.startswith(":pause_button:")


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


# The prompt's tri-state gating contract (see the <assessment_json> skeleton in
# prompts/roles/scout_hub/phase4-thread-reply.md — relocated there from the
# deleted phase5-new-post.md by the 2026-08-12 removal cycle's reply-only-hub
# reconciliation): every gating.* value must be exactly one of these three
# strings, never a bare boolean — "the PI declined" (not_met) and "we never
# asked" (unconfirmed) are different facts, and a boolean can express only the
# first two of these three outcomes.
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
