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
from src.agent.authorship_rules import (
    LabPublicationRecord,
    lab_self_names,
    normalize_claim_text,
    strip_ungrounded_authorship_lines,
    validate_authorship_claims,
)
from src.agent.channels import SEEDED_CHANNELS
from src.agent.foa_cache import extract_foa_number, format_foa_for_prompt
from src.agent.funding_rules import (
    format_funding_thread_summary,
    format_your_prior_messages,
    is_acknowledgment_only_funding_reply,
    is_announcement_only_funding_reply,
    summarize_funding_thread,
)
from src.agent.ids import WRITER_ENGINE, TsMinter
from src.agent.mentions import BOT_TAG_RE, extract_bot_mentions
from src.agent.message_log import LogEntry, MessageLog, is_funding_post
from src.agent.prompt_safety import delimit
from src.agent.roles import load_role
from src.agent.slack_client import SlackListingIncomplete, ThreadNotFound
from src.agent.state import PostRef, ProposalRef, ThreadState
from src.agent.tools import execute_tool, tools_for_role
from src.config import get_settings
from src.models import (
    AgentChannel,
    AgentMessage,
    LlmCallLog,
    ProposalReview,
    SimulationRun,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
from src.services.cohorts import SERVICE_AGENT_IDS, compute_gates, summarise_gates
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


# Don't kick-start refinement for a handover older than this. A reopen is
# meant to be picked up by the next sim run; if a migrated thread's handover is
# this stale it was either already refined or abandoned, and re-seeding it on a
# fresh process would risk re-posting to a long-dead channel. See
# _seed_private_refinements.
_PRIVATE_REFINEMENT_SEED_MAX_AGE_S = 14 * 24 * 3600  # 14 days

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
_UNIVERSAL_CHANNELS = {"general", "funding-opportunities"}

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

# agent_messages.pi_inbound_state (migration 0029) — the DB inbound poller's
# durable handled-marker, and the only two values anything writes. It exists
# because _poll_inbound_from_db now appends a PI row to the MessageLog BEFORE it
# runs the handler (the append is what records the PI's text durably), so the
# log entry's presence can no longer be the dedup key: MessageLog.append is not
# idempotent, so keying on it would either skip the retry or duplicate the PI's
# message. A THIRD state is carried by NULL — "no inbound poller has claimed
# this row" — which every reader must treat as the pre-0029 behaviour, i.e.
# dedup on log presence. That covers every legacy row and, crucially, every row
# _poll_channels appended itself: this poller re-reads those with no origin
# predicate, so a two-valued marker would re-run handle_channel_tag on every
# tagged Slack message. See docs/plans/2026-09-04-decisions/task-7.md (which
# supersedes D25) and task-8.md.
PI_INBOUND_INGESTED = "ingested"
PI_INBOUND_HANDLED = "handled"
# A fourth state, written at INSERT time by record_pi_message (RC-2 / #20
# blocker): "this row needs a DB inbound poller's attention no matter how far
# behind the cursor it is". Without it, a PI message written while agent-run
# was down could age past PI_INBOX_LOOKBACK_S before the process came back —
# _seed_pi_inbox_cursor jumps the cursor to max(created_at) at startup, so the
# lookback window never reaches a row older than that. 'pending' rows are
# fetched by _poll_inbound_from_db regardless of the cursor and are never
# skipped by the dedup predicate (it only special-cases HANDLED and the
# NULL-fallback), so they always reach the normal ingest→handle→HANDLED path
# once a poller is running again. See docs/plans/2026-09-08-audit-fixes.md RC-2.
PI_INBOUND_PENDING = "pending"

# Cursor value meaning "nothing seen yet" — every real created_at sorts after it.
EPOCH_UTC = datetime.fromtimestamp(0, tz=UTC)

# The run's total_messages / total_api_calls are cosmetic counters shown in the
# admin UI. Recomputing total_messages with a full COUNT(*) on every flush is
# wasteful once a run accumulates many rows (B1), so refresh the run-stats row at
# most this often (a final refresh is forced on shutdown). The message rows
# themselves are still upserted every flush.
RUN_STATS_UPDATE_INTERVAL = 30.0

# Max rows per agent_messages upsert statement. Postgres binds at most 32767
# parameters per statement and each row here binds one per column, so a single
# VALUES list covering the whole buffer breaks once the buffer is a few thousand
# entries. That is not hypothetical: a resumed run reconciles its Slack backlog
# into the buffer before turn 1, so the very first flush of a busy workspace
# carries thousands of rows. Because a failed flush is re-queued in full rather
# than dropped (see _flush_persisted), an oversized batch is a poison pill — it
# fails identically on every retry and the buffer never drains, leaving every
# message in volatile memory while the DB is supposed to be the durable store.
# Observed in production 2026-08-14: 6,988 buffered rows x 16 columns = ~112k
# parameters, failing every turn. The effective chunk is also floored against the
# real column count at call time, so adding columns cannot reintroduce the limit.
PERSIST_MAX_ROWS_PER_STMT = 500
_PG_MAX_BIND_PARAMS = 32767

# Ceiling on the LLM-call-log re-queue (COR-11). _flush_llm_logs prepends a
# failed batch back onto self._llm_log_buffer rather than dropping it, because
# the sliding-window rate limiter rebuilds call_times from llm_call_logs on
# restart. Unbounded, that trades a bounded loss (<= _llm_log_flush_size rows
# per failure) for unbounded retention: every buffered row carries a FULL
# system prompt (prompts/agent-system.md alone is ~14 KB before substitution),
# the message list and the response text, and the agent container runs under
# `mem_limit: 768m`. At an order of ~30 KB per row this ceiling is ~30 MB —
# and it is 100x _llm_log_flush_size, so a sustained outage spanning a hundred
# consecutive flushes is needed before anything is dropped at all. Overflow
# drops the OLDEST rows: what the re-queue exists to protect is the limiter's
# in-window view, and the newest rows are the ones still inside that window.
LLM_LOG_REQUEUE_MAX_ROWS = 1000

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

# RC-9a (#20 audit 2026-09-08): a thread parked for this many of its agent's
# own turns is dropped from active_threads entirely. _is_parked_thread already
# excludes a parked thread from load/rate accounting, but nothing previously
# evicted it — a counterpart that never posts again left the ThreadState
# sitting in active_threads forever, and Phase 3 can always re-activate the
# conversation later if the counterpart does come back. No decision is
# written and no DM is sent (task-5's ruling stands: a parked thread is not
# closed, since it never reached outcome=timeout).
PARKED_THREAD_MAX_TURNS = 20

# Prose-named lab mentions ("the Good lab", "Su Lab's") for the authorship
# guard (audit finding I4): a fabricated co-author named in prose instead of
# @-tagged must still be resolved against the roster. Possessive/article
# words that precede "lab(s)" without naming one are excluded.
_PROSE_LAB_RE = re.compile(r"\b([A-Z][\w-]+)(?:['’]s)?\s+[Ll]abs?\b")

# Same tag pattern as BOT_TAG_RE, wrapped for _strip_disallowed_tags: consumes
# leading whitespace, and a negative lookbehind so "a@subot.example" or a URL
# path is never mangled. Built from BOT_TAG_RE.pattern rather than duplicating
# the literal string, so the two can never drift again (COR-8).
_DISALLOWED_TAG_STRIP_RE = re.compile(
    rf"[ \t]*(?<![\w./@-]){BOT_TAG_RE.pattern}", re.IGNORECASE,
)
_PROSE_LAB_STOPWORDS = frozenset({
    "our", "my", "their", "your", "his", "her", "its", "the", "a", "an",
    "this", "that", "these", "those", "each", "every", "both", "all", "any",
    "other", "another", "wet", "dry", "which", "whose", "one", "two",
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

        self._start_time: datetime | None = None
        self._running = False
        self.message_log = MessageLog()
        self._pi_slack_id_to_agent_ids: dict[str, list[str]] = {}  # PI slack_user_id -> [agent_ids]
        self._dm_poll_cursors: dict[str, str] = {}  # agent_id -> latest DM ts
        self._pi_handler = None  # Initialized in start() after PI mappings loaded

        # Agent name lookups. GrantBot is a service bot: its own token, no
        # AgentRegistry row, never a roster slot — so nothing else ever puts
        # it in this map. It is seeded here (via _rebuild_bot_name_map) because
        # its :moneybag: posts come back in through the same inbound paths as
        # roster bots, and _entry_allowed fails closed on a bot row with a
        # NULL agent_id (unattributable ⇒ belongs to no cohort). Without the
        # entry, every funding post is invisible to every gated agent.
        # setdefault, not assignment: a roster PI actually named Grant would own
        # bot_name "GrantBot", and the roster answer must win. Iterated from
        # SERVICE_AGENT_IDS so a second service bot cannot be added to the
        # manifest validator and admin UI while silently missing the engine.
        # See _rebuild_bot_name_map's docstring for why every later mutation
        # of self.agents also routes back through this same rebuild rather
        # than an incremental edit. Declared (empty) here, not rebound inside
        # _rebuild_bot_name_map, so the dict object's identity is stable —
        # this class's docstrings elsewhere promise in-place mutation of the
        # roster structures it shares by reference (e.g. with PIHandler).
        self._bot_name_to_id: dict[str, str] = {}
        self._rebuild_bot_name_map()
        self.message_log.set_bot_name_map(self._bot_name_to_id)

        # Slack bot_user_id -> agent_id for service bots. The name map above is
        # not sufficient on its own: Slack omits `username` on most of grantbot's
        # posts (all 315 in production landed with sender_name = its raw uid), so
        # uid is the only key that reliably attributes them. Populated by
        # _resolve_service_bot_uids during start(); stays empty when Slack is off.
        self._service_bot_uids: dict[str, str] = {}
        # agent_id -> the token _resolve_service_bot_uids last probed with.
        # Lets _sync_roster_from_db notice a DB-side token rotation on a
        # service bot (no AgentRegistry status=='active' row, so it never
        # appears in the roster diff) and re-run the probe. See #23 COR-26c/D10.
        self._service_bot_tokens: dict[str, str] = {}
        self.message_log.set_bot_uid_map(self._bot_uid_map())

        # agent_id → LabPublicationRecord (publications-table ground truth for
        # the authorship emit guard). Populated by _load_publication_records at
        # roster sync; an agent absent from this dict has NO records and every
        # first-person authorship claim from it fails closed. See issue #29.
        self._agent_publications: dict[str, LabPublicationRecord] = {}

        # LLM call log buffer. Bounded on the failure path by
        # LLM_LOG_REQUEUE_MAX_ROWS — see _flush_llm_logs (COR-11).
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
        # Thread ids whose Slack parent is confirmed gone (ThreadNotFound /
        # silent thread_ts drop), set by _evict_dead_thread. In-process only,
        # deliberately not persisted or derived on rebuild — a durable marker
        # is a follow-up. Unlike _closed_thread_ids (which _reopen_thread
        # legitimately discards for a PI-reopened thread), a dead thread must
        # never be un-tombstoned: _poll_inbound_from_db and
        # _handle_pi_inbound_entry consult this set to refuse to re-hydrate or
        # reopen a thread whose history was purged from the log, which is what
        # closed the resurrection loop found in COR-1c fix round 1 (C1) — a
        # PI row inside the 5-minute inbound lookback would otherwise
        # re-trigger _hydrate_thread_from_db + _reopen_thread every tick.
        self._dead_thread_ids: set[str] = set()
        # Already-accounted-for marker for the _prior_threads append (Phase 5
        # dedup context) — like _closed_thread_ids, but ALSO covers a
        # reopened-and-not-yet-reclosed thread, which _rebuild_agent_state
        # must not add to _closed_thread_ids (that set means "definitely
        # done"). See COR-13 / red-team B6.
        self._prior_thread_accounted: set[str] = set()

        # #20 I1: consecutive Slack-post-failure count for a Phase 5 reply
        # target, keyed by (agent_id, target_post_id). Phase 4's equivalent
        # counter lives on ThreadState (post_failure_count) because a
        # ThreadState always already exists there; Phase 5's two reply branches
        # attempt to post to a target BEFORE any ThreadState exists for it (one
        # is only ever created on a SUCCESSFUL threaded reply, and never for a
        # flat private-channel reply), so there is nothing to hang a per-thread
        # field on until a post actually lands. The agent_id is part of the key
        # because this map is engine-level while the thing it counts is
        # per-agent: several agents can hold the same post in
        # interesting_posts, and on a post-id-only key one agent's refusals
        # would back another agent off after a single failure of its own, while
        # one agent's success would clear every other agent's strikes (#20
        # COR-1b). Popped on that agent's success or once its two-strike drop
        # fires — never grows unbounded.
        self._phase5_post_failure_counts: dict[tuple[str, str], int] = {}

        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}

        # Thread IDs already reopened via DB-synced PI guidance (rating=0 reviews)
        # to avoid re-processing on every turn.
        self._db_reopened_thread_ids: set[str] = set()

        # Thread IDs whose private-channel refinement handover has already been
        # seeded as a PI-priority interesting post, so we kick-start refinement
        # exactly once per process. See _seed_private_refinements.
        self._db_private_refined_thread_ids: set[str] = set()

        # Names of collab_private channels whose refinement has converged on a
        # recorded revised proposal. Bots stop posting there (Phase 5 skips
        # them) and finalization is not re-run. Populated at startup from the DB
        # and when a private refinement is finalized. See
        # _finalize_private_proposal / _check_private_channel_outcome.
        self._finalized_private_channels: set[str] = set()

        # Last-seen (exists, mtime) signature of each agent's on-disk profile
        # files (private + public), keyed by agent_id. The web editor runs in
        # a separate process and writes profiles/{private,public}/{id}.md on a
        # shared volume; this process caches profile content per Agent, so a
        # per-turn signature check tells us when an external edit — including
        # a deletion — happened and the cache must be invalidated. See
        # _sync_profiles_from_disk.
        self._profile_mtimes: dict[str, tuple[tuple[str, bool, float | None], ...]] = {}

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

    @staticmethod
    def _is_parked_thread(thread: ThreadState) -> bool:
        """True while the two-strike post back-off has this thread benched.

        The single definition of "parked", shared by ``_agent_load`` and
        ``_non_funding_thread_count`` so the two views of an agent's live load
        cannot drift apart. It is exactly the state the Phase 4 refusal branch
        leaves a thread in: ``post_failure_count`` reached 2 and
        ``has_pending_reply`` was cleared, so nothing re-enqueues the thread
        until the counterpart posts again.

        Both halves are load-bearing. ``has_pending_reply`` is False after every
        SUCCESSFUL reply too, so on its own it would exclude perfectly healthy
        threads waiting on their partner; ``post_failure_count`` stays at 2
        after ``has_new`` revives a thread (it is only zeroed by a successful
        post), so on its own it would keep excluding a thread that is a live
        obligation again.

        A parked thread is not closed. Closing it would write outcome="timeout"
        into a PI DM, into ``/admin/discussions`` and into both agents'
        prompt-fed working memory, none of which happened — see
        docs/plans/2026-09-04-decisions/task-5.md (#20 COR-1b).
        """
        return thread.post_failure_count >= 2 and not thread.has_pending_reply

    def _evict_stale_parked_threads(self, agent: Agent) -> None:
        """Drop a thread parked for PARKED_THREAD_MAX_TURNS of this agent's
        own turns (RC-9a, #20 audit 2026-09-08).

        A parked thread (``_is_parked_thread``) already generates no LLM work
        and is excluded from load/rate accounting, but nothing previously
        evicted it from ``active_threads`` — a counterpart that never posts
        again left it sitting there forever, a permanent slot spent on a
        conversation that can only be revived by a restart's Phase 3
        re-hydration or the counterpart posting again. This only removes THIS
        agent's ``ThreadState``; it writes no decision and sends no DM (a
        parked thread never reached outcome=timeout — task-5's ruling stands),
        so the counterpart's own side (if any) and the persisted message
        history are untouched, and Phase 3 can re-activate the thread later.
        """
        for thread_id, thread in list(agent.state.active_threads.items()):
            if self._is_parked_thread(thread):
                thread.parked_turns += 1
                if thread.parked_turns >= PARKED_THREAD_MAX_TURNS:
                    agent.state.active_threads.pop(thread_id, None)
                    logger.info(
                        "[%s] Dropping thread %s from active_threads after %d "
                        "parked turns",
                        agent.agent_id, thread_id, thread.parked_turns,
                    )
            else:
                thread.parked_turns = 0

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

        Threads parked by the post back-off are not obligations: they generate no
        LLM call until the counterpart speaks, so counting them handed the agent
        allowance and selection weight for work it cannot do (#20 COR-1b).
        """
        live = sum(
            1 for t in agent.state.active_threads.values()
            if t.status == "active" and not self._is_parked_thread(t)
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

    def _non_funding_thread_count(self, agent: Agent) -> int:
        """Count active threads that are NOT funding-related.

        Feeds ``at_thread_threshold`` in Phase 5, so every thread counted here
        spends one of the agent's ``active_thread_threshold`` regular discussion
        slots. Threads parked by the post back-off are excluded on the same
        ``_is_parked_thread`` test ``_agent_load`` uses: with a silent
        counterpart a parked thread never advances to the 12-message timeout
        close and never leaves ``active_threads``, so counting it took the slot
        permanently — three of them put the agent into ``blocked_for_regular``
        for the rest of the run, after which Phase 5 returns before the LLM call
        whenever nothing funding/PI-priority/private is available (#20 COR-1b).
        """
        return sum(
            1 for t in agent.state.active_threads.values()
            if not self._is_parked_thread(t)
            and not self.message_log.is_funding_thread(t.thread_id)
        )

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
        # Learn service-bot uids BEFORE the Slack reconcile, which attributes bot
        # senders by uid. This only covers messages NEW to the log: rows already
        # persisted with a NULL agent_id are skipped by the reconcile (their ts
        # is in _known_slack_ts and MessageLog.append is ts-idempotent), so they
        # stay NULL — the historical grantbot backlog was repaired by a one-shot
        # UPDATE against agent_messages at rollout, not by this pass.
        await self._resolve_service_bot_uids()
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

        # Backfill FOA cache for any previously posted opportunities
        await self._backfill_foa_cache()

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
        - **The remaining threads are read through the agent's gate.** Threads are
          not always two-party — a funding thread is open to all
          (``get_thread_allowed_agents`` returns None) — so a non-cohort third party
          posting into an otherwise legal thread would otherwise manufacture
          reactive priority for a sender the agent is not supposed to act on.
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

        # RC-9a: evict a thread that has been parked too long before it can
        # occupy any of this turn's other phases.
        self._evict_stale_parked_threads(agent)

        # Phase 1: Channel discovery
        self._phase1_channel_discovery(agent)

        # Phase 2: Scan & filter new posts
        await self._phase2_scan_filter(agent)

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
        phase2_ran = agent.api_call_count > api_calls_before
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

        has_new_work = has_interesting or has_phase4_work or phase2_ran or has_pi

        if has_new_work or spontaneous_ready:
            api_calls_before_phase5 = agent.api_call_count
            await self._phase5_new_post(agent, phase4_thread_ids)
            phase5_acted = agent.api_call_count > api_calls_before_phase5
        else:
            phase5_acted = False
            logger.debug(
                "[%s] Phase 5: Skipped (no state change, spontaneous in %ds)",
                agent.agent_id,
                int(spontaneous_interval - since_last_action),
            )

        # Clear the PI directive flag only once it has actually been acted
        # on — Phase 5 (the flag's only trigger, via has_new_work) made a
        # real LLM call this turn. If Phase 5 bailed out early (daily cap,
        # random skip, blocked with nothing available) the directive is
        # preserved so a later turn retries it, instead of being silently
        # dropped having influenced no prompt at all. See E7b.
        # Known trade-off (red-team m5): this is a latch, not a retry-with-
        # backoff — an agent PERMANENTLY unable to get Phase 5 to act (stuck
        # blocked with nothing bypass-eligible available, or sustained
        # rate-limiting) never clears the flag. Cheap (no LLM cost — Phase 5
        # still bails out before any API call in that state) but unbounded;
        # a turn counter or TTL would cap it if that ever proves to matter in
        # practice.
        if agent.state.has_pi_directive and phase5_acted:
            agent.state.has_pi_directive = False

        # Update cursor. Bounded by the log's own high-water mark, not the wall
        # clock: MessageLog filters on `posted_at <= since`, and an external
        # writer (a Slack human, the web app, GrantBot — each minting posted_at
        # from its own clock) can commit a row whose posted_at is behind
        # `time.time()` by the time this turn ends. A wall-clock cursor would
        # already be past that row's posted_at, filtering it out of every
        # future scan forever. `latest_timestamp` never exceeds what is
        # actually in the log, which removes the wall-clock/DB-clock skew this
        # bug is named for — though a late-arriving row is only rescued by
        # this bound when it also happens to be the newest thing in the log at
        # that moment; a row that commits after the cursor advanced but stays
        # below the log's max is still filtered by `posted_at <= since`
        # elsewhere. `_poll_inbound_from_db`'s `PI_INBOX_LOOKBACK` window is
        # the real belt-and-braces for that narrower residual case (red-team
        # m3). See COR-6.
        agent.state.last_seen_cursor = self.message_log.latest_timestamp

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
        """Scan new top-level posts and decide which to add to interesting_posts."""
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
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 2: Empty response from LLM, skipping", agent.agent_id)
                return
            result = _extract_json(response)
            selected_ids = set(result.get("selected_post_ids", []))

            # Add selected posts to interesting_posts
            for post in new_posts:
                if post.ts in selected_ids:
                    foa_num = None
                    snippet_len = 200
                    if is_funding_post(post.content):
                        foa_num = extract_foa_number(post.content)
                        snippet_len = 500  # funding posts need more context
                    agent.state.interesting_posts.append(PostRef(
                        post_id=post.ts,
                        channel=post.channel,
                        sender_agent_id=post.sender_agent_id or post.sender_name,
                        content_snippet=post.content[:snippet_len],
                        posted_at=post.posted_at,
                        foa_number=foa_num,
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
        """Prune interesting_posts to ≤ cap."""
        system_prompt, messages = agent.build_phase2_prune_prompt()

        agent.record_api_call()
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500,
                log_meta={"agent_id": agent.agent_id, "phase": "prune"},
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
            is_funding = self.message_log.is_funding_thread(thread_id)
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
                # Extract FOA number from root post for funding threads
                foa_num = None
                if is_funding:
                    root = self.message_log.get_entry(thread_id)
                    if root:
                        foa_num = extract_foa_number(root.content)
                # message_count_offset mirrors the reopen paths (_reopen_thread,
                # the web reopen): a thread already at or near the message cap
                # when this agent is first tagged/replied-into it must not
                # instantly close as "timeout" before the agent gets a chance
                # to post. See COR-2.
                existing_count = self.message_log.get_thread_message_count(thread_id)
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=existing_count,
                    message_count_offset=existing_count,
                    has_pending_reply=True,
                    foa_number=foa_num,
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
            is_funding = self.message_log.is_funding_thread(thread_id)
            # Threshold gates Phase 5 (starting new threads), not Phase 3.
            # Ghosting a reply to our own post is worse than running over the cap.
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                # Extract FOA number from root post for funding threads
                foa_num = None
                if is_funding:
                    root = self.message_log.get_entry(thread_id)
                    if root:
                        foa_num = extract_foa_number(root.content)
                existing_count = self.message_log.get_thread_message_count(thread_id)
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=existing_count,
                    message_count_offset=existing_count,
                    has_pending_reply=True,
                    foa_number=foa_num,
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (reply from %s)",
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

        # Mid-turn rate gate (E6-1): _within_rate_limit was previously
        # consulted only once, at turn selection (_turn_eligible, :891/893).
        # Phase 4 fans every active thread out in one gather with per-retry
        # booking, so a turn could overshoot the sliding-window allowance by
        # as many threads as were open.
        #
        # Fix-round-1 (I1): a per-thread `_within_rate_limit()` loop here is
        # semantically all-or-nothing, not a real cap — nothing books a call
        # between iterations (that only happens once each dispatched thread's
        # generate_agent_response call actually lands, well after this whole
        # batch has been dispatched), so _within_rate_limit(agent, now) is
        # pure and returns the SAME answer on every iteration: either every
        # thread passes or (once the deque is pruned) every thread fails.
        # Measured: allowance 1 + three pending threads dispatched all three.
        #
        # A headroom slice, not a per-item re-check: call _within_rate_limit
        # once for its pruning side effect (it drops expired entries from
        # agent.state.call_times), then compute how many calls are actually
        # still free under the allowance and take exactly that many threads —
        # the same allowance/load expression _within_rate_limit itself uses.
        # Threads beyond the slice keep has_pending_reply=True (set above) and
        # are retried next turn exactly like a failed/empty reply already is.
        now = time.time()
        self._within_rate_limit(agent, now)
        headroom = (
            int(self._calls_per_load(agent) * self._agent_load(agent))
            - len(agent.state.call_times)
        )
        eligible_threads = threads_to_reply[: max(0, headroom)]
        if len(eligible_threads) < len(threads_to_reply):
            logger.info(
                "[%s] Phase 4: rate-limited, skipping %d remaining thread(s) this turn",
                agent.agent_id, len(threads_to_reply) - len(eligible_threads),
            )

        # Run replies in parallel
        tasks = [
            self._reply_to_thread(agent, thread)
            for thread in eligible_threads
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

        # Funding-thread context (self-dedup + late-joiner summary)
        is_funding = self.message_log.is_funding_thread(thread.thread_id)
        your_prior_text: str | None = None
        thread_activity_text: str | None = None
        if is_funding:
            your_prior_entries = [
                e for e in history_entries if e.sender_agent_id == agent.agent_id
            ]
            your_prior_text = format_your_prior_messages(your_prior_entries)
            summary = summarize_funding_thread(
                self.message_log, thread.thread_id, viewer_agent_id=agent.agent_id,
            )
            thread_activity_text = format_funding_thread_summary(summary)

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
            is_funding_thread=is_funding,
            your_prior_messages=your_prior_text,
            thread_activity_summary=thread_activity_text,
            visibility=thread_visibility,
            channel_id=thread_channel_id,
        )

        # Create tool executor bound to this thread's state
        async def tool_executor(tool_name: str, tool_input: dict) -> str:
            return await execute_tool(
                tool_name, tool_input, agent.agent_id, thread, role=agent.role
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
            # build_phase4_prompt already injected this as "authoritative" PI
            # direction (agent.py:461-467) into the prompt this call just sent
            # — clear it here, not right after build_phase4_prompt returns, so
            # a transport/LLM error on THIS call (swallowed by Phase 4's
            # asyncio.gather(return_exceptions=True) fan-out) does not lose
            # the guidance having reached no prompt at all. See E7c / m4.
            thread.pi_context = None

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

            # Funding-thread draft validators: reject announcement-only and
            # acknowledgment-only replies before they hit Slack.
            if is_funding:
                rejected_reason = None
                if is_announcement_only_funding_reply(response_text):
                    rejected_reason = "announcement-only"
                elif is_acknowledgment_only_funding_reply(response_text):
                    rejected_reason = "acknowledgment-only"
                if rejected_reason:
                    thread.funding_reject_count += 1
                    logger.info(
                        "[%s] Phase 4: Rejected %s draft in funding thread %s (count=%d)",
                        agent.agent_id, rejected_reason, thread.thread_id,
                        thread.funding_reject_count,
                    )
                    if thread.funding_reject_count >= 2:
                        # Back off: drop the pending-reply flag so the agent
                        # stops re-attempting this thread for a while.
                        thread.has_pending_reply = False
                        logger.info(
                            "[%s] Phase 4: Backing off funding thread %s after %d rejections",
                            agent.agent_id, thread.thread_id, thread.funding_reject_count,
                        )
                    return

                # The draft cleared both funding validators this turn. Reset
                # here rather than only on a later successful post (issue #23
                # COR-28b'): a draft suppressed for an unrelated reason (dedup,
                # a transient Slack failure) used to leave a stale reject
                # streak in place, so two rejections caused by the ack/
                # announcement detectors' known false positives could
                # permanently back the thread off even after it recovered.
                thread.funding_reject_count = 0

            # Authorship guard (issue #29): reject drafts claiming authorship
            # the publication records cannot verify — mirrors the funding
            # validators' reject-and-back-off pattern, but applies to EVERY
            # thread, funding or not.
            authorship_reason = self._reject_ungrounded_authorship(agent, response_text)
            if authorship_reason:
                thread.authorship_reject_count += 1
                logger.warning(
                    "[%s] Phase 4: Rejected reply to thread %s — %s (count=%d)",
                    agent.agent_id, thread.thread_id, authorship_reason,
                    thread.authorship_reject_count,
                )
                if thread.authorship_reject_count >= 2:
                    thread.has_pending_reply = False
                    logger.info(
                        "[%s] Phase 4: Backing off thread %s after %d authorship rejections",
                        agent.agent_id, thread.thread_id, thread.authorship_reject_count,
                    )
                return

            # Post the reply
            posted = await self._post_message(
                agent.agent_id, thread.channel, response_text,
                thread_ts=thread.thread_id,
            )
            if not posted:
                # #20 I1: a deterministic Slack refusal (is_archived,
                # not_in_channel, invalid_auth, recurring msg_too_long) used
                # to just log here forever — has_pending_reply stayed True
                # and message_count never advanced, so the thread could never
                # reach the 12-message timeout close and burned one LLM call
                # per turn indefinitely. Mirrors authorship_reject_count's
                # two-strike pattern.
                thread.post_failure_count += 1
                # "nothing persisted" until f29e295, which restored the
                # DB-only row (slack_ts=None) a refused post writes — the text
                # survives, only the Slack identity and the turn do not.
                logger.info(
                    "[%s] Suppressed post in #%s — turn not counted, kept as a "
                    "DB-only row (post_failure_count=%d)",
                    agent.agent_id, thread.channel, thread.post_failure_count,
                )
                if thread.post_failure_count >= 2:
                    # Park the thread: nothing re-enqueues it until the
                    # counterpart posts (:1402). _is_parked_thread reads exactly
                    # this state so the parked thread stops spending one of the
                    # agent's regular discussion slots and stops inflating its
                    # rate allowance — see #20 COR-1b and
                    # docs/plans/2026-09-04-decisions/task-5.md.
                    thread.has_pending_reply = False
                    logger.info(
                        "[%s] Phase 4: Backing off thread %s after %d post failures",
                        agent.agent_id, thread.thread_id, thread.post_failure_count,
                    )
            else:
                agent.message_count += 1
                thread.has_pending_reply = False
                thread.funding_reject_count = 0
                thread.authorship_reject_count = 0
                thread.empty_response_count = 0
                thread.post_failure_count = 0

                # Check for thread outcome
                await self._check_thread_outcome(agent, thread, response_text)

        except Exception as exc:
            logger.error(
                "[%s] Phase 4 reply to thread %s failed: %s",
                agent.agent_id, thread.thread_id, exc,
            )

    @staticmethod
    def _is_finalize_marker(text: str) -> bool:
        """True when ``text`` carries the proposal-confirmation signal.

        Accepts both the raw emoji and Slack's shortcode — the LLM is asked
        for the unicode form (agent.py, prompts/phase4-thread-reply.md,
        prompts/agent-system.md) but nothing stops it emitting the shortcode
        on its own, and a missed match here means a silent grind to the
        12-message timeout close instead of a clean finalize. See COR-4.
        Shared by the public (_check_thread_outcome) and private
        (_check_private_channel_outcome) paths so they can't drift again.
        """
        return "✅" in text or ":white_check_mark:" in text

    async def _check_thread_outcome(
        self,
        agent: Agent,
        thread: ThreadState,
        latest_reply: str,
    ) -> None:
        """Check if a thread should be closed based on the latest reply."""
        # Check for ✅ confirmation of a :memo: Summary
        if self._is_finalize_marker(latest_reply):
            # The memo must be the OTHER agent's MOST RECENT message in the
            # thread — not merely the most recent one of theirs that happens
            # to contain :memo:. If they have said anything else since (a
            # renegotiation, a question, anything), that memo is stale and
            # this ✅ is not confirming it. See COR-3.
            history = self.message_log.get_thread_history(thread.thread_id)
            for entry in reversed(history):
                if entry.sender_agent_id != thread.other_agent_id:
                    continue
                if ":memo:" in entry.content:
                    # Proposal confirmed!
                    logger.info(
                        "[%s] Thread %s: proposal confirmed with ✅",
                        agent.agent_id, thread.thread_id,
                    )
                    # Extract text starting from :memo: marker
                    memo_idx = entry.content.find(":memo:")
                    summary_text = entry.content[memo_idx:].strip() if memo_idx >= 0 else entry.content
                    agent.state.pending_proposals = [
                        p for p in agent.state.pending_proposals
                        if p.thread_id != thread.thread_id
                    ]
                    agent.state.pending_proposals.append(ProposalRef(
                        thread_id=thread.thread_id,
                        channel=thread.channel,
                        other_agent_id=thread.other_agent_id,
                        summary_text=summary_text,
                        proposed_at=time.time(),
                    ))
                    await self._close_thread(agent, thread, "proposal", summary_text)
                    return
                # The other agent's latest message is NOT a memo — this ✅ is
                # not confirming anything. Stop before an older memo. (COR-3)
                break

        # Check if this agent posted a :memo: Summary
        if ":memo:" in latest_reply:
            # The other agent needs to confirm — thread stays active
            thread.status = "active"
            logger.info(
                "[%s] Thread %s: posted :memo: Summary, waiting for ✅",
                agent.agent_id, thread.thread_id,
            )
            return

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
        if thread.status == "closed":
            # Idempotency guard (COR-7): a second call against the same
            # ThreadState instance would otherwise double the ThreadDecision
            # row, the PI DM, both agents' memory updates, and the
            # _prior_threads dedup-context entry. Placed before the DB write
            # (Task 20.10/COR-13 reordered this method so that write happens
            # first) rather than after it, since the write itself is one of
            # the things a repeat call must not re-trigger. A genuine reopen
            # builds a NEW ThreadState (see _reopen_thread / the web reopen
            # in _sync_proposal_reviews_from_db), so a legitimately reopened
            # and re-closed thread is unaffected — it runs this method on a
            # fresh object whose status was never "closed".
            logger.debug(
                "[%s] _close_thread: thread %s is already closed, skipping",
                agent.agent_id, thread.thread_id,
            )
            return
        thread.status = "closed"
        self._closed_thread_ids.add(thread.thread_id)
        self._prior_thread_accounted.add(thread.thread_id)

        # Log to DB FIRST (moved ahead of the dedup-context append and the
        # other-agent close) so decision.id is available for the
        # ProposalRef(s) constructed below. See COR-13.
        # decision.id is readable after commit because every session factory
        # in this codebase sets expire_on_commit=False (src/database.py:41,
        # src/agent/main.py, src/worker/main.py, src/agent/grantbot.py,
        # tests/conftest.py) — stated explicitly so a later "cleanup" does not
        # move this read before the commit under the mistaken belief it is
        # needed to dodge an expired-attribute refresh (red-team m7).
        decision_id: uuid.UUID | None = None
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
                    decision_id = decision.id
            except Exception as exc:
                logger.warning("Failed to log thread decision: %s", exc)

        # Track for Phase 5 dedup context. Carries thread_id (missing before —
        # COR-7) so a future reader can tell two entries apart or dedup by it;
        # the idempotency guard above is what actually prevents an accidental
        # duplicate append for the same close event.
        pair_key = tuple(sorted([agent.agent_id, thread.other_agent_id]))
        self._prior_threads.setdefault(pair_key, []).append({
            "thread_id": thread.thread_id,
            "channel": thread.channel,
            "outcome": outcome,
            "summary": (summary_text or "")[:400] or None,
        })

        # This agent's own ProposalRef (if any) was appended by the caller
        # (_check_thread_outcome / _check_private_channel_outcome) BEFORE
        # _close_thread ran, so decision_id was not yet known there — stamp
        # it now.
        if decision_id is not None:
            for p in agent.state.pending_proposals:
                if p.thread_id == thread.thread_id:
                    p.thread_decision_id = decision_id
                    break

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
                    thread_decision_id=decision_id,
                ))

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

    async def _check_private_channel_outcome(
        self, agent: Agent, channel: str, message_text: str,
    ) -> None:
        """Flat-channel analog of _check_thread_outcome for collab_private refinement.

        Collab_private channels are flat (no ThreadState / threading), so the
        threaded :memo:-Summary→✅ finalization never runs there. Here we detect
        the same handshake on top-level posts: when this agent posts a ✅ that
        confirms the *other* member's most recent :memo: Summary, we record the
        refined proposal (see _finalize_private_proposal). A bare :memo: just
        waits for the other bot's ✅.
        """
        if channel in self._finalized_private_channels:
            return
        if not self._is_finalize_marker(message_text):
            return
        cid = self._channel_id_map.get(channel)
        if not cid:
            return
        other_id = next(
            (m for m in self._private_channel_members.get(cid, set()) if m != agent.agent_id),
            None,
        )
        if not other_id:
            return
        # Find the other member's most recent *revised* :memo: Summary in this
        # channel. Skip the handover post: it embeds the ORIGINAL proposal
        # summary (also marked :memo:), so without this a casual ✅ could
        # finalize the un-revised proposal. The handover is identifiable by its
        # header (see private_channels._build_handover_messages).
        for entry in reversed(self.message_log._entries):
            if entry.channel != channel:
                continue
            if entry.sender_agent_id != other_id or ":memo:" not in entry.content:
                continue
            if "Private refinement channel" in entry.content:
                continue  # handover, not a revised summary
            memo_idx = entry.content.find(":memo:")
            summary_text = entry.content[memo_idx:].strip()
            await self._finalize_private_proposal(
                agent, other_id, channel, entry.ts, summary_text,
            )
            return

    async def _finalize_private_proposal(
        self,
        agent: Agent,
        other_id: str,
        channel: str,
        thread_id: str,
        summary_text: str,
    ) -> None:
        """Record a refined proposal reached in a collab_private channel.

        Writes a ThreadDecision with origin_visibility='collab_private' (kept out
        of the public collaboration graph — see the visibility filter in
        routers/public.py), blocks both bots pending review (a pending unreviewed
        proposal), marks the channel finalized so refinement stops, and DMs the
        PI. Idempotent: a private proposal already recorded for this channel is a
        no-op. The PI reviews it through the normal dashboard/email flow (both
        PIs are members of the channel).
        """
        if channel in self._finalized_private_channels:
            return
        decision_id: uuid.UUID | None = None
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    existing_row = (await db.execute(
                        sa_select(ThreadDecision.id).where(
                            ThreadDecision.channel == channel,
                            ThreadDecision.origin_visibility == VISIBILITY_COLLAB_PRIVATE,
                            ThreadDecision.outcome == "proposal",
                        )
                    )).first()
                    if existing_row is None:
                        decision = ThreadDecision(
                            simulation_run_id=self.simulation_run_id,
                            thread_id=thread_id,
                            channel=channel,
                            agent_a=agent.agent_id,
                            agent_b=other_id,
                            outcome="proposal",
                            summary_text=summary_text,
                            origin_visibility=VISIBILITY_COLLAB_PRIVATE,
                        )
                        db.add(decision)
                        await db.commit()
                        decision_id = decision.id
                    else:
                        decision_id = existing_row[0]
            except Exception as exc:
                logger.warning("Failed to record private refined proposal: %s", exc)
                return

        self._finalized_private_channels.add(channel)

        # Block both bots pending review and reflect the proposal in their state.
        for aid, other in ((agent.agent_id, other_id), (other_id, agent.agent_id)):
            ag = self.agents.get(aid)
            if not ag:
                continue
            ag.state.pending_proposals = [
                p for p in ag.state.pending_proposals if p.thread_id != thread_id
            ]
            ag.state.pending_proposals.append(ProposalRef(
                thread_id=thread_id,
                channel=channel,
                other_agent_id=other,
                summary_text=summary_text,
                proposed_at=time.time(),
                reviewed=False,
                thread_decision_id=decision_id,
            ))

        logger.info(
            "[%s] Finalized revised proposal with %s in private #%s — recorded for PI review",
            agent.agent_id, other_id, channel,
        )

        # DM the finalizing agent's PI (best-effort). The normal unreviewed-
        # proposal email/dashboard flow surfaces it to both PIs for review.
        if self._pi_handler:
            try:
                shim = ThreadState(
                    thread_id=thread_id, channel=channel, other_agent_id=other_id,
                )
                await self._pi_handler.notify_thread_conclusion(
                    agent.agent_id, shim, "proposal", summary_text,
                )
            except Exception as exc:
                logger.debug("PI notify (private proposal) failed: %s", exc)

    def _evict_dead_thread(self, thread_id: str) -> None:
        """Remove a thread_id from every agent's in-memory state.

        Fires when Slack reports the parent message no longer exists (via
        ThreadNotFound from conversations.replies or a silent thread_ts drop
        on chat.postMessage). Without eviction the same dead thread gets
        re-polled and replied-to forever, producing noisy error logs and —
        worse — cascading top-level posts.

        Defensive (#20 C1): refuse when the root is DB-only (``slack_ts`` is
        ``None``) — Slack has never seen this thread, so a ``ThreadNotFound``
        for it is a caller mistranslation, not proof the thread is dead.
        Every current caller already translates via ``_slack_parent_ts``
        before calling Slack, so this should be unreachable in practice; it
        guards future call sites the same way.
        """
        root = self.message_log.get_entry(thread_id)
        if root is not None and root.slack_ts is None:
            logger.warning(
                "Refusing to evict DB-only thread %s (no Slack root)", thread_id,
            )
            return
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
        # NOT a discard, and an ADD rather than a no-op (red-team m1): a dead
        # thread (Slack deleted the parent) must come out of this eviction
        # CLOSED regardless of whether it already was — it gains nothing from
        # being reopenable (its history is gone from the log two lines below).
        # The old `.discard()` here un-closed a thread the outcome machinery
        # had already finalized, which is what let a stale ThreadDecision keep
        # scheduling replies to a grave. See COR-1c.
        #
        # Both this marker and _dead_thread_ids below are in-process only:
        # _rebuild_agent_state derives its closed-thread set from
        # ThreadDecision rows, and eviction writes none, so a restart still
        # re-hydrates a dead thread from the DB (pre-existing behaviour, not a
        # regression this task introduces — a durable eviction marker is a
        # deliberate follow-up, not in scope here).
        self._closed_thread_ids.add(thread_id)
        # Tombstone: unlike _closed_thread_ids, NEVER discarded for this
        # thread_id. Without it, _poll_inbound_from_db's 5-minute lookback
        # would re-ingest a PI's DB row for this now-purged thread (the
        # dedup-by-get_entry check no longer finds it) and
        # _handle_pi_inbound_entry would see thread_id in _closed_thread_ids
        # and call _hydrate_thread_from_db + _reopen_thread — resurrecting a
        # thread whose Slack parent is gone, which fails to post again,
        # re-evicts, and repeats every tick until the row ages out of the
        # lookback. See COR-1c fix round 1 (C1).
        self._dead_thread_ids.add(thread_id)
        purged = self.message_log.purge_thread(thread_id)
        if evicted_from or purged:
            logger.info(
                "Evicted dead thread %s from %d agent(s)' state (purged %d log entries)",
                thread_id, evicted_from, purged,
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

    def _note_phase5_post_failure(self, agent: Agent, target_post_id: str) -> None:
        """Two-strike backoff for a Slack-refused Phase 5 reply (#20 I1).

        Mirrors ThreadState.post_failure_count's shape, tracked per
        (agent_id, target_post_id) on the engine instead — see
        ``self._phase5_post_failure_counts``'s docstring for why. After this
        agent's second consecutive failure, drops ``target_post_id`` from
        ``agent.state.interesting_posts`` so it stops being re-targeted (and
        re-costing an LLM call) every turn. Another agent's attempts on the
        same post neither add to nor clear this count.
        """
        key = (agent.agent_id, target_post_id)
        count = self._phase5_post_failure_counts.get(key, 0) + 1
        self._phase5_post_failure_counts[key] = count
        logger.info(
            "[%s] Suppressed reply to %s — turn not counted, kept as a "
            "DB-only row (post_failure_count=%d)",
            agent.agent_id, target_post_id, count,
        )
        if count >= 2:
            agent.state.interesting_posts = [
                p for p in agent.state.interesting_posts if p.post_id != target_post_id
            ]
            self._phase5_post_failure_counts.pop(key, None)
            logger.info(
                "[%s] Phase 5: Backing off post %s after %d consecutive failures",
                agent.agent_id, target_post_id, count,
            )

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

        today_posts = self._count_today_posts(agent)

        # Check preconditions
        at_thread_threshold = self._non_funding_thread_count(agent) >= settings.active_thread_threshold
        unreviewed_non_funding_count = sum(
            1 for p in agent.state.pending_proposals
            if not p.reviewed and not self.message_log.is_funding_thread(p.thread_id)
        )
        has_unreviewed_non_funding = (
            agent.agent_id not in UNBLOCK_EXEMPT_AGENTS
            and unreviewed_non_funding_count >= settings.unreviewed_proposal_block_count
        )
        blocked_for_regular = at_thread_threshold or has_unreviewed_non_funding

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

            is_funding = self.message_log.is_funding_thread(post.post_id)
            # Posts in collab_private channels are by definition PI-engaged
            # refinement; they must bypass the unreviewed-proposal block for
            # the same reason pi_priority and funding posts do. Without this,
            # an agent with any unrelated pending proposal would silently skip
            # the handover message that migrated the conversation into the
            # private channel in the first place.
            is_private = (
                self._channel_visibility.get(post.channel) == VISIBILITY_COLLAB_PRIVATE
            )

            # A private channel whose refinement already converged on a recorded
            # revised proposal is closed for further discussion — the proposal
            # is now awaiting PI review. Don't keep refining it.
            if post.channel in self._finalized_private_channels:
                continue

            # PI-priority, funding, and private-channel posts bypass regular blocking
            if blocked_for_regular and not is_funding and not post.pi_priority and not is_private:
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

        # Daily post cap (E7a) — bypassed only by an ACTIONABLE PI-priority,
        # funding or collab_private candidate, i.e. one that survived the
        # availability filter above. Computing the bypass over the raw
        # interesting_posts list let a candidate the agent had already
        # replied to (active_threads / phase4_thread_ids) or that the
        # allowed-set rule excludes unlock an LLM call every turn while
        # capped, only for the post-LLM re-check below to reject the
        # result. _count_today_posts already excludes collab_private posts
        # from the count itself.
        has_bypass_candidate = any(
            p.pi_priority
            or self.message_log.is_funding_thread(p.post_id)
            or self._channel_visibility.get(p.channel) == VISIBILITY_COLLAB_PRIVATE
            for p in available_posts
        )
        if not has_bypass_candidate and today_posts >= settings.daily_post_cap:
            logger.debug(
                "[%s] Phase 5: Skipped (daily cap %d/%d)",
                agent.agent_id, today_posts, settings.daily_post_cap,
            )
            return

        # If blocked and no available posts to reply to, still allow Phase 5
        # so the agent can create funding collaboration posts (Option B)
        has_funding_interesting = any(
            self.message_log.is_funding_thread(p.post_id)
            for p in agent.state.interesting_posts
        )
        has_thread_foas = any(
            ts.foa_number for ts in agent.state.active_threads.values()
        )
        if not available_posts and blocked_for_regular and not has_funding_interesting and not has_thread_foas:
            logger.debug("[%s] Phase 5: Skipped (blocked, no funding/PI posts available)", agent.agent_id)
            return

        # Temporarily replace interesting_posts for prompt building
        original_posts = agent.state.interesting_posts
        agent.state.interesting_posts = available_posts
        try:
            # Build prompt — include agent's recent posts for dedup
            recent_entries = self.message_log.get_agent_top_level_posts(agent.agent_id, limit=10)
            recent_posts = [
                {"channel": e.channel, "content_snippet": e.content[:150]}
                for e in recent_entries
            ]

            # Pre-load cached FOA text for funding posts so Phase 5 has full context
            foa_contexts: dict[str, str] = {}
            funding_thread_summaries: dict[str, str] = {}
            for post in available_posts:
                if post.foa_number:
                    foa_text = format_foa_for_prompt(post.foa_number)
                    if foa_text:
                        foa_contexts[post.post_id] = foa_text
                if self.message_log.is_funding_thread(post.post_id):
                    summary = summarize_funding_thread(
                        self.message_log, post.post_id, viewer_agent_id=agent.agent_id,
                    )
                    if not summary.is_empty():
                        funding_thread_summaries[post.post_id] = format_funding_thread_summary(summary)

            # Also pre-load FOAs from active/closed threads for Option B
            # (starting a new funding collab from a previously seen FOA)
            thread_foa_contexts: dict[str, str] = {}
            for ts in agent.state.active_threads.values():
                if ts.foa_number and ts.foa_number not in thread_foa_contexts:
                    foa_text = format_foa_for_prompt(ts.foa_number)
                    if foa_text:
                        thread_foa_contexts[ts.foa_number] = foa_text

            # Resolve the visibility context for the prompt. Phase 5 now also drives
            # collab_private refinement (flat follow-ups). When the agent's only
            # actionable posts are in a private channel, build the prompt in that
            # channel's context so the Private Channel Rules — including the
            # converge-on-a-revised-:memo:-Summary instruction — are injected and the
            # dedup context is filtered for that visibility. Mixed/empty cases stay
            # public (the default for new public posts).
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

            # funding_only strips the prompt to funding actions. Only apply when
            # the agent is actually funding-restricted — if any available post is
            # non-funding (e.g., a private-channel handover that also bypasses
            # blocking), the LLM needs the regular reply path.
            has_available_non_funding = any(
                not self.message_log.is_funding_thread(p.post_id)
                for p in available_posts
            )
            funding_only = blocked_for_regular and not has_available_non_funding

            system_prompt, messages = agent.build_phase5_prompt(
                recent_posts=recent_posts,
                foa_contexts=foa_contexts,
                thread_foa_contexts=thread_foa_contexts,
                prior_threads=prior_threads,
                funding_only=funding_only,
                funding_thread_summaries=funding_thread_summaries,
                visibility=current_visibility,
                channel_id=private_channel_id,
            )
        finally:
            # Restore unconditionally — a raise anywhere above must not
            # permanently narrow interesting_posts to this turn's filtered
            # subset. See E7d.
            agent.state.interesting_posts = original_posts

        # Mid-turn rate gate (E6-1) — see _phase4_reply_threads for the same
        # check and its rationale. Phase 5 runs after Phase 4 in the same
        # turn, so it needs its own check even though _turn_eligible already
        # passed at turn selection.
        if not self._within_rate_limit(agent, time.time()):
            logger.info("[%s] Phase 5: rate-limited, skipping this turn", agent.agent_id)
            return

        agent.record_api_call()
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                model=settings.llm_agent_model_opus,
                max_tokens=1000,
                log_meta={"agent_id": agent.agent_id, "phase": "new_post"},
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
            # post something anyway — defaulting to "new_post" here is what lets
            # a malformed action dict fall through into posting to #general with
            # an empty post_type instead of being rejected outright.
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

            # Real action — reset skip backoff
            agent.state.consecutive_phase5_skips = 0
            agent.state.last_phase5_action_time = time.time()

            channel = action_data.get("channel", "general").lstrip("#")
            target_post_id = action_data.get("target_post_id")
            post_type = action_data.get("post_type", "")

            # A reply's channel is the target post's real channel, never the
            # LLM's free-form field — trusting the LLM here let a reply
            # "targeting" a collab_private post declare "general" and post
            # publicly while still threading onto the private root's ts. Falls
            # back to the LLM's value only if the target isn't in the log
            # (e.g. windowed out) — better to trust it than refuse to reply.
            # See COR-9b.
            if action == "reply" and target_post_id:
                target_entry = self.message_log.get_entry(target_post_id)
                if target_entry:
                    channel = target_entry.channel

            # Turn-taking enforcement for private channels: reject any action
            # that would post back-to-back with our previous private-channel
            # message. Belt-and-braces — the available_posts pre-filter also
            # catches this for the "reply" path, but this gate covers new
            # top-level posts the LLM might propose.
            if (
                self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
                and self.message_log.get_last_bot_sender_in_channel(channel)
                == agent.agent_id
            ):
                logger.info(
                    "[%s] Phase 5: Rejecting back-to-back post in private #%s",
                    agent.agent_id, channel,
                )
                agent.state.consecutive_phase5_skips += 1
                return

            # If agent is blocked, only allow bypass-eligible actions: funding
            # replies, funding posts, or replies to a post in a collab_private
            # channel (the PI has explicitly engaged that refinement).
            if blocked_for_regular:
                is_funding_reply = (
                    action == "reply" and target_post_id
                    and self.message_log.is_funding_thread(target_post_id)
                )
                is_funding_post = post_type == "funding_collab"
                is_private_reply = False
                if action == "reply" and target_post_id:
                    target_entry = self.message_log.get_entry(target_post_id)
                    if target_entry and (
                        self._channel_visibility.get(target_entry.channel)
                        == VISIBILITY_COLLAB_PRIVATE
                    ):
                        is_private_reply = True
                if not is_funding_reply and not is_funding_post and not is_private_reply:
                    logger.info(
                        "[%s] Phase 5: Blocked non-funding action while proposals pending",
                        agent.agent_id,
                    )
                    return

            # The daily cap was bypassed above because a
            # bypass-eligible candidate existed (E7a). Re-check against the
            # action the LLM actually chose, exactly as blocked_for_regular
            # does above — otherwise one funding/PI-priority candidate in
            # interesting_posts makes the cap unenforceable for the rest of
            # the day. See red-team M4.
            if today_posts >= settings.daily_post_cap:
                cap_exempt = (
                    post_type == "funding_collab"
                    or self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
                    or (
                        action == "reply" and target_post_id
                        and (
                            self.message_log.is_funding_thread(target_post_id)
                            or any(
                                p.post_id == target_post_id and p.pi_priority
                                for p in original_posts
                            )
                        )
                    )
                )
                if not cap_exempt:
                    logger.info(
                        "[%s] Phase 5: daily cap %d/%d — the chosen action is not "
                        "bypass-eligible", agent.agent_id, today_posts, settings.daily_post_cap,
                    )
                    return

            # Retroactively add channel to the LLM log entry (unknown at call time)
            if self._llm_log_buffer:
                self._llm_log_buffer[-1]["channel"] = channel

            # Authorship guard (issue #29) — one gate for both the reply and
            # new-post branches below. Runs on the ORIGINAL draft, BEFORE the
            # cohort-tag strip: stripping a disallowed co-author's @tag first
            # would blind the tagged-co-author check to exactly the
            # fabrication it exists to catch (audit finding I1). The gate is
            # read-only, so the swap is safe.
            authorship_reason = self._reject_ungrounded_authorship(agent, message_text)
            if authorship_reason:
                logger.warning(
                    "[%s] Phase 5: Rejected draft — %s", agent.agent_id, authorship_reason,
                )
                agent.state.consecutive_phase5_skips += 1
                return

            # Cross-cohort mention stripping now happens in _post_message, which
            # covers every outbound path instead of only this one. Phase 5 still
            # needs the *cleaned* text locally, though: the tagged_agent decision
            # and _check_private_channel_outcome below both read message_text.
            message_text = self._strip_disallowed_tags(message_text, agent)

            if action == "reply" and target_post_id:
                # Enforce thread participation rules
                allowed = self.message_log.get_thread_allowed_agents(target_post_id)
                if allowed and agent.agent_id not in allowed:
                    logger.info(
                        "[%s] Phase 5: Blocked reply to %s — not in allowed set %s",
                        agent.agent_id, target_post_id, allowed,
                    )
                    return

                # Funding-thread draft validators (atomic spin-off + no-ack rules)
                if self.message_log.is_funding_thread(target_post_id):
                    if is_announcement_only_funding_reply(message_text):
                        logger.info(
                            "[%s] Phase 5: Rejected announcement-only funding reply to %s",
                            agent.agent_id, target_post_id,
                        )
                        agent.state.consecutive_phase5_skips += 1
                        return
                    if is_acknowledgment_only_funding_reply(message_text):
                        logger.info(
                            "[%s] Phase 5: Rejected acknowledgment-only funding reply to %s",
                            agent.agent_id, target_post_id,
                        )
                        agent.state.consecutive_phase5_skips += 1
                        return

                # In a collab_private channel, the whole channel IS the
                # discussion — post flat (no thread_ts) and don't create an
                # active_thread. The other agent will see this as a new
                # top-level post on its next Phase 2 scan and continue the
                # flat conversation. See specs/privacy-and-channel-visibility.md.
                is_private_channel = (
                    self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
                )

                if is_private_channel:
                    posted = await self._post_message(agent.agent_id, channel, message_text)
                    if not posted:
                        self._note_phase5_post_failure(agent, target_post_id)
                    else:
                        self._phase5_post_failure_counts.pop(
                            (agent.agent_id, target_post_id), None,
                        )
                        agent.message_count += 1
                        # Consume the interesting post (we acted on it) but do not
                        # create an active_thread — private channels don't thread.
                        agent.state.interesting_posts = [
                            p for p in agent.state.interesting_posts
                            if p.post_id != target_post_id
                        ]
                        logger.info(
                            "[%s] Phase 5: Posted flat follow-up to %s in private #%s",
                            agent.agent_id, target_post_id, channel,
                        )
                else:
                    # Reply to an interesting post → creates a new thread
                    posted = await self._post_message(
                        agent.agent_id, channel, message_text,
                        thread_ts=target_post_id,
                    )
                    if not posted:
                        self._note_phase5_post_failure(agent, target_post_id)
                    else:
                        self._phase5_post_failure_counts.pop(
                            (agent.agent_id, target_post_id), None,
                        )
                        agent.message_count += 1

                        # Move from interesting_posts to active_threads
                        agent.state.interesting_posts = [
                            p for p in agent.state.interesting_posts
                            if p.post_id != target_post_id
                        ]
                        # Determine the other agent from the original post
                        original_entry = self.message_log.get_entry(target_post_id)
                        other_id = original_entry.sender_agent_id if original_entry else None
                        if other_id:
                            # Carry FOA number from the PostRef if this is a funding post
                            post_foa = None
                            for p in original_posts:
                                if p.post_id == target_post_id:
                                    post_foa = p.foa_number
                                    break
                            agent.state.active_threads[target_post_id] = ThreadState(
                                thread_id=target_post_id,
                                channel=channel,
                                other_agent_id=other_id,
                                message_count=2,  # original + this reply
                                foa_number=post_foa,
                            )

                        logger.info(
                            "[%s] Phase 5: Replied to post %s in #%s",
                            agent.agent_id, target_post_id, channel,
                        )

            else:
                # New top-level post
                posted = await self._post_message(agent.agent_id, channel, message_text)
                if not posted:
                    # #20 I1: unlike the two reply branches above, there is no
                    # target_post_id/PostRef and no ThreadState here — each
                    # turn's "new post" is a fresh LLM decision with no
                    # persistent object across turns to hang a two-strike
                    # counter on, so there is nothing to drop or back off.
                    logger.info(
                        "[%s] Suppressed post in #%s — turn not counted, kept as "
                        "a DB-only row",
                        agent.agent_id, channel,
                    )
                else:
                    agent.message_count += 1

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

            # In a collab_private channel, a :memo: Summary + ✅ handshake
            # finalizes the refined proposal (the flat path has no
            # _check_thread_outcome). Runs for either action since both post flat.
            # Gated on `posted`: a suppressed post (empty after strip, an
            # authorship rejection, or COR-1b's connected-client-failed path)
            # never reached the channel, so there is nothing to finalize against.
            # See COR-1d.
            if (
                posted
                and message_text
                and self._channel_visibility.get(channel) == VISIBILITY_COLLAB_PRIVATE
            ):
                await self._check_private_channel_outcome(agent, channel, message_text)

        except Exception as exc:
            logger.error("[%s] Phase 5 failed: %s", agent.agent_id, exc)

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
        cleaned = _DISALLOWED_TAG_STRIP_RE.sub(_repl, message_text)
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

    def _reject_ungrounded_authorship(self, agent: Agent, text: str | None) -> str | None:
        """Return a rejection reason if ``text`` makes an authorship claim the
        publication records cannot back; None when the draft is clean.

        Ground truth is the publications table (loaded per roster sync into
        ``_agent_publications``) unioned with the agent's profile-parsed DOIs.
        Tagged bots are resolved through ``_bot_name_to_id`` and their labs'
        records are enforced on co-authorship claims — the issue-#29 origin
        message fails HERE, not on the own-DOI check. Fails closed on every
        unverifiable claim.
        """
        if not text:
            return None
        own_db = self._agent_publications.get(agent.agent_id)
        profile_dois = agent.own_publication_dois
        own = LabPublicationRecord(
            dois=(own_db.dois if own_db else set()) | profile_dois,
            has_records=bool(own_db) or bool(profile_dois),
        )
        tagged: dict[str, LabPublicationRecord] = {}
        for m in BOT_TAG_RE.finditer(text):
            bot_name = m.group(1)
            target_id = self._bot_name_to_id.get(bot_name.lower())
            if target_id is None or target_id == agent.agent_id:
                continue
            tagged[bot_name] = self._lab_record_for(target_id)

        # Prose-named labs (audit finding I4): "co-authored ... with the Good
        # lab" dodges the @-tag scan above. Resolve capitalized "<Name> lab"
        # mentions through the roster (PI last name or agent_id) and enforce
        # their records exactly like a tagged bot's. Deliberately
        # conservative: an unresolved name is left alone — the roster is the
        # only ground truth available, and gating arbitrary capitalized words
        # would block legit mentions of outside labs. Same-surname collisions
        # (wu vs pwu) get the benefit of the doubt: the union record stands
        # if ANY namesake lab can back the claim.
        name_to_ids: dict[str, set[str]] = {}
        for aid, roster_agent in self.agents.items():
            name_to_ids.setdefault(aid.lower(), set()).add(aid)
            pi_name = (roster_agent.pi_name or "").strip()
            if pi_name:
                name_to_ids.setdefault(pi_name.split()[-1].lower(), set()).add(aid)
        for m in _PROSE_LAB_RE.finditer(normalize_claim_text(text)):
            name = m.group(1)
            if name.lower() in _PROSE_LAB_STOPWORDS:
                continue
            candidate_ids = name_to_ids.get(name.lower(), set()) - {agent.agent_id}
            if not candidate_ids:
                continue
            merged = LabPublicationRecord()
            bot_names: list[str] = []
            for cid in sorted(candidate_ids):
                rec = self._lab_record_for(cid)
                merged.dois |= rec.dois
                merged.has_records = merged.has_records or rec.has_records
                roster_agent = self.agents.get(cid)
                bot_names.append(roster_agent.bot_name if roster_agent else cid)
            tagged.setdefault("/".join(bot_names), merged)

        verdict = validate_authorship_claims(text, own, tagged)
        return None if verdict.ok else verdict.reason

    def _lab_record_for(self, agent_id: str) -> LabPublicationRecord:
        """A lab's ground truth: publications-table rows ∪ profile DOIs."""
        rec = self._agent_publications.get(agent_id)
        roster_agent = self.agents.get(agent_id)
        profile_dois = roster_agent.own_publication_dois if roster_agent else set()
        return LabPublicationRecord(
            dois=(rec.dois if rec else set()) | profile_dois,
            has_records=bool(rec) or bool(profile_dois),
        )

    def _parse_phase5_response(self, response: str) -> tuple[dict | None, str | None]:
        """Parse Phase 5 response into (json_data, message_text).

        Expects JSON block + <slack_message> tags.  Uses the LAST JSON code
        block so that if the LLM revises its decision mid-response the final
        action wins.  Requires <slack_message> tags for the message body —
        raw text after the JSON block is never used (prevents reasoning leakage).
        """
        data = None
        try:
            # Find the LAST ```json``` block (LLM may revise mid-response)
            json_matches = list(
                re.finditer(r"```json\s*\n(.*?)\n```", response, re.DOTALL)
            )
            if json_matches:
                data = json.loads(json_matches[-1].group(1))
            else:
                # Try finding raw JSON
                json_start = response.find("{")
                json_end = response.find("}", json_start) + 1 if json_start >= 0 else -1
                if json_start >= 0 and json_end > json_start:
                    data = json.loads(response[json_start:json_end])
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
                        # Resolve agent_id by bot name, then by Slack uid. The uid
                        # fallback is what actually attributes service bots: Slack
                        # usually omits `username` on grantbot's posts, so the name
                        # lookup misses and the row would persist with a NULL
                        # agent_id — which _entry_allowed fails closed on, hiding
                        # the funding post from every gated agent.
                        bot_agent_id = self.message_log._bot_name_to_id.get(
                            bot_name.lower()
                        ) or self._service_bot_uids.get(user_id)
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
                            # branch ingests are another workspace bot's posts, i.e.
                            # GrantBot's funding posts, whose threads are open to all
                            # agents. Slack-origin ⇒ canonical id *is* the Slack ts,
                            # so the thread parent needs no translation.
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

                    # Check if PI message references a proposal (clears pending block).
                    # pi_agent_ids (computed above at the human-message branch) is the
                    # authorization: only agents THIS Slack user is a registered PI/
                    # delegate for. See COR-5.
                    await self._check_pi_proposal_review(
                        entry, authorized_agent_ids=set(pi_agent_ids),
                    )

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

                      # PI tagged their bot in a top-level post or reply —
                      # either the literal "@BotName" form or a real Slack
                      # <@Uxxx> mention (what a human typing in Slack actually
                      # produces once autocomplete fires). See COR-8.
                      tagged_agent_ids = {
                          self._bot_name_to_id.get(m, m)
                          for m in extract_bot_mentions(
                              msg.get("text", ""), self._bot_uid_map(),
                          )
                      }
                      if pi_agent_id in tagged_agent_ids:
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
        from sqlalchemy import and_ as sa_and
        from sqlalchemy import or_ as sa_or
        from sqlalchemy import select as sa_select
        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(AgentMessage)
                    .where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        sa_or(
                            # Cursor over created_at (the DB server's clock), with a
                            # lookback so a row that committed after the cursor
                            # advanced past its stamp is still caught (H2). Re-scanned
                            # rows are free — the log dedup below skips anything
                            # already ingested. See PI_INBOX_LOOKBACK_S (H2 + R3).
                            AgentMessage.created_at > self._pi_inbox_cursor - PI_INBOX_LOOKBACK,
                            # A row explicitly marked 'pending' at write time (RC-2)
                            # is fetched regardless of how far behind the cursor it
                            # is — this is exactly the recovery path for a PI
                            # message written while agent-run was down, which would
                            # otherwise age past the lookback window before the
                            # process ever came back to see it.
                            sa_and(
                                AgentMessage.is_bot.is_(False),
                                AgentMessage.pi_inbound_state == PI_INBOUND_PENDING,
                            ),
                        ),
                    )
                    # Ingest in the DB's arrival order; posted_at remains the
                    # ordering key for the conversation content itself.
                    .order_by(AgentMessage.created_at.asc())
                )).scalars().all()
        except Exception as exc:
            logger.warning("Inbound DB poll failed: %s", exc)
            return

        for r in rows:
            # COR-10(3): dedup for a PI row reads the durable handled-marker,
            # not the log entry, because the append now runs ahead of the
            # handler. NULL means "no inbound poller has claimed this row",
            # which is every pre-0029 row and every row _poll_channels appended
            # itself (this query has no origin predicate) — for those, dedup
            # falls back to log presence exactly as before, so a database that
            # is migrated but running older code, or code rolled back over a
            # migrated database, behaves as it does today. Bot rows never carry
            # the marker at all. See PI_INBOUND_INGESTED.
            state = r.pi_inbound_state if not r.is_bot else None
            in_log = bool(r.message_ts and self.message_log.get_entry(r.message_ts))
            if (
                not r.message_ts
                or state == PI_INBOUND_HANDLED
                or (state is None and in_log)
            ):
                # Already known (the handler confirmed it, or — under the NULL
                # fallback — the engine itself appended and flushed it, or a
                # prior poll ingested it) — skip re-processing, but the cursor
                # still advances: this row is fully accounted for.
                if r.created_at and r.created_at > self._pi_inbox_cursor:
                    self._pi_inbox_cursor = r.created_at
                continue
            if r.message_ts in self._dead_thread_ids or (
                r.thread_ts and r.thread_ts in self._dead_thread_ids
            ):
                # Tombstoned: _evict_dead_thread already purged this thread from
                # the log, so the dedup check above no longer catches it and
                # this row would otherwise be re-ingested every tick within the
                # lookback window. Re-appending it (and, for a PI row, running
                # _handle_pi_inbound_entry) would re-hydrate and reopen a
                # thread whose Slack parent is gone — a resurrection loop. See
                # COR-1c fix round 1 (C1). Nothing will ever process this row,
                # so the cursor advances past it too.
                if r.created_at and r.created_at > self._pi_inbox_cursor:
                    self._pi_inbox_cursor = r.created_at
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
                sender_user_id=r.sender_user_id,
            )
            if not r.is_bot:
                logger.info("PI (web) message in #%s: %.60s", entry.channel, entry.content[:60])
                # COR-10(3), ruled option (a): the append is what records the
                # PI's TEXT, so it happens BEFORE the handler and the cursor
                # advance happens after it — "apply side effects before ... the
                # cursor advance", with the row itself never at risk. The
                # marker, not the log entry, is what keeps the retry from
                # re-applying side effects, so appending first no longer
                # re-creates the defect COR-10(3) was filed for. This supersedes
                # Decision D25 (which kept the row but lost the triggers) and
                # this session's c4de842 ordering (which ran the handler first
                # and so lost the row outright once it aged past
                # PI_INBOX_LOOKBACK_S). Ruling:
                # docs/plans/2026-09-04-decisions/task-7.md.
                if not in_log:
                    self.message_log.append(entry)
                if state is None:
                    await self._mark_pi_inbound_state(r.message_ts, PI_INBOUND_INGESTED)
                try:
                    await self._handle_pi_inbound_entry(entry)
                except Exception as exc:
                    logger.error(
                        "[%s] Failed to apply PI inbound side effects for %s: %s",
                        entry.channel, entry.thread_ts or entry.ts, exc,
                    )
                    # 'ingested' is durable, so the next poll re-runs the
                    # handler for as long as the row stays inside
                    # PI_INBOX_LOOKBACK_S; the cursor stays put so it is
                    # re-scanned. Past that window the triggers are lost (D25's
                    # trade) but the PI's message is in the log either way.
                    # At-least-once: a retry can repeat a non-idempotent side
                    # effect (e.g. a DM) that the failed attempt already ran.
                    continue
                await self._mark_pi_inbound_state(r.message_ts, PI_INBOUND_HANDLED)
            else:
                logger.info("External bot message in #%s: %.60s", entry.channel, entry.content[:60])
                self.message_log.append(entry)
            if r.created_at and r.created_at > self._pi_inbox_cursor:
                self._pi_inbox_cursor = r.created_at

    async def _mark_pi_inbound_state(self, message_ts: str, state: str) -> None:
        """Persist the inbound poller's handled-marker for one PI row.

        The only writer of ``agent_messages.pi_inbound_state``, and only for
        ``is_bot=False`` rows, so NULL keeps meaning "no inbound poller has
        claimed this row". Each call is its own short transaction because the
        ordering is the whole point: ``'ingested'`` must be committed *before*
        ``_handle_pi_inbound_entry`` runs or its durability is fictional, and
        ``'handled'`` as soon as it returns. Keyed on
        ``(simulation_run_id, message_ts)``, which is the table's own unique
        constraint.

        A write failure is logged and swallowed. ``_run_main_loop`` does not
        guard its pollers, and the cost of a lost marker write is bounded: a
        lost ``'handled'`` means one at-least-once retry of a handler that
        already succeeded, and a lost ``'ingested'`` leaves the row reading NULL
        — which, now that the entry is in the log, is the pre-0029 skip. Neither
        can lose the PI's text.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import update as sa_update
        try:
            async with self.session_factory() as db:
                await db.execute(
                    sa_update(AgentMessage)
                    .where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        AgentMessage.message_ts == message_ts,
                    )
                    .values(pi_inbound_state=state)
                )
                await db.commit()
        except Exception as exc:
            logger.warning(
                "Inbound marker write failed for %s (%s): %s", message_ts, state, exc
            )

    async def _agent_ids_owned_by_user(self, user_id: uuid.UUID | None) -> set[str]:
        """Agents this user actually owns or represents (RC-1 / #20 COR-5).

        The DB/web (and now e-mail) inbound path has a real sender identity
        since migration 0030 (``agent_messages.sender_user_id``), so ownership
        can be resolved the same way the Slack map already does — registry
        owner (``AgentRegistry.user_id``) union delegate
        (``AgentDelegate.agent_registry_id -> AgentRegistry.id`` for this
        user) — instead of trusting whoever else happens to have posted in the
        same thread. Returns the empty set for ``None`` (no sender recorded)
        or on any DB failure: fail closed, since an empty set makes every
        ownership-gated side effect in ``_handle_pi_inbound_entry`` a no-op
        rather than a wrong grant.
        """
        if not user_id or not self.session_factory:
            return set()
        from sqlalchemy import or_ as sa_or
        from sqlalchemy import select as sa_select

        from src.models import AgentRegistry
        from src.models.delegate import AgentDelegate
        try:
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(AgentRegistry.agent_id)
                    .outerjoin(
                        AgentDelegate, AgentDelegate.agent_registry_id == AgentRegistry.id,
                    )
                    .where(
                        sa_or(
                            AgentRegistry.user_id == user_id,
                            AgentDelegate.user_id == user_id,
                        )
                    )
                    .distinct()
                )).scalars().all()
            return set(rows)
        except Exception as exc:
            logger.warning("Failed to resolve agents owned by user %s: %s", user_id, exc)
            return set()

    async def _handle_pi_inbound_entry(self, entry: LogEntry) -> None:
        """Apply PI-message side effects, gated to agents the sender owns.

        ``owned`` is the set of agent_ids ``entry.sender_user_id`` actually
        owns or represents (registry owner or delegate — see
        ``_agent_ids_owned_by_user``). Every side effect below — clearing a
        pending-proposal block, reopening a closed thread, setting
        ``pi_context``/``has_pending_reply``/``has_pi_directive`` on an active
        thread, and the ``@bot`` tag route — is restricted to that set. Before
        migration 0030 this used to trust the thread's own participants
        instead (any agent that had ever posted in the thread), which let PI A
        drive PI B's agent in a thread the two agents share. See #20 COR-5 /
        docs/plans/2026-09-08-audit-fixes.md RC-1.

        A row with ``sender_user_id IS NULL`` — a bot-authored row (never
        reaches here; callers only invoke this for ``is_bot=False`` rows), a
        pre-0030 row, or a since-deleted user — gets NO ownership-gated side
        effect at all and one WARNING naming the row. It is still appended to
        the log by the caller, so the text is never lost; only the automatic
        reactions to it are withheld, since there is nothing to authorize them
        against.
        """
        # Tombstoned: this thread's Slack parent is confirmed gone
        # (_evict_dead_thread). _poll_inbound_from_db already skips tombstoned
        # rows before calling here, but this guard is defense-in-depth against
        # any other caller — no hydrate, no reopen of a thread whose history
        # was purged from the log. See COR-1c fix round 1 (C1).
        if entry.ts in self._dead_thread_ids or (
            entry.thread_ts and entry.thread_ts in self._dead_thread_ids
        ):
            return

        if entry.sender_user_id is None:
            logger.warning(
                "PI inbound row %s (thread %s) has no sender_user_id — skipping "
                "ownership-gated side effects (proposal-review clear, reopen, "
                "pi_context, @bot tag)",
                entry.ts, entry.thread_ts,
            )
            return

        owned = await self._agent_ids_owned_by_user(entry.sender_user_id)

        # Clears any pending proposal on this thread, but only for an agent
        # that BOTH the sender owns AND actually participates in this
        # thread — the second clause keeps a PI's message in one thread from
        # reaching into an unrelated proposal their agent happens to also have
        # pending elsewhere.
        thread_participants: set[str] = set()
        if entry.thread_ts:
            thread_participants = {
                e.sender_agent_id
                for e in self.message_log.get_thread_history(entry.thread_ts)
                if e.sender_agent_id
            }
        await self._check_pi_proposal_review(
            entry, authorized_agent_ids=owned & thread_participants,
        )

        thread_ts = entry.thread_ts
        if thread_ts:
            # Reopen a closed thread for its participants — restricted to the
            # sender's own agent(s).
            if thread_ts in self._closed_thread_ids:
                # Old closed threads may have been windowed out of the log at
                # startup (B2) — pull the history back so participants resolve.
                await self._hydrate_thread_from_db(thread_ts)
                history = self.message_log.get_thread_history(thread_ts)
                participants = [
                    h.sender_agent_id for h in history
                    if h.sender_agent_id and h.sender_agent_id in self.agents
                    and h.sender_agent_id in owned
                ]
                if participants:
                    await self._reopen_thread(participants[0], thread_ts, entry)
            else:
                # Active thread → treat the PI message as authoritative context
                # for the sender's own agent(s) only.
                for agent in self.agents.values():
                    if agent.agent_id not in owned:
                        continue
                    thread = agent.state.active_threads.get(thread_ts)
                    if thread:
                        thread.pi_context = entry.content
                        thread.has_pending_reply = True
                        agent.state.has_pi_directive = True

        # @bot tag → route to the tagged agent, only if the sender owns it
        # (same as the Slack path). Keeps F1's forged-injection concern closed.
        tagged_id = self.message_log._extract_tagged_agent(entry.content)
        if (
            tagged_id and tagged_id in self.agents and tagged_id in owned
            and self._pi_handler
        ):
            self.agents[tagged_id].state.has_pi_directive = True
            await self._pi_handler.handle_channel_tag(tagged_id, entry)

    async def _check_pi_proposal_review(
        self, entry: LogEntry, *, authorized_agent_ids: set[str],
    ) -> None:
        """Check if a PI message clears a pending proposal for an owned agent.

        Only clears proposals belonging to an agent in `authorized_agent_ids`
        — the set of agents the actual sender is verified to own/represent.
        Without this, any message landing in the right thread_id cleared the
        block for whichever agent was waiting on it, regardless of who sent
        it — including another lab's PI, or (on the Slack channel poller) any
        workspace human at all. See COR-5.
        """
        thread_ts = entry.thread_ts
        if not thread_ts or not authorized_agent_ids:
            return

        # list(...), not a bare dict-values iteration: this method is now
        # async with an await inside the loop body
        # (_persist_implicit_proposal_review), and _sync_roster_from_db can
        # mutate self.agents from the same main-loop task. No live "dict
        # changed size during iteration" exists today, but the loop is now
        # interruptible where it previously was not — a free hedge.
        for agent in list(self.agents.values()):
            if agent.agent_id not in authorized_agent_ids:
                continue
            for proposal in agent.state.pending_proposals:
                if proposal.thread_id == thread_ts and not proposal.reviewed:
                    proposal.reviewed = True
                    logger.info(
                        "[%s] Proposal in thread %s reviewed by PI",
                        agent.agent_id, thread_ts,
                    )
                    await self._persist_implicit_proposal_review(
                        agent.agent_id, proposal.thread_decision_id,
                    )

    async def _persist_implicit_proposal_review(
        self, agent_id: str, thread_decision_id: uuid.UUID | None,
    ) -> None:
        """Best-effort ProposalReview row for a review cleared by thread
        engagement rather than the explicit review form.

        `thread_decision_id` comes straight off the `ProposalRef` (the same
        unified review key `_sync_proposal_reviews_from_db` uses — COR-13),
        not a fresh lookup by thread_id: after a re-propose cycle mints a new
        ThreadDecision for the same thread_id, re-deriving by thread_id could
        pick the wrong (stale) decision. `None` (a proposal whose ThreadDecision
        write never landed) means in-memory-only — nothing to persist against.

        An agent with no linked ``AgentRegistry.user_id`` gets the record on
        ``thread_decisions.pi_engaged_at`` instead of a review row — see the
        branch below and docs/plans/2026-09-04-decisions/task-8.md.

        rating=-1 is a dedicated sentinel — never confused with the explicit
        1-4 star rating or the reopen-with-guidance sentinel rating=0 (see
        _sync_proposal_reviews_from_db) — so this can never accidentally
        trigger a thread reopen. Skips the insert entirely if a review
        already exists for this (thread_decision, agent) pair (the unique
        constraint allows only one, and an explicit rating or reopen-with-
        guidance submission must not be clobbered). Never raises — this is
        called from pollers that must keep running on a DB hiccup; the
        in-memory `reviewed` flag (set by the caller before this runs) is
        the source of truth for the current process regardless.
        """
        if not self.session_factory or thread_decision_id is None:
            return
        try:
            from sqlalchemy import select as sa_select
            from sqlalchemy import update as sa_update
            from sqlalchemy.exc import IntegrityError

            from src.models import AgentRegistry

            async with self.session_factory() as db:
                user_id = (await db.execute(
                    sa_select(AgentRegistry.user_id).where(AgentRegistry.agent_id == agent_id)
                )).scalar_one_or_none()
                if not user_id:
                    # #20 COR-5. ProposalReview.user_id is NOT NULL, so a
                    # backfilled/bulk-provisioned agent — or one whose PI
                    # deleted their account — can never get a review row here.
                    # Making that column nullable was the obvious fix and was
                    # rejected: it is ForeignKey("users.id",
                    # ondelete="CASCADE") (models/agent_registry.py:82-84), so
                    # deleting a PI would destroy the engine's own
                    # block-clearing markers and every proposal that PI had
                    # engaged with would re-block on the next restart. The
                    # carrier is thread_decisions.pi_engaged_at instead — a
                    # timestamp on the thread's own decision row, which
                    # cascades from simulation_runs only. Ruling and rejected
                    # alternatives: docs/plans/2026-09-04-decisions/task-8.md.
                    #
                    # The `IS NULL` predicate makes this write-once: a later
                    # engagement must not move the timestamp off the one that
                    # actually cleared the block, and a repeat call is then a
                    # no-op rather than a rewrite. Read back by
                    # _rebuild_agent_state and _rebuild_one_agent_state.
                    await db.execute(
                        sa_update(ThreadDecision)
                        .where(
                            ThreadDecision.id == thread_decision_id,
                            ThreadDecision.pi_engaged_at.is_(None),
                        )
                        .values(pi_engaged_at=datetime.now(UTC))
                    )
                    await db.commit()
                    # INFO, not WARNING: the review is persisted now, so a
                    # warning would be crying wolf. The operator still wants
                    # the line, because this carrier is invisible to every
                    # proposal_reviews reader — the dashboard, the review
                    # e-mail, the digest and the admin review count all key on
                    # review rows, so this agent shows as unreviewed there.
                    logger.info(
                        "[%s] No linked PI user — implicit review for "
                        "thread_decision %s recorded as "
                        "thread_decisions.pi_engaged_at, not a proposal_reviews row",
                        agent_id, thread_decision_id,
                    )
                    return

                existing = (await db.execute(
                    sa_select(ProposalReview.id).where(
                        ProposalReview.thread_decision_id == thread_decision_id,
                        ProposalReview.agent_id == agent_id,
                    )
                )).scalar_one_or_none()
                if existing:
                    return  # Already reviewed by something — never overwrite it.

                db.add(ProposalReview(
                    thread_decision_id=thread_decision_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    rating=-1,
                    submitted_via="engine",
                ))
                try:
                    await db.commit()
                except IntegrityError:
                    # Lost a race against a concurrent review write for the
                    # same (thread_decision_id, agent_id) pair.
                    await db.rollback()
        except Exception as exc:
            logger.warning(
                "[%s] Failed to persist implicit proposal review for decision %s: %s",
                agent_id, thread_decision_id, exc,
            )

    async def _reopen_thread(self, agent_id: str, thread_ts: str, pi_entry: LogEntry) -> None:
        """Reopen a closed thread when a PI posts in it."""
        self._closed_thread_ids.discard(thread_ts)
        await self._mark_thread_decisions_reopened(thread_ts)
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

    async def _mark_thread_decisions_reopened(self, thread_id: str) -> None:
        """Stamp reopened_at on every ThreadDecision row for this thread.

        Durable counterpart to discarding thread_id from _closed_thread_ids /
        _db_reopened_thread_ids: without this, _rebuild_agent_state has no way
        to know a thread was reopened after its decision, and re-closes it on
        every restart — which is what forced the reopen mechanisms to redo
        their whole side effect (a fresh synthetic PI-guidance row, a fresh
        reply budget) every restart, forever. Best-effort: never raises,
        matching _close_thread's own DB-write error handling. See COR-13.
        """
        if not self.session_factory:
            return
        try:
            from sqlalchemy import select as sa_select
            async with self.session_factory() as db:
                rows = (await db.execute(
                    sa_select(ThreadDecision).where(ThreadDecision.thread_id == thread_id)
                )).scalars().all()
                if not rows:
                    return
                now = datetime.now(UTC)
                changed = False
                for row in rows:
                    if row.reopened_at is None:
                        row.reopened_at = now
                        changed = True
                if changed:
                    await db.commit()
        except Exception as exc:
            logger.warning("Failed to mark thread %s reopened: %s", thread_id, exc)

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
                # slack_sdk re-raises non-HTTP transport failures (socket/SSL/
                # DNS errors) unchanged — _call_with_retry only catches
                # SlackApiError (slack_client.py:310-341) — so an unguarded
                # call here kills the whole simulation on one flaky network
                # blip. Both sibling pollers already guard per-item; this one
                # didn't. See COR-10(1).
                try:
                    messages = client.poll_dm_messages(pi_slack_id, oldest=oldest)
                except Exception as exc:
                    logger.error("[%s] Failed to poll PI DMs: %s", agent_id, exc)
                    continue

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
        """Start the DM poller's window past existing inbound DMs on startup.

        Seeds only the cursor (max created_at — the DB server's clock, see
        R3), which bounds the query window for performance. It no longer also
        seeds ``_pi_dm_seen`` from the DB (RC-2): dedup against a row already
        processed is now the durable ``handled_at`` column, which — unlike the
        in-memory seen-set — survives a restart, so an unhandled DM older than
        the window is still found by ``_poll_pi_dms_from_db``'s ``handled_at
        IS NULL`` clause instead of being silently skipped. ``_pi_dm_seen``
        remains as in-process dedup only (populated as rows are processed,
        within one process's lifetime).
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
        except Exception as exc:
            logger.warning("PI DM cursor seed failed: %s", exc)

    async def _mark_pi_dm_handled(self, dm_id: uuid.UUID) -> None:
        """Durably mark one pi_dm_messages row as handled (RC-2).

        Set once, after ``PIHandler.handle_dm`` returns OR after a handler
        exception is logged — one attempt, unlike the channel path's two-state
        INGESTED->HANDLED marker: a DM has no thread-reopen/tag side effect
        whose retry would duplicate a Slack post, so there is nothing extra a
        second state would protect. Best-effort like
        ``_mark_pi_inbound_state``: a failed write here just means one
        at-least-once retry of a handler that may have already run, which
        ``PIHandler.handle_dm`` must already tolerate for the same reason the
        channel path's callers do.
        """
        if not self.session_factory or not self.simulation_run_id:
            return
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        from sqlalchemy import update as sa_update

        from src.models import PiDmMessage
        try:
            async with self.session_factory() as db:
                await db.execute(
                    sa_update(PiDmMessage)
                    .where(PiDmMessage.id == dm_id)
                    .values(handled_at=_datetime.now(_UTC))
                )
                await db.commit()
        except Exception as exc:
            logger.warning("PI DM handled-marker write failed for %s: %s", dm_id, exc)

    async def _poll_pi_dms_from_db(self) -> None:
        """Process inbound PI DMs recorded in the DB (Slack or web-originated).

        The single processor for PI DMs: reads new inbound pi_dm_messages rows
        and runs each through PIHandler.handle_dm (classify → standing
        instruction / feedback / question), then flips has_pi_directive so
        Phase 5 runs. Works with Slack off. See specs/local-db-conversations.md.

        RC-2: dedup is keyed on the durable ``handled_at`` column, not on
        presence in the in-memory ``_pi_dm_seen`` set or on the lookback
        window alone — both reset to nothing on every restart, which is
        exactly how a DM written while ``agent-run`` was down used to lose its
        side effects forever (the cursor jumps past it at startup, and the
        window never reaches back far enough once enough time has passed).
        The query still ORs in the existing lookback window, mirroring the
        channel poller's own 'pending' OR clause, so a late-committing row is
        still caught even though its ``handled_at`` write hasn't landed yet
        (H2).
        """
        if not self._pi_handler or not self.session_factory or not self.simulation_run_id:
            return
        from sqlalchemy import or_ as sa_or
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
                        sa_or(
                            # created_at, not posted_at, so the window doesn't
                            # depend on the writing process's clock (R3).
                            PiDmMessage.created_at > floor,
                            # The durable marker (RC-2): an unhandled row is
                            # always re-fetched, however far behind the
                            # cursor it has fallen — this is the recovery
                            # path for a DM written while agent-run was down.
                            PiDmMessage.handled_at.is_(None),
                        ),
                    )
                    .order_by(PiDmMessage.created_at.asc())
                )).scalars().all()
        except Exception as exc:
            logger.warning("PI DM inbox poll failed: %s", exc)
            return

        for r in rows:
            if r.created_at and r.created_at > self._pi_dm_cursor:
                self._pi_dm_cursor = r.created_at
            if r.handled_at is not None:
                continue  # durable marker — already processed
            if r.ts and r.ts in self._pi_dm_seen:
                continue  # in-process dedup (same tick/lookback re-scan)
            if r.agent_id not in self.agents:
                continue
            if r.ts:
                self._pi_dm_seen[r.ts] = r.created_at or EPOCH_UTC
            try:
                await self._pi_handler.handle_dm(r.agent_id, r.pi_user_id, r.content)
                self.agents[r.agent_id].state.has_pi_directive = True
            except Exception as exc:
                logger.error("[%s] Failed to handle PI DM (DB): %s", r.agent_id, exc)
            await self._mark_pi_dm_handled(r.id)

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

            # Translate before the Slack call, exactly as _post_message does
            # (:3841). A DB-only root (slack_ts is None — minted while Slack
            # was off) has never been seen by Slack: polling it with the
            # canonical id gets a real thread_not_found, which downstream
            # would misread as the thread being dead. Skip it instead. See
            # #20 C1.
            slack_ts = self._slack_parent_ts(thread_id)
            if slack_ts is None:
                continue

            try:
                replies = client.get_thread_replies(ch_id, slack_ts, oldest=oldest)
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
                    visibility=self._resolve_channel_visibility(channel_name),
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

                # Mark proposal as reviewed — only for agents this PI actually
                # owns. The `user_id not in pi_user_ids` guard above only
                # proves the sender is SOME registered PI, not that they own
                # the specific agent whose proposal is on this thread. See
                # COR-5.
                pi_agent_ids = self._pi_slack_id_to_agent_ids.get(user_id, [])
                await self._check_pi_proposal_review(
                    entry, authorized_agent_ids=set(pi_agent_ids),
                )

                # Reopen the thread for all PI's agents
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
    ) -> bool:
        """Post a message to Slack and record it in the message log + DB.

        Returns whether a message was actually recorded — ``False`` when the
        text stripped to nothing, or the reply's parent thread was found to be
        deleted. In either case nothing was posted and no log entry was written,
        so a caller must not count the turn, clear backoff state, or move posts
        between ``interesting_posts`` and ``active_threads``.
        """
        # Final safety: strip any leaked <slack_message> tags
        text = re.sub(r"</?slack_message>", "", text).strip()

        # A truncated response can strip to nothing — the whole body may have been
        # tags. Slack rejects empty text anyway, but bailing here also matters for
        # what happens *after* posting: without this guard _post_message still
        # mints a ts and writes a LogEntry with content="" and slack_ts=None — a DB
        # row with no corresponding Slack message, breaking the
        # row-count-matches-Slack-message-count invariant documented below — and the
        # caller still counts the turn as published even though nothing went out.
        # Return before any of that: no Slack call, no minted ts, no log entry.
        if not text:
            logger.warning(
                "[%s] Suppressed a post to #%s: text was empty after stripping the "
                "slack_message tags — likely a truncated response with no real body.",
                agent_id, channel,
            )
            return False

        client = self.slack_clients.get(agent_id)
        agent = self.agents.get(agent_id)

        # Authorship guard, chokepoint pass (issue #29). The phase gates have
        # already run for phase-4/phase-5 drafts (they own the backoff
        # counters); this pass exists so no future call site can bypass the
        # guard. Idempotent — a clean draft validates twice at negligible
        # cost. Skipped for senders without an Agent (system posts).
        if agent is not None:
            authorship_reason = self._reject_ungrounded_authorship(agent, text)
            if authorship_reason:
                logger.warning(
                    "[%s] Suppressed post to #%s at _post_message: %s",
                    agent_id, channel, authorship_reason,
                )
                return False

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
        slack_refused = False
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
                return False
            if result is None:
                # A connected client attempted the post and Slack refused it (a
                # handled SlackApiError other than thread_not_found — msg_too_long,
                # is_archived, not_in_channel, invalid_auth, ...): AgentSlackClient
                # logs the reason and returns None. That None is indistinguishable
                # by *value* from the disconnected/MOCK path below, which also
                # leaves `result` at its initial None — but the two must not be
                # treated the same: MOCK is an intentional no-Slack-call skip that
                # still records the message as a DB-only row (Slack-off simulation
                # mode); this is a connected client that tried and failed. Falling
                # through to the shared mint-a-ts-and-persist logic below would
                # count the turn and let the thread-outcome checks act on a
                # message that does not exist on Slack. See COR-1b.
                # ...but do not DROP the message either. The DB is the durable store
                # (specs/local-db-conversations.md), and returning False here without
                # persisting lost the text outright — pinned as data loss by
                # test_slack_lifecycle_live.py::test_posting_to_an_archived_channel_does_not_crash,
                # which passes at 18ba52c and failed once COR-1b landed. That live test
                # is skipped unless the copi-test credentials are exported, which is why
                # the regression reached this branch unnoticed.
                #
                # Record it the way the Slack-off path already does: a DB-only row with
                # slack_ts=None and a locally-minted canonical id, so nothing claims a
                # Slack identity the message does not have (`_slack_parent_ts` returns
                # None for it and the mirror is correctly skipped). Then still report
                # failure to the caller, so the turn is not counted and the two-strike
                # post-failure backoff applies. That satisfies COR-1b's actual
                # requirement — no phantom Slack row — without the data loss.
                logger.error(
                    "[%s] Slack post to #%s failed (connected client, no result) "
                    "— recording it as a DB-only row and reporting the post as failed",
                    agent_id, channel,
                )
                slack_refused = True
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
        # False when a connected client tried and Slack refused: the row above is a
        # DB-only record of what the agent said, not evidence that it posted.
        return not slack_refused

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

    async def _backfill_foa_cache(self) -> None:
        """Ensure locally cached FOA details exist for all previously posted opportunities."""
        from sqlalchemy import select as sa_select

        from src.agent.foa_cache import backfill_cache
        from src.models import GrantbotPostedFoa

        if not self.session_factory:
            return
        try:
            async with self.session_factory() as db:
                result = await db.execute(sa_select(GrantbotPostedFoa.foa_number))
                posted_numbers = [n for n in result.scalars().all() if n]
            if posted_numbers:
                count = await backfill_cache(posted_numbers)
                if count:
                    logger.info("Backfilled FOA cache for %d opportunities", count)
        except Exception as exc:
            logger.warning("FOA cache backfill failed: %s", exc)

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
                sender_user_id=r.sender_user_id,
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
                sender_user_id=r.sender_user_id,
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
        # Stay under Postgres' per-statement bind-parameter ceiling. See
        # PERSIST_MAX_ROWS_PER_STMT: one oversized VALUES list is a poison pill,
        # because the except below re-queues the whole batch on failure.
        per_row_params = max(1, len(rows[0]))
        chunk_size = max(1, min(PERSIST_MAX_ROWS_PER_STMT, _PG_MAX_BIND_PARAMS // per_row_params))
        try:
            async with self.session_factory() as db:
                for start in range(0, len(rows), chunk_size):
                    stmt = pg_insert(AgentMessage.__table__).values(rows[start:start + chunk_size])
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

    async def _resolve_service_bot_uids(self) -> None:
        """Learn the Slack uid of each service bot (grantbot today).

        A service bot never holds an AgentRegistry status=='active' row, so it
        is never a roster slot and no roster client carries its uid — the
        inbound paths cannot attribute its posts: production shows all 315 of
        grantbot's :moneybag: posts persisted with agent_id NULL, which
        _entry_allowed fails closed on. One throwaway auth.test is the only way
        to get the uid.

        Token resolution is DB-first, same precedence as grantbot.py itself
        (#23 COR-26c / D10, Task 23.4): ``get_agent_bot_token(db, "grantbot")``
        reads the AgentRegistry row's slack_bot_token column (the documented
        /admin/agents provisioning path) when a session_factory is available,
        falling back to the dedicated ``slack_bot_token_grantbot`` settings
        field. NOT ``get_agent_bot_token``'s own internal env fallback — that
        goes through ``Settings.get_slack_tokens()``, which has no "grantbot"
        key, so relying on it would silently regress .env-only deployments.

        Deliberately NOT reusing grantbot.py's SuBot-token fallback: posts made on
        su's token carry *su's* uid, so mapping that uid to "grantbot" would
        mis-attribute SuBot's own traffic. Every failure mode here (no token, bad
        token, DB down, Slack down) degrades to the pre-existing NULL
        attribution — it must never abort start().
        """
        if not self.slack_enabled:
            return
        from src.agent.slack_client import AgentSlackClient
        from src.services.slack_tokens import get_agent_bot_token, is_valid_token

        token: str | None = None
        if self.session_factory is not None:
            try:
                async with self.session_factory() as db:
                    token = await get_agent_bot_token(db, "grantbot")
            except Exception as exc:
                logger.warning(
                    "grantbot DB token lookup failed — falling back to the "
                    "settings field: %s", exc,
                )
                token = None
        if not is_valid_token(token):
            token = getattr(get_settings(), "slack_bot_token_grantbot", "")
        if not is_valid_token(token):
            logger.info(
                "No usable grantbot token — its funding posts stay unattributed "
                "(invisible to gated agents)",
            )
            return
        # Never enters self.slack_clients: this client exists for one auth.test.
        # In the roster dict it would be polled through and posted through as if
        # it were an agent.
        probe = AgentSlackClient(agent_id="grantbot", bot_token=token)
        try:
            connected = probe.connect()
        except Exception as exc:
            # connect() only handles SlackApiError; DNS/SSL/socket errors escape it.
            # Record the attempted token even on failure (see the dict's comment
            # in __init__): otherwise a dead-but-valid-looking DB token never
            # equals `_service_bot_tokens.get("grantbot")` and _sync_roster_from_db
            # re-probes (and re-logs this warning) on every single tick forever.
            self._service_bot_tokens["grantbot"] = token
            logger.warning("grantbot uid probe raised — continuing without it: %s", exc)
            return
        if not connected or not probe.bot_user_id:
            self._service_bot_tokens["grantbot"] = token
            logger.warning(
                "grantbot auth.test yielded no bot_user_id — its funding posts stay "
                "unattributed this run",
            )
            return
        self._service_bot_uids[probe.bot_user_id] = "grantbot"
        self._service_bot_tokens["grantbot"] = token
        logger.info("Service bot grantbot resolved to Slack uid %s", probe.bot_user_id)

    def _bot_uid_map(self) -> dict[str, str]:
        """Slack bot_user_id -> agent_id for every bot whose posts we can attribute.

        Roster clients first; service bots merged in with setdefault so they can
        never override a roster entry. The collision was real until `dd82c91`
        (#23 COR-26c) stopped grantbot borrowing SuBot's token; posts already
        made that way are still su's, so the roster answer stays the true one.
        """
        # getattr, not attribute access: this is now called from __init__ (see
        # set_bot_uid_map above), and `slack_clients` legitimately holds
        # partial doubles in the unit suite as well as NullTransport and
        # AgentSlackClient in production. A transport that cannot name its bot
        # user simply contributes no uid mapping.
        uid_map = {
            uid: aid
            for aid, c in self.slack_clients.items()
            if c is not None and (uid := getattr(c, "bot_user_id", None))
        }
        for uid, aid in self._service_bot_uids.items():
            uid_map.setdefault(uid, aid)
        return uid_map

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

        # bot_user_id -> agent_id, roster clients plus service bots. Covers both
        # resolution sites below (channel history and thread replies), which is
        # where grantbot's backlog is ingested on a resumed run.
        bot_uid_to_agent = self._bot_uid_map()

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

    def _pi_name_forms(self, *agent_ids: str | None) -> set[str]:
        """Sender-name forms that mean "this row is really the PI's".

        Verified against the real writers rather than invented: the plain
        ``"PI"`` DM/legacy fallback (``_poll_slack_for_pi_messages`` inbound-DB
        branch and ``record_pi_dm``), the guidance-reopen synthetic row's
        ``"PI (via web)"`` (``already_minted`` block below), the web reply/DM
        writer's ``f"{current_user.name} (PI)"`` (``src/routers/agent_page.py``),
        and a Slack-native PI reply's own resolved display name — which is the
        PI's plain name, i.e. ``Agent.pi_name`` for one of the thread's two
        participants. Used to fail closed on an unattributed row (#20 I2):
        without this, ANY sender_agent_id-None row — an arbitrary workspace
        human, or an unattributed bot post — was accepted as authoritative PI
        guidance on rebuild.
        """
        names = {"PI (via web)", "PI"}
        for a in agent_ids:
            if a is None:
                continue
            agent = self.agents.get(a)
            if agent is not None:
                names.add(agent.pi_name)
                names.add(f"{agent.pi_name} (PI)")
        return names

    @staticmethod
    def _reopen_offset(history: list[LogEntry], reopened_at: float) -> int:
        """How much of a reopened thread predates the reopen — i.e. is free.

        Phase 4 recomputes ``message_count = len(history) - offset`` (:1548)
        and closes the thread once that reaches ``max_thread_messages``, so
        ``message_count_offset`` answers "how much of this thread does not
        count against the budget the reopen granted". At the reopen instant
        the answer is "all of it", which is what the two LIVE reopen paths
        record (``_reopen_thread``, the web-guidance block in
        ``_sync_proposal_reviews_from_db``). On a REBUILD it is not: the
        replies the reopen already paid for are in the log too, and counting
        them as prior history refunds them — COR-13's "plus a fresh reply
        budget each time", which the per-tick dedup set stopped and the
        rebuild did not.

        ``reopened_at`` is durable (``thread_decisions.reopened_at``,
        migration 0028) and ``posted_at`` is on every log entry, so the spent
        count is DERIVED here rather than stored: no column and no hot-path
        write. Shared by both rebuild loops so a roster flip and a restart
        cannot disagree about one thread's budget.
        """
        return sum(1 for h in history if h.posted_at < reopened_at)

    def _derive_post_failure_count(self, agent_id: str, history: list[LogEntry]) -> int:
        """Reconstruct the two-strike post-failure backoff on rebuild (RC-9b,
        #20 audit 2026-09-08).

        ``ThreadState.post_failure_count`` is in-memory only, so a restart
        used to hand a still-failing thread a fresh two strikes every time —
        silently undoing the #20 COR-1b back-off (the thread re-enters
        ``active_threads`` unparked and burns another LLM call per turn until
        it fails twice again). A trailing run of this agent's own DB-only rows
        (``slack_ts IS NULL``) at the tail of the thread history is exactly
        what a Slack-refused post leaves behind (``_post_message``'s
        ``slack_refused`` branch) — counting it reconstructs the same signal.

        Gated on ``self.slack_clients.get(agent_id)`` being a CONNECTED
        client: with Slack off (or no client for this agent), EVERY row is
        legitimately ``slack_ts IS NULL`` — that is the normal DB-primary
        write path, not a failure — so the trailing-run signal would
        misinterpret ordinary traffic as two strikes and park every thread on
        every restart. Only a connected client's refusal is evidence of an
        actual failure.
        """
        client = self.slack_clients.get(agent_id)
        if not (client and client.is_connected):
            return 0
        count = 0
        for entry in reversed(history):
            if entry.sender_agent_id != agent_id or entry.slack_ts is not None:
                break
            count += 1
            if count >= 2:
                break
        return count

    async def _rebuild_agent_state(self) -> None:
        """Reconstruct per-agent state from the message log + DB.

        Runs after both the DB rebuild and the optional Slack reconcile, so it
        behaves identically with Slack on or off. Reads only self.message_log,
        thread_decisions, proposal_reviews and llm_call_logs — no Slack calls.
        """
        # Rebuild active_threads per agent.
        # Get all closed thread IDs and prior thread summaries from thread_decisions
        closed_thread_ids: set[str] = set()
        # Reopened-and-not-since-re-closed threads (reopened_at set on the
        # latest decision row) must NOT go into closed_thread_ids — Phase 3/5
        # and the "2." generic active-threads loop below all treat that set
        # as "definitely done". They still need the SAME _prior_threads
        # idempotency accounting as a closed thread, though, so they get their
        # own local set here and are folded into the shared
        # _prior_thread_accounted marker below, instead of reusing
        # _closed_thread_ids for that purpose. See COR-13 / red-team B6.
        reopened_thread_ids: set[str] = set()
        # thread_id -> the reopen instant, as a posted_at-comparable epoch
        # float. The reply budget a reopen grants is SPENT by the replies that
        # follow it, and that spending has to survive a restart: see the
        # message_count_offset computation in the "2." loop below.
        reopened_at_by_thread: dict[str, float] = {}
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(sa_select(ThreadDecision))
                    all_decisions = result.scalars().all()
                    # Latest decision per thread_id: a thread reopened after its
                    # ORIGINAL close (reopened_at set on that row) must not be
                    # treated as still-closed on rebuild — unless a NEWER
                    # ThreadDecision (a genuine re-close) supersedes it. See
                    # COR-13 / migration 0028.
                    latest_for_thread: dict[str, ThreadDecision] = {}
                    for td in all_decisions:
                        current = latest_for_thread.get(td.thread_id)
                        td_ts = td.decided_at.timestamp() if td.decided_at else 0.0
                        cur_ts = current.decided_at.timestamp() if current and current.decided_at else -1.0
                        if current is None or td_ts > cur_ts:
                            latest_for_thread[td.thread_id] = td
                    for td in all_decisions:
                        latest = latest_for_thread[td.thread_id]
                        if latest.reopened_at is not None:
                            reopened_thread_ids.add(td.thread_id)
                            reopened_at_by_thread[td.thread_id] = (
                                latest.reopened_at.timestamp()
                            )
                        else:
                            closed_thread_ids.add(td.thread_id)
                        # _prior_threads is a list per pair, so appending here
                        # unconditionally is not idempotent: a second rebuild —
                        # or a rebuild after _close_thread already recorded this
                        # thread in-process — feeds Phase 5 the same prior
                        # discussion twice, as "you already tried this N times".
                        # _prior_thread_accounted is the shared already-
                        # accounted-for marker (_close_thread sets it before its
                        # own append, alongside _closed_thread_ids — Step 3d),
                        # and it is only updated after this loop, so a thread
                        # with several decision rows from repeated
                        # propose/reopen cycles still contributes each of them
                        # on the first pass.
                        if td.thread_id in self._prior_thread_accounted:
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
                    self._prior_thread_accounted.update(closed_thread_ids | reopened_thread_ids)
                    # COR-13 (closure blocker 1): _db_reopened_thread_ids is
                    # in-memory only and always starts empty, so without this
                    # seed a restart forgets every reopen and
                    # _sync_proposal_reviews_from_db's per-tick reopen block
                    # (keyed the same way, on thread_id — :6139/:6221) fires
                    # again on the next tick: it re-grants
                    # message_count_offset = len(history), a fresh reply
                    # budget every restart, so a reopened thread could never
                    # reach the 12-message timeout close across restarts.
                    # already_minted (below) independently prevents a
                    # duplicate persisted guidance row; this seed prevents the
                    # budget regrant.
                    self._db_reopened_thread_ids.update(reopened_thread_ids)
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
                # A reopened-but-not-since-re-closed thread (COR-13) needs its
                # remaining reply budget and its PI guidance restored here, or
                # the very next Phase 4 recompute (len(history) - offset,
                # :1348) immediately hits max_thread_messages and closes it as
                # "timeout", and pi_context — never itself persisted — is
                # simply gone. Mirrors what a LIVE (same-process) reopen
                # already does in _reopen_thread / the web-guidance reopen
                # block (Step 3i). See red-team B6.
                #
                # REMAINING, not fresh: the offset is the reopen point, not
                # the current message count. Re-granting the full budget on
                # every rebuild is the half of COR-13 that survived ac218fb —
                # a reopened thread could then never reach the timeout close,
                # because each restart refunded the replies since the reopen.
                # See _reopen_offset. A thread with no reopened_at keeps
                # offset 0, exactly as before.
                offset = 0
                pi_context = None
                if thread_id in reopened_thread_ids:
                    offset = self._reopen_offset(
                        history, reopened_at_by_thread[thread_id],
                    )
                    pi_names = self._pi_name_forms(aid, other_id)
                    for h in reversed(history):
                        if h.sender_agent_id is None and not h.is_bot and h.sender_name in pi_names:
                            pi_context = h.content
                            break
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=msg_count,
                    has_pending_reply=has_pending,
                    message_count_offset=offset,
                    pi_context=pi_context,
                    post_failure_count=self._derive_post_failure_count(aid, history),
                )

        # 3. Rebuild pending_proposals per agent
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select

                from src.models import AgentRegistry
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
                    # thread_decisions.pi_engaged_at is COR-5's carrier for an
                    # agent with no linked AgentRegistry.user_id, which can
                    # never have a proposal_reviews row (that table's user_id
                    # is NOT NULL — see _persist_implicit_proposal_review).
                    # It sits on the DECISION, so it is per-decision where a
                    # review row is per-agent, and a decision has two agents:
                    # honouring it for an agent that DOES have a linked PI
                    # would let one lab's PI clear the other lab's block, the
                    # exact cross-lab unblock COR-5's first half exists to
                    # stop. So scope the read to the population that produces
                    # the write. Selecting the LINKED agents (rather than the
                    # unlinked ones) keeps reader and writer symmetric for an
                    # agent_id with no AgentRegistry row at all: the writer's
                    # scalar_one_or_none() reads that as "no user" too.
                    linked_pi_agent_ids = set((await db.execute(
                        sa_select(AgentRegistry.agent_id).where(
                            AgentRegistry.user_id.isnot(None)
                        )
                    )).scalars().all())

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
                    is_reviewed = (td.id, aid) in reviewed_set or (
                        td.pi_engaged_at is not None
                        and aid not in linked_pi_agent_ids
                    )
                    other = td.agent_b if aid == td.agent_a else td.agent_a
                    ref = ProposalRef(
                        thread_id=td.thread_id,
                        channel=td.channel,
                        other_agent_id=other,
                        summary_text=td.summary_text or "",
                        proposed_at=td.decided_at.timestamp() if td.decided_at else 0.0,
                        reviewed=is_reviewed,
                        thread_decision_id=td.id,
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

    async def _rebuild_one_agent_state(self, agent_id: str) -> None:
        """Reconstruct ONE agent's state from the DB + message_log.

        Used when a roster re-add builds a fresh ``Agent()`` with empty
        ``AgentState`` — an inactive->active flip previously discarded
        pending_proposals (the unreviewed-proposal block evaporated),
        active_threads, last_seen_cursor (-> 0.0, a full rescan from epoch),
        and call_times/api_call_count (the rate limiter and legacy cap both
        silently reset). Deliberately NOT ``_rebuild_agent_state()`` — that
        method's cursor step sets every agent's last_seen_cursor to the log's
        current high-water mark, which is only safe once, at startup, before
        any agent has taken a turn; called mid-simulation it would
        fast-forward every OTHER already-running agent's cursor too, skipping
        content they had not scanned yet. Every read and the cursor write
        here are scoped to `agent_id` alone. See E6(2).
        """
        agent = self.agents.get(agent_id)
        if not agent or not self.session_factory or not self.simulation_run_id:
            return
        try:
            from sqlalchemy import func as sa_func
            from sqlalchemy import or_ as sa_or
            from sqlalchemy import select as sa_select

            from src.models import AgentRegistry

            cutoff = datetime.now(UTC) - timedelta(
                seconds=get_settings().llm_rate_window_seconds
            )
            async with self.session_factory() as db:
                decisions = (await db.execute(
                    sa_select(ThreadDecision).where(
                        ThreadDecision.outcome == "proposal",
                        sa_or(
                            ThreadDecision.agent_a == agent_id,
                            ThreadDecision.agent_b == agent_id,
                        ),
                    )
                )).scalars().all()
                reviewed_result = await db.execute(
                    sa_select(ProposalReview.thread_decision_id).where(
                        ProposalReview.agent_id == agent_id,
                    )
                )
                reviewed_ids = {r.thread_decision_id for r in reviewed_result}
                # The same COR-5 carrier _rebuild_agent_state's step 3 reads
                # (see the note there). Without it an inactive->active roster
                # flip re-blocks exactly the proposal a restart no longer
                # re-blocks — E6(2) rebuilt every other piece of this agent's
                # state for that reason. Scoped to THIS agent's own link
                # state, so a linked agent stays governed by its own review row.
                has_linked_pi = (await db.execute(
                    sa_select(AgentRegistry.user_id).where(
                        AgentRegistry.agent_id == agent_id
                    )
                )).scalar_one_or_none() is not None
                # Reopened-and-not-since-re-closed threads for this agent (COR-13 / B6):
                # the same restoration _rebuild_agent_state's "2." loop does (Task 20.10),
                # or a re-added agent's reopened thread comes back with no reply budget
                # and no PI guidance and is re-closed as 'timeout' on its first Phase 4.
                reopened_rows = (await db.execute(
                    sa_select(ThreadDecision.thread_id, ThreadDecision.reopened_at).where(
                        sa_or(
                            ThreadDecision.agent_a == agent_id,
                            ThreadDecision.agent_b == agent_id,
                        ),
                    )
                )).all()
                reopened_thread_ids = {
                    r.thread_id for r in reopened_rows if r.reopened_at is not None
                }
                # The reopen instant per thread, for the same derived reply
                # budget _rebuild_agent_state computes — a roster flip and a
                # restart must not disagree about one thread's remaining
                # replies. MAX over the rows because a thread can carry
                # several decision rows from repeated propose/reopen cycles
                # and _mark_thread_decisions_reopened only stamps the ones
                # still NULL, so the newest stamp is the reopen that granted
                # the budget in force. That is the same row
                # _rebuild_agent_state picks via latest_for_thread.
                reopened_at_by_thread: dict[str, float] = {}
                for r in reopened_rows:
                    if r.reopened_at is None:
                        continue
                    stamp = r.reopened_at.timestamp()
                    if stamp > reopened_at_by_thread.get(r.thread_id, 0.0):
                        reopened_at_by_thread[r.thread_id] = stamp
                # Mirrors _rebuild_agent_state's steps 4 and 4b exactly (red-team
                # M3): an all-time COUNT scoped to THIS simulation_run_id for
                # api_call_count, and a SEPARATE windowed query for the live
                # throttle. Unscoped-by-run + unwindowed (this task's original
                # single `sa_select(LlmCallLog.created_at).where(agent_id==...)`
                # read) would give a re-added agent a LIFETIME cross-run
                # api_call_count — immediately benching it under any non-zero
                # --budget via _agent_within_budget, and corrupting
                # SimulationRun.total_api_calls — while also pulling every
                # historical row for that agent into Python on every roster flip.
                call_count = (await db.execute(
                    sa_select(sa_func.count(LlmCallLog.id)).where(
                        LlmCallLog.simulation_run_id == self.simulation_run_id,
                        LlmCallLog.agent_id == agent_id,
                    )
                )).scalar() or 0
                window_rows = (await db.execute(
                    sa_select(LlmCallLog.created_at)
                    .where(
                        LlmCallLog.simulation_run_id == self.simulation_run_id,
                        LlmCallLog.agent_id == agent_id,
                        LlmCallLog.created_at >= cutoff,
                    )
                    .order_by(LlmCallLog.created_at)
                )).all()

                # Latest decision per thread — an agent can have several
                # propose/reopen/re-propose rows for the same thread_id.
                latest_by_thread: dict[str, ThreadDecision] = {}
                for td in decisions:
                    current = latest_by_thread.get(td.thread_id)
                    td_ts = td.decided_at.timestamp() if td.decided_at else 0.0
                    cur_ts = (
                        current.decided_at.timestamp()
                        if current and current.decided_at else -1.0
                    )
                    if current is None or td_ts > cur_ts:
                        latest_by_thread[td.thread_id] = td

                agent.state.pending_proposals = []
                for td in latest_by_thread.values():
                    other = td.agent_b if agent_id == td.agent_a else td.agent_a
                    agent.state.pending_proposals.append(ProposalRef(
                        thread_id=td.thread_id,
                        channel=td.channel,
                        other_agent_id=other,
                        summary_text=td.summary_text or "",
                        proposed_at=td.decided_at.timestamp() if td.decided_at else 0.0,
                        reviewed=(
                            td.id in reviewed_ids
                            or (not has_linked_pi and td.pi_engaged_at is not None)
                        ),
                        thread_decision_id=td.id,
                    ))

                agent.api_call_count = call_count
                agent.state.call_times.clear()
                for r in window_rows:
                    agent.state.call_times.append(r.created_at.timestamp())

            # active_threads from the in-memory message_log (already loaded
            # at startup and kept live since) — mirrors _rebuild_agent_state's
            # own reconstruction loop (:4190-4243), scoped to this agent only.
            # `has_own_messages` doubles as the message half of the
            # prior-state test below: every entry in the log was hydrated from
            # an `agent_messages` row, so "this agent authored one" is durable
            # evidence it has been live before, not an in-memory artefact.
            has_own_messages = False
            for entry in self.message_log._entries:
                if entry.sender_agent_id != agent_id:
                    continue
                has_own_messages = True
                thread_id = entry.thread_ts or entry.ts
                if thread_id in self._closed_thread_ids:
                    continue
                if thread_id in agent.state.active_threads:
                    continue
                if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                    continue
                if entry.thread_ts is None:
                    history = self.message_log.get_thread_history(thread_id)
                    if len(history) <= 1:
                        continue
                root = self.message_log.get_entry(thread_id)
                if not root:
                    continue
                other_id = root.sender_agent_id if root.sender_agent_id != agent_id else None
                if not other_id:
                    for h in self.message_log.get_thread_history(thread_id):
                        if h.sender_agent_id and h.sender_agent_id != agent_id:
                            other_id = h.sender_agent_id
                            break
                if not other_id:
                    continue
                msg_count = self.message_log.get_thread_message_count(thread_id)
                history = self.message_log.get_thread_history(thread_id)
                last_sender = history[-1].sender_agent_id if history else None
                offset = 0
                pi_context = None
                if thread_id in reopened_thread_ids:
                    # Task 20.10's restoration: a reopened thread gets its reply
                    # budget from the reopen point and carries the PI's guidance.
                    # From the reopen POINT, not from now: `offset = msg_count`
                    # here would hand a re-added agent a full budget for a
                    # thread the restart path counts as nearly spent, and the
                    # two would drift further apart on every flip. Same
                    # derivation, same helper — see _reopen_offset / COR-13.
                    offset = self._reopen_offset(
                        history, reopened_at_by_thread[thread_id],
                    )
                    pi_names = self._pi_name_forms(agent_id, other_id)
                    for h in history:
                        if h.sender_agent_id is None and not h.is_bot and h.sender_name in pi_names:
                            pi_context = h.content
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=msg_count,
                    has_pending_reply=(last_sender is not None and last_sender != agent_id),
                    message_count_offset=offset,
                    pi_context=pi_context,
                    post_failure_count=self._derive_post_failure_count(agent_id, history),
                )

            # Fast-forward the cursor ONLY for an agent that has prior state to
            # resume from. A re-added agent legitimately picks up at the log's
            # high-water mark — re-scanning history it demonstrably already
            # saw would re-evaluate the same posts. A FIRST-TIME activation has
            # no high-water mark, and fast-forwarding it there suppresses the
            # whole REBUILD_WINDOW_S backlog that startup just hydrated: its
            # first Phase 2 scans an empty channel and it can only ever react
            # to traffic posted after the flip. See E6(2).
            #
            # `last_seen_cursor` is persisted nowhere, so the test is made
            # against the three things that ARE durable and are already read
            # above: `agent_messages` rows this agent authored (hydrated into
            # the log), `thread_decisions` naming it, and its `llm_call_logs`
            # rows for this run. A first-time activation has none of the three.
            # `reopened_rows` is the unfiltered ThreadDecision read (every row
            # naming this agent, whatever its outcome), so it subsumes
            # `decisions`, which is only the outcome=='proposal' subset.
            # On the false branch the cursor is left ALONE rather than forced
            # to 0.0 — a fresh Agent() already starts at 0.0 (state.py:80), and
            # an explicit rewind would be a way to un-scan a live agent's
            # history if this ever gains a second caller.
            has_prior_state = bool(has_own_messages or reopened_rows or call_count)
            if has_prior_state:
                agent.state.last_seen_cursor = self.message_log.latest_timestamp
            logger.info(
                "[roster] Rebuilt state for %s agent %s: %d active thread(s), "
                "%d proposal(s), %d API call(s)",
                "re-added" if has_prior_state else "first-time",
                agent_id, len(agent.state.active_threads),
                len(agent.state.pending_proposals), agent.api_call_count,
            )
        except Exception as exc:
            logger.warning(
                "[roster] Failed to rebuild state for re-added agent %s: %s", agent_id, exc,
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
            # Re-queue instead of dropping (mirrors _flush_persisted,
            # :3946-3950): the #30 sliding-window rate limiter rebuilds
            # call_times from llm_call_logs on restart (_rebuild_agent_state
            # step 4b, :4353-4393), so a silently dropped flush under-counts
            # an agent's in-window calls and lets it exceed its allowance
            # after a restart. Prepend so entries buffered while this flush
            # was in flight stay in chronological order after the retry.
            # See COR-11.
            self._llm_log_buffer[0:0] = batch
            logger.warning(
                "Failed to flush %d LLM call logs, re-queued for retry: %s",
                len(batch), exc,
            )
            # ...but bounded: a re-queue that never drains would otherwise hold
            # every full prompt since the outage began in RAM. See
            # LLM_LOG_REQUEUE_MAX_ROWS. One WARNING per overflowing flush, so
            # the loss is never silent and never one line per lost row.
            overflow = len(self._llm_log_buffer) - LLM_LOG_REQUEUE_MAX_ROWS
            if overflow > 0:
                del self._llm_log_buffer[:overflow]
                logger.warning(
                    "LLM call log re-queue is over its %d-row ceiling: dropped "
                    "the %d oldest buffered call(s)",
                    LLM_LOG_REQUEUE_MAX_ROWS, overflow,
                )

    def _sync_profiles_from_disk(self) -> None:
        """Reload any agent whose profile files changed on disk since last turn.

        Private and public profiles can be edited from the web app, which runs
        in a separate process and writes profiles/{private,public}/{id}.md on a
        shared mounted volume. Each Agent caches its profile content in memory
        and otherwise only invalidates that cache for in-process edits (the
        Slack-DM path via Agent.update_private_profile). Without this check, a
        web edit would not reach the running simulation until a restart.

        Detection is by a per-file (exists, mtime) signature rather than a
        scalar mtime maximum: a max can only go up, so it cannot represent a
        deletion — clearing profiles/private/{id}.md (the web UI's
        clear-after-write behaviour) would otherwise never register as a
        change and the agent would keep serving its cached private
        instructions until restart. Comparing the whole (exists, mtime) pair
        per file, cheap (two stat() calls per agent, no DB round-trip) and
        tied to exactly what the agent reads, catches deletions too.
        """
        for agent in self.agents.values():
            signature: list[tuple[str, bool, float | None]] = []
            for sub in ("private", "public"):
                path = PROFILES_DIR / sub / f"{agent.agent_id}.md"
                try:
                    mtime: float | None = path.stat().st_mtime
                except OSError:
                    mtime = None  # file may not exist yet (or a race) — treat as absent
                signature.append((sub, mtime is not None, mtime))

            prev = self._profile_mtimes.get(agent.agent_id)
            new_signature = tuple(signature)
            if prev is None:
                # First observation — record the baseline without reloading.
                self._profile_mtimes[agent.agent_id] = new_signature
                continue
            if new_signature != prev:
                agent.reload_profiles()
                self._profile_mtimes[agent.agent_id] = new_signature
                logger.info(
                    "[%s] Reloaded profiles from disk (external edit detected)",
                    agent.agent_id,
                )

    def _rebuild_bot_name_map(self) -> None:
        """Recompute ``_bot_name_to_id`` from ``self.agents`` and re-seed the
        SERVICE_AGENT_IDS entries (see __init__'s comment on why the
        "grantbot" entry matters: it is the only thing that lets
        _entry_allowed attribute GrantBot's own posts).

        A full rebuild (rather than an incremental pop/set on the one changed
        key) is the fix for #26 DOC-B's residual bug: a roster agent that had
        claimed a service-bot name (the roster answer legitimately overrides
        the seed while it holds the name) and is then renamed AWAY from it
        must not leave the seed permanently missing — rebuilding from
        scratch every time re-applies the setdefault unconditionally. It also
        makes a same-tick swap between two agents' bot_names come out right
        regardless of processing order, since the result is always derived
        from the current source of truth (self.agents) rather than a
        sequence of incremental edits.
        """
        self._bot_name_to_id.clear()
        self._bot_name_to_id.update(
            (a.bot_name.lower(), a.agent_id) for a in self.agents.values()
        )
        for service_id in SERVICE_AGENT_IDS:
            self._bot_name_to_id.setdefault(service_id, service_id)

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
            from src.services.slack_tokens import env_token, get_agent_bot_token, is_valid_token

            grantbot_db_token: str | None = None
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

                # Isolated from the roster query above: a publications-join
                # failure must never abort the roster sync (add/remove/role-
                # diff below never runs otherwise, since both queries were
                # sharing the outer try/except). Stale grounding data is
                # preferable to a silently no-op'd roster tick — leave
                # _agent_publications (and each Agent's db_publication_dois)
                # exactly as they were on failure; absent agents still fail
                # closed regardless. See issue #29 review.
                try:
                    await self._load_publication_records(db)
                except Exception as exc:
                    logger.warning(
                        "[roster] publication-record load failed (grounding data "
                        "may be stale): %s", exc,
                    )

                # grantbot is a service bot (no status=='active' row above, so
                # it never appears in `rows`/`desired`); read its token here,
                # on the session already open, so a rotation can be noticed
                # below without a second DB round trip. See #23 COR-26c/D10.
                #
                # Isolated from the roster query above, same rationale as the
                # publication-record load just above: a failure here (this is
                # its own single-column select, separate from the roster read
                # that already succeeded) must never abort the add/remove/
                # role-diff work still to come — that would silently no-op a
                # newly active agent's admission for the whole tick. See #29
                # review.
                if self.slack_enabled:
                    try:
                        grantbot_db_token = await get_agent_bot_token(db, "grantbot")
                    except Exception as exc:
                        logger.warning(
                            "[roster] grantbot token lookup failed (uid re-probe "
                            "skipped this tick): %s", exc,
                        )
                        grantbot_db_token = None

            desired = {r.agent_id: r for r in rows}

            # Role/name-diff for surviving agents (agents present in both current
            # and desired). Must run even when to_add/to_remove are empty, or a
            # reassignment on a running agent is invisible until the next
            # add/remove. bot_name/pi_name used to be read only in the to_add
            # branch below (#26 DOC-B): a DB rename of a live agent never
            # reached the Agent object or _bot_name_to_id until a restart.
            roster_changed = False
            bot_name_changed = False
            for aid, agent in self.agents.items():
                r = desired.get(aid)
                if r is None:
                    continue
                if getattr(r, "role", "pi_lab") != agent.role:
                    logger.info("[roster] %s role %s -> %s", aid, agent.role, r.role)
                    agent.role = r.role
                    roster_changed = True
                if r.bot_name != agent.bot_name:
                    logger.info(
                        "[roster] %s bot_name %s -> %s", aid, agent.bot_name, r.bot_name,
                    )
                    agent.bot_name = r.bot_name
                    roster_changed = True
                    bot_name_changed = True
                if r.pi_name != agent.pi_name:
                    logger.info(
                        "[roster] %s pi_name %s -> %s", aid, agent.pi_name, r.pi_name,
                    )
                    agent.pi_name = r.pi_name
                    roster_changed = True
            if bot_name_changed:
                # A full rebuild (rather than an incremental pop/set on just
                # the renamed key) re-applies the SERVICE_AGENT_IDS seed and
                # gets same-tick swaps right regardless of processing order —
                # see _rebuild_bot_name_map's docstring.
                self._rebuild_bot_name_map()

            # Token-diff for surviving agents. `main.py` admits every active
            # agent to self.agents regardless of token, so an agent provisioned
            # AFTER startup is in neither to_add nor to_remove: the membership
            # diff below early-returns and the client-building loop (which only
            # runs over to_add) never sees it. It then posts DB-only, silently,
            # until the process restarts. Measured 2026-08-06: 48 bots installed
            # mid-run, tokens all in AgentRegistry, and `Connected as` never rose
            # above the 7 that had tokens at boot. Adopt them here, before the
            # early return, so the docstring's promise is actually true.
            #
            # clients_changed tracks whether ANY Slack client was (re)built this
            # tick — a roster rebuild below, or a grantbot uid re-probe further
            # down. Every exit path that flushes the bot_name-map snapshot also
            # flushes the uid-map snapshot when this is set; skipping that left
            # <@Unew> mentions of a rotated bot unresolved and its posts
            # mis-attributed until a restart (message_log holds a COPY of
            # _bot_uid_map(), taken by set_bot_uid_map).
            clients_changed = False
            if self.slack_enabled:
                for aid in self.agents:
                    r = desired.get(aid)
                    if r is None:
                        continue
                    existing = self.slack_clients.get(aid)
                    # DB is authoritative for rotation: an existing client is
                    # only ever rebuilt off a valid DB token. A cleared/absent
                    # DB token must not fall back to .env for an agent that
                    # already has a live, DB-provisioned client — the fallback
                    # would silently downgrade it, and a dead env token would
                    # otherwise retry a reconnect attempt every tick forever.
                    # getattr, not attribute access, on `existing`: it is
                    # outside the Transport protocol, so a partial double
                    # missing it must not raise (an AttributeError here is
                    # swallowed by the outer except and silently disables
                    # EVERY roster diff for the rest of the run).
                    db_token = r.slack_bot_token if is_valid_token(r.slack_bot_token) else None
                    if existing is not None and db_token is None:
                        continue
                    token = db_token if db_token is not None else env_token(aid)
                    if not is_valid_token(token):
                        continue  # still tokenless — retry on a later tick
                    if existing is not None and getattr(existing, "bot_token", None) == token:
                        continue  # already connected with the current token
                    client = AgentSlackClient(agent_id=aid, bot_token=token)
                    if not client.connect():
                        logger.warning(
                            "[roster] Slack %s failed for %s — will retry",
                            "reconnect" if existing is not None else "connect adopting",
                            aid,
                        )
                        continue
                    self.slack_clients[aid] = client
                    clients_changed = True
                    logger.info(
                        "[roster] %s Slack client for %s (token %s)",
                        "Rebuilt" if existing is not None else "Adopted",
                        aid,
                        "rotated" if existing is not None else "provisioned after startup",
                    )

            # Service-bot (grantbot) uid re-probe: grantbot never carries a
            # status=='active' AgentRegistry row, so the role/name diff above
            # and `desired` never see it — a DB-side token rotation on its row
            # would otherwise go unnoticed until a restart. Compare against the
            # token the last probe ATTEMPTED (success or failure) — a dead
            # token must be probed once, not on every tick forever (#23
            # COR-26c/D10).
            if (
                self.slack_enabled
                and is_valid_token(grantbot_db_token)
                and grantbot_db_token != self._service_bot_tokens.get("grantbot")
            ):
                old_service_bot_uids = dict(self._service_bot_uids)
                await self._resolve_service_bot_uids()
                if self._service_bot_uids != old_service_bot_uids:
                    clients_changed = True
                # A failed re-probe logs its own reason inside
                # _resolve_service_bot_uids and keeps the old uid mapping.

            current = set(self.agents)
            to_remove = current - set(desired)
            to_add = set(desired) - current
            if not to_remove and not to_add:
                # Recompute the gate FIRST: _recompute_allowed_sender_ids ends by
                # refreshing the directory (step 4), so after this line the
                # directory already agrees with the gate. The role/name branch
                # stays because a role or name change alters the directory's
                # *contents* (pi_name headings, bot_name lookups) without moving
                # the gate at all.
                await self._recompute_allowed_sender_ids()
                # Unconditional, exactly like set_bot_uid_map below (#26 I2): a
                # flush skipped this tick by a later exception (e.g.
                # _recompute_allowed_sender_ids raising) has no other way to
                # self-heal — bot_name_changed is only True on the SAME tick as
                # the rename, so a conditional flush here would never retry on
                # a later, healthy tick. bot_name_changed is kept only as a
                # debug-log discriminator, mirroring clients_changed below.
                self.message_log.set_bot_name_map(self._bot_name_to_id)
                if bot_name_changed:
                    logger.debug("[roster] name map flushed after a rename")
                # Unconditional: a small dict copy every ROSTER_POLL_INTERVAL is
                # cheap, and it means a flush skipped this tick by some earlier
                # exception (e.g. an isolated grantbot-probe failure above) still
                # self-heals on the very next tick instead of staying stale until
                # a client is next (re)built. clients_changed is kept only so a
                # debug log can distinguish "flushed because something changed"
                # from "flushed as a no-op".
                self.message_log.set_bot_uid_map(self._bot_uid_map())
                if clients_changed:
                    logger.debug("[roster] uid map flushed after a client (re)build")
                if roster_changed:
                    self.refresh_lab_directories()
                return

            # --- Removals: agent no longer active ---------------------------
            for aid in to_remove:
                self.agents.pop(aid, None)
                self.slack_clients.pop(aid, None)  # Web API only — no socket to close
                self._dm_poll_cursors.pop(aid, None)
                logger.info("[roster] Removed inactive agent %s from live roster", aid)
            if to_remove:
                # Rebuild rather than pop the removed agent's own bot_name key
                # directly: a removed agent that had claimed a service-bot
                # name (e.g. an agent literally named GrantBot) must leave the
                # SERVICE_AGENT_IDS seed reseeded, not missing entirely — see
                # _rebuild_bot_name_map's docstring.
                self._rebuild_bot_name_map()

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
                # A fresh Agent() has empty AgentState — restore whatever this
                # agent already has on record (pending_proposals, active
                # threads, cursor, call ledger) rather than silently
                # discarding it on an inactive->active flip. See E6(2).
                await self._rebuild_one_agent_state(aid)

            # Rebuild cross-agent derived structures after any membership change.
            self.message_log.set_bot_name_map(self._bot_name_to_id)
            self.message_log.set_bot_uid_map(self._bot_uid_map())
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

    async def _load_publication_records(self, db) -> None:
        """Refresh per-agent publication ground truth from the publications table.

        DOIs are normalized to the same form _extract_dois produces
        (lowercase, trailing punctuation stripped) so emit-guard set
        membership works. A lab with registry rows but zero publications is
        deliberately ABSENT from the map — the guard treats that as
        "cannot verify → fail closed" (issue #29 acceptance criterion).
        """
        from sqlalchemy import select as sa_select

        from src.models import AgentRegistry, Publication

        rows = (await db.execute(
            sa_select(AgentRegistry.agent_id, Publication.doi)
            .join(Publication, Publication.user_id == AgentRegistry.user_id)
        )).all()

        records: dict[str, LabPublicationRecord] = {}
        for agent_id, doi in rows:
            record = records.setdefault(
                agent_id, LabPublicationRecord(dois=set(), has_records=True)
            )
            if doi:
                record.dois.add(doi.strip().rstrip(".,;").lower())
        self._agent_publications = records

        # Push DB DOIs onto live Agent objects so the intake guard
        # (cites_own_paper) sees them too.
        for agent_id, agent in self.agents.items():
            record = records.get(agent_id)
            agent.db_publication_dois = record.dois if record else set()

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
                # The gates from the last successful tick are kept above (see the
                # docstring). But the directory is DERIVED from those gates, so a
                # gate that is correct-but-stale makes a directory rebuilt from it
                # correct-but-stale too — which is strictly better than leaving it
                # absent. Without this, a newly-added agent whose gate isn't
                # reflected in any directory yet gets _lab_directory = None for the
                # rest of this failed tick, and existing agents' directories omit
                # it until the next successful sync.
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
                        ProposalReview.thread_decision_id,
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

            # Keyed by (thread_decision_id, agent_id) — the same key the
            # rebuild uses (:4295 `(td.id, aid) in reviewed_set`). Previously
            # this was (agent_id, thread_id): after a re-propose cycle mints a
            # NEW ThreadDecision for the same thread_id, the rebuild (keyed on
            # decision id) correctly blocked on the new decision while this
            # tick (keyed on thread_id) matched the OLD decision's review row
            # and silently unblocked it. See COR-13.
            reviewed_set = {(r.thread_decision_id, r.agent_id) for r in rows}
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

            # Migrated reopens (refined_in_channel set) handle their guidance in
            # the private channel, not the public thread. Collect the channel id
            # + PI guidance per migrated thread so we can seed the handover as a
            # PI-priority interesting post and actually kick off refinement.
            migrated_info: dict[str, tuple[str, str]] = {}  # thread_id -> (refined_channel_id, guidance)
            for r in rows:
                if r.refined_in_channel is None:
                    continue
                guidance = _strip_reopen_prefix(r.comment) if (r.rating == 0 and r.comment) else ""
                # Prefer a row that carries guidance if multiple reviews exist.
                if r.thread_id not in migrated_info or guidance:
                    migrated_info[r.thread_id] = (r.refined_in_channel, guidance)

            # Mark matching in-memory proposals as reviewed. A proposal is
            # considered reviewed for unblocking purposes if EITHER this agent
            # has a ProposalReview row OR the proposal has been migrated to a
            # private channel (refinement supersedes review).
            newly_reviewed: list[tuple[Agent, str]] = []
            for agent in self.agents.values():
                for proposal in agent.state.pending_proposals:
                    if not proposal.reviewed:
                        unblock = (
                            (proposal.thread_decision_id, agent.agent_id) in reviewed_set
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
                # in thread history and the agents can see it — UNLESS the
                # thread history already carries this exact guidance (a prior
                # process minted it and the rebuild/hydrate above already
                # reloaded it from the DB; re-minting it every restart is the
                # duplicate-row half of the bug COR-13 exists to fix). See
                # red-team B6.
                already_minted = any(
                    e.sender_name == "PI (via web)" and e.content == guidance
                    for e in self.message_log.get_thread_history(thread_id)
                )
                if not already_minted:
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
                        visibility=self._resolve_channel_visibility(channel),
                    )
                    self.message_log.append(pi_entry)

                # Reopen the thread for both agents
                self._closed_thread_ids.discard(thread_id)
                await self._mark_thread_decisions_reopened(thread_id)
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

            # Kick-start refinement for proposals migrated to a private channel.
            self._seed_private_refinements(migrated_info)
        except Exception as exc:
            logger.debug("Proposal review sync failed: %s", exc)

    def _seed_private_refinements(self, migrated_info: dict[str, tuple[str, str]]) -> None:
        """Seed the private-channel handover as a PI-priority interesting post.

        When a PI reopens a proposal it migrates to a collab_private channel and
        the web flow posts the handover (proposal summary + PI guidance + a
        "bots, please proceed" prompt). Unblocking the agents is not enough to
        make them act: in the flat private-channel model refinement flows
        through Phase 2 scan -> interesting_posts -> Phase 5, but the handover
        is older than the agents' resumed cursor (and the cursor rewind can
        overshoot to a stale sibling channel), so Phase 2 never surfaces it and
        both bots skip Phase 5 forever.

        We therefore inject the handover directly into the *responding* bot's
        interesting_posts as a PI-priority post carrying the guidance as
        pi_context — mirroring how the legacy public reopen force-seeds an
        active_thread. pi_priority bypasses the random Phase 5 skip and the
        unreviewed-proposal block; the existing private-channel turn-taking
        (don't reply if we posted last) decides which bot goes first.

        Fires once per thread per process (tracked in
        _db_private_refined_thread_ids). No-ops until the channel is tracked and
        its handover has been polled into the message log — so it self-heals on
        a later tick if discovery/poll hasn't caught up yet.
        """
        if not migrated_info:
            return
        name_by_id = {cid: name for name, cid in self._channel_id_map.items()}
        for thread_id, (refined_cid, guidance) in migrated_info.items():
            if thread_id in self._db_private_refined_thread_ids:
                continue
            channel_name = name_by_id.get(refined_cid)
            if not channel_name:
                continue  # channel not tracked yet — retry next tick
            if channel_name in self._finalized_private_channels:
                self._db_private_refined_thread_ids.add(thread_id)
                continue  # refinement already converged on a recorded proposal
            # Anchor on the most recent top-level bot post in the channel (the
            # handover). If none is in the log yet, the poll hasn't reached it.
            anchor = next(
                (
                    e for e in reversed(self.message_log._entries)
                    if e.channel == channel_name
                    and e.thread_ts is None
                    and e.is_bot
                    and e.sender_agent_id
                ),
                None,
            )
            if anchor is None:
                continue  # handover not polled in yet — retry next tick

            # Recency guard: only kick-start refinements that are fresh. A stale
            # handover was already refined or abandoned; re-seeding it on a
            # fresh process would risk reviving a long-dead channel (the
            # in-process dedup set is empty after a restart).
            if time.time() - anchor.posted_at > _PRIVATE_REFINEMENT_SEED_MAX_AGE_S:
                logger.debug(
                    "Skipping stale private refinement #%s (thread %s, handover %.0fd old)",
                    channel_name, thread_id,
                    (time.time() - anchor.posted_at) / 86400,
                )
                self._db_private_refined_thread_ids.add(thread_id)
                continue

            last_poster = self.message_log.get_last_bot_sender_in_channel(channel_name)
            members = self._private_channel_members.get(refined_cid, set())
            for aid in members:
                agent = self.agents.get(aid)
                if not agent:
                    continue
                # Seed the bot whose turn it is to respond — the member who is
                # NOT the most recent poster. This both kick-starts a fresh
                # refinement (responder hasn't posted) and RE-engages an active
                # one on resume (the bot owing a reply), since Phase 2 won't
                # reliably re-surface the counterpart's last post on its own.
                # Stale channels are excluded by the recency guard above and
                # finalized ones by the check at the top, so re-seeding here only
                # ever revives live, in-flight refinements.
                if aid == last_poster:
                    continue
                if anchor.ts in agent.state.active_threads:
                    continue
                if any(p.post_id == anchor.ts for p in agent.state.interesting_posts):
                    continue
                agent.state.interesting_posts.append(PostRef(
                    post_id=anchor.ts,
                    channel=channel_name,
                    sender_agent_id=anchor.sender_agent_id,
                    content_snippet=(guidance or anchor.content)[:200],
                    posted_at=anchor.posted_at,
                    pi_priority=True,
                    pi_context=guidance or None,
                ))
                logger.info(
                    "[%s] Seeded private refinement in #%s (thread %s) as PI-priority post",
                    aid, channel_name, thread_id,
                )
            # We had a real chance to seed (channel + handover present): don't
            # retry this thread again, even if the only members were the last
            # poster (the counterpart will be seeded once they're loaded).
            self._db_private_refined_thread_ids.add(thread_id)

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

Keep it concise — under 300 words.

Authorship notes: when recording that a paper was (co)authored, name the authoring lab(s) explicitly
(e.g. "Wu Lab co-authored the Desiderata paper"), never a subject-less "Co-authored X". Never record
your own lab as an author of a paper unless it appears in your own publication list.""",
                }
            ]

            agent.record_api_call()
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                # 4000, not 800: the Claude 5 models write longer syntheses (and
                # Sonnet 5's tokenizer spends ~30% more tokens on the same text);
                # 800 (retried at 1600) truncated every memory turn in the
                # migration rehearsal. The cap is a ceiling, not a target — unused
                # headroom costs nothing — and this call is not pinned by the
                # characterization snapshots.
                max_tokens=4000,
                log_meta={"agent_id": agent.agent_id, "phase": "memory"},
            )
            if not response or not response.strip():
                logger.warning("[%s] Memory update: empty response", agent.agent_id)
                return

            # Authorship hygiene (issue #29): a false authorship note written
            # here is re-injected into every future prompt. Strip lines the
            # publication records can't back before persisting.
            own_db = self._agent_publications.get(agent.agent_id)
            profile_dois = agent.own_publication_dois
            own_record = LabPublicationRecord(
                dois=(own_db.dois if own_db else set()) | profile_dois,
                has_records=bool(own_db) or bool(profile_dois),
            )
            response, stripped_lines = strip_ungrounded_authorship_lines(
                response,
                own_record,
                self_names=lab_self_names(
                    agent.agent_id, agent.bot_name, agent.pi_name
                ),
            )
            for line in stripped_lines:
                logger.warning(
                    "[%s] Memory update: stripped ungrounded authorship line: %s",
                    agent.agent_id, line[:160],
                )

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
