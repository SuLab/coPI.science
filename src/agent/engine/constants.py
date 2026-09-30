"""The engine's module constants. Units import these by name, except the six that
tests patch (see the note below ``REBUILD_WINDOW_S``), which are read as
``constants.X`` at call time."""

from sqlalchemy import func
from sqlalchemy.exc import DataError, IntegrityError

from src.agent.agent import PROFILES_DIR
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.models import LlmCallLog

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

#: Rows per INSERT statement in `_flush_persisted`. The agent_messages upsert
#: binds 17 parameters per row and asyncpg refuses a statement past 32,767, so
#: an unchunked flush of 1,928+ rows raised InterfaceError, which is not a
#: row-level error: the whole batch re-queued and failed identically forever.
#: 500 rows is 8,500 binds.
PERSIST_UPSERT_CHUNK_ROWS = 500


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

#: How often ONE channel's poll failure may be reported at WARNING.
#:
#: The per-channel `except` in `_poll_slack_for_bot_messages` used to log at
#: DEBUG, and production runs at INFO — so a channel failing on every tick for
#: hours produced nothing an operator would ever see. Raising it to WARNING with
#: no rate limit trades that for the opposite failure: the poller sweeps every
#: CHANNEL_POLL_INTERVAL (15 s) and a broken channel fails every sweep, which is
#: 240 identical lines an hour PER CHANNEL burying everything else.
POLL_ERROR_LOG_INTERVAL = 300.0
ROSTER_POLL_INTERVAL = 30.0    # seconds between AgentRegistry roster re-syncs
#: Seconds between control-plane polls (claim a pending stop command, refresh
#: the heartbeat row). See _poll_control_plane.
CONTROL_POLL_INTERVAL = 30.0

#: The one interview length the phase guidance is written for (spec §8.4 AG-7):
#: thread_guidance renders EXPLORE <=4, DECIDE <=11, CONCLUDE above, so any
#: other max_thread_messages desynchronises the CONCLUDE turn from the close.
#: Checked at engine startup only, so the web app and worker are unaffected.
REQUIRED_MAX_THREAD_MESSAGES = 12

# How many consecutive main-loop iterations the proposal target must read
# "cap reached AND no open interviews" before the run ends. Bridges the brief
# window between the last pitch and the hub opening its interview thread; a
# single zero-read is not enough. max_runtime is the independent backstop.
PROPOSAL_DRAIN_SETTLE_TICKS = 3

# Distinguishes "role has no cached rate yet" from "role's cached rate is None
# (no override)". A plain dict.get() default cannot tell those apart, so the
# cache would re-read role.toml from disk on every tick for every default role.
_UNSET = object()

# The run's total_messages / total_api_calls are display counters shown in the
# admin UI. Recomputing total_messages with a full COUNT(*) on every flush is
# wasteful once a run accumulates many rows (B1), so refresh the run-stats row at
# most this often (a final refresh is forced on shutdown). The message rows
# themselves are still upserted every flush.
#
# "Cosmetic" was true of BOTH until 2026-08-22. It is no longer true of
# `total_api_calls`: that column now counts REAL API CALLS (tool rounds and
# retries included), where it used to count turns, so it is not comparable with
# any run recorded before that date. See `Agent.record_api_call`,
# `SimulationEngine._unbooked_calls` and `_CALLS_PER_LOG_ROW`. The old,
# per-turn figure is still recoverable for any run as
# `SELECT COUNT(*) FROM llm_call_logs WHERE simulation_run_id = ...`.
RUN_STATS_UPDATE_INTERVAL = 30.0

# How many queued working-memory updates stop() will still run. Each is a
# real LLM call (seconds); the container's stop grace period (-t 420) was
# sized for ONE 16k call, so an unbounded shutdown drain can outlive it and
# get SIGKILLed mid-flush. Anything beyond this bound is dropped LOUDLY.
MEMORY_EVENTS_MAX_AT_SHUTDOWN = 10

# Headlines to post during `stop()`. Each is up to two Slack round-trips and the
# container's stop grace period is finite, so the sweep is bounded like the
# memory drain beside it. Higher than that bound because a headline is cheap
# next to an LLM call, and because a whole run's worth of un-announced verdicts
# arriving at once is exactly the case this exists for.
HEADLINES_MAX_AT_SHUTDOWN = 25

# Closed-thread summaries kept in memory per agent pair, for the Phase-5
# dedup context. The DB's thread_decisions table remains the full record;
# this bounds only what a process accumulates (audit finding 5: one dict per
# close, forever). Must be >= agent.PRIOR_THREADS_RENDERED_PER_PAIR.
PRIOR_THREADS_KEPT_PER_PAIR = 50

# Startup rebuild window (B2): the MessageLog is hydrated with messages from the
# last REBUILD_WINDOW_S plus the full history of any still-undecided thread, so
# RAM/startup cost grows with recent + live volume rather than all-time history.
# Old *closed* threads are left in the DB. Nothing in the engine reopens one
# since 23da58d (2026-08-13). Sized to comfortably cover any active
# conversation's lifetime.
REBUILD_WINDOW_S = 14 * 24 * 3600  # 14 days

# SEEDED_CHANNELS, ASSESSMENTS_SUMMARY_CHANNEL and PROFILES_DIR are re-bound here so
# tests can patch them in ONE place. Engine code reads SEEDED_CHANNELS,
# CHANNEL_POLL_INTERVAL, HEADLINES_MAX_AT_SHUTDOWN, PROFILES_DIR, _UNIVERSAL_CHANNELS
# and _CHANNEL_KEYWORDS as `constants.X` at call time (tests/unit/
# test_engine_import_graph.py::test_patch_seam_is_used_everywhere); every other
# constant is imported by name, which keeps AST checks such as
# tests/unit/test_flush_poison_row.py's bare `_ROW_LEVEL_DB_ERRORS` valid.
__all__ = [
    "ASSESSMENTS_SUMMARY_CHANNEL", "CHANNEL_POLL_INTERVAL", "CONTROL_POLL_INTERVAL",
    "HEADLINES_MAX_AT_SHUTDOWN", "MEMORY_EVENTS_MAX_AT_SHUTDOWN",
    "PERSIST_UPSERT_CHUNK_ROWS", "PER_ROW_RECOVERY_DEADLINE_S", "POLL_ERROR_LOG_INTERVAL",
    "PRIOR_THREADS_KEPT_PER_PAIR", "PROFILES_DIR", "PROPOSAL_DRAIN_SETTLE_TICKS",
    "REBUILD_WINDOW_S", "ROSTER_POLL_INTERVAL", "RUN_STATS_UPDATE_INTERVAL",
    "SEEDED_CHANNELS", "TRUNCATION_NOTICE", "_CALLS_PER_LOG_ROW", "_CHANNEL_KEYWORDS",
    "_ROW_LEVEL_DB_ERRORS", "_UNIVERSAL_CHANNELS", "_UNSET",
]
