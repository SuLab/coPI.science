"""The reply lane: which (agent, thread) pairs owe a reply, bounded concurrent servicing under the thread lock, the Phase-4 turn and the close check (spec §7.1)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import anthropic

from src.agent.agent import Agent
from src.agent.engine import deps
from src.agent.engine.constants import TRUNCATION_NOTICE
from src.agent.engine.context import EngineContext, RunState, via
from src.agent.engine.helpers import _thread_phase_label, _was_truncated
from src.agent.engine.sidecar import _extract_slack_message, _reply_closes_thread
from src.agent.role_capabilities import capabilities_for
from src.agent.state import ThreadState
from src.agent.thread_guidance import phase4_guidance
from src.agent.tools import execute_tool, tools_for_role
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory
    from src.agent.engine.panel import Panel
    from src.agent.engine.post_lane import PostLane
    from src.agent.engine.scheduler import Scheduler
    from src.agent.engine.slack_io import SlackIO
    from src.agent.engine.threads import Threads
    from src.agent.engine.verdicts import Verdicts

logger = logging.getLogger("src.agent.simulation")

_NON_TRANSIENT_ERRORS = (
    anthropic.BadRequestError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)
REPLY_BACKOFF_BASE_S = 60.0
REPLY_BACKOFF_CAP_S = 1800.0


def reply_backoff_seconds(failures: int) -> float:
    """``min(60 s x 4^(n-1), 1,800 s)`` for the n-th consecutive failure."""
    return min(REPLY_BACKOFF_BASE_S * 4 ** (failures - 1), REPLY_BACKOFF_CAP_S)


class ReplyLane:
    """The unpaced reply lane: every (agent, thread) pair that owes a reply."""

    _thread_locks = via("ctx", "thread_locks")
    agents = via("ctx")
    message_log = via("ctx")
    _running = via("_run_state", "running")
    _channel_id_map = via("_channel_directory")
    _channel_visibility = via("_channel_directory")
    _resolve_channel_visibility = via("_channel_directory")
    _note_consult = via("_panel")
    _post_panel_note = via("_panel")
    _record_specialist_consult = via("_panel")
    _specialist_consults = via("_panel")
    _phase3_activate_threads = via("_post_lane")
    _allowance_for = via("_scheduler")
    _post_message = via("_slack_io")
    _close_thread = via("_threads")
    _closed_thread_ids = via("_threads")
    _assessed_threads = via("_verdicts")
    _capture_hub_assessment = via("_verdicts")
    _record_assessment_drop = via("_verdicts")
    _warn_if_hub_conclude_missing_assessment = via("_verdicts")

    OWNED_STATE: tuple[str, ...] = ("_reply_in_flight", "_reply_sem")

    def __init__(
        self,
        ctx: EngineContext,
        *,
        run_state: RunState,
        scheduler: Scheduler,
        post_lane: PostLane,
        threads: Threads,
        verdicts: Verdicts,
        panel: Panel,
        slack_io: SlackIO,
        channel_directory: ChannelDirectory,
    ) -> None:
        self.ctx = ctx
        self._run_state = run_state
        self._scheduler = scheduler
        self._post_lane = post_lane
        self._threads = threads
        self._verdicts = verdicts
        self._panel = panel
        self._slack_io = slack_io
        self._channel_directory = channel_directory
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
        now = deps.time.time()
        if self.ctx.circuit.is_open(now):
            return []
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
                if (has_new or thread.has_pending_reply) and thread.retry_after <= now:
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
        See ``_activate_threads_for_all_agents`` for why only Phase 3 is guarded and why a failed agent's cursor holds.

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
        failed_agent_ids = self._activate_threads_for_all_agents()

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

    def _activate_threads_for_all_agents(self) -> set[str]:
        """Run Phase 3 (thread activation) for every agent, each in its own
        try/except, and return the ids of the agents whose pass raised.

        **Only Phase 3 is guarded**: the blanket try/except that
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
        enough on its own — the cursor-advance loop in ``_dispatch_reply_lane`` used to run
        unconditionally for every agent, so a caught-and-logged exception
        for agent X still marked X's own unprocessed messages "seen" for
        good, the exact permanent, silent loss this whole guard exists to
        avoid. `failed_agent_ids` is collected in the Phase 3 loop and
        consulted in that advance loop so a failed agent's cursor holds where
        it was; its unactivated messages are retried on the next dispatch
        rather than lost.
        """
        # An agent whose Phase 3 pass raises must NOT have
        # its cursor advanced by the caller — that would mark this exact agent's
        # unprocessed messages "seen" on the strength of a pass that never
        # actually ran, a permanent silent loss of the same shape the
        # batch-wide retry promotion (see ``_dispatch_reply_lane``'s docstring) exists to prevent (see
        # test_dispatch_isolates_one_agents_phase3_failure_from_the_others in
        # tests/unit/test_reply_lane.py, which only
        # pins that OTHER agents keep working — it says nothing about the
        # failed agent's own cursor). Collected here, consulted in
        # ``_dispatch_reply_lane``'s cursor-advance loop.
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
        return failed_agent_ids

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
        # `floor_armed` is set once at activation (see
        # `Threads.activate_thread` and its callers), but activation happens long before this thread's
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

        thread_history = await self._reply_preflight(agent, thread, settings)
        if thread_history is None:
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

        tool_executor = self._reply_tool_executor(agent, thread)

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
            raw_response = await self._generate_reply(
                agent, thread, system_prompt, messages, tool_executor,
                reply_thread_phase, reply_message_ordinal, stop_reasons, settings,
            )
        except Exception as exc:  # noqa: BLE001 — S1-04: back off, never spin
            await self._note_reply_failure(agent, thread, exc)
            return
        self._note_reply_success(thread)
        try:
            await self._handle_reply_response(agent, thread, raw_response, stop_reasons)
        except Exception as exc:
            logger.error(
                "[%s] Phase 4 reply to thread %s failed: %s",
                agent.agent_id, thread.thread_id, exc,
            )

    def _note_reply_success(self, thread: ThreadState) -> None:
        thread.failed_call_count = 0
        thread.retry_after = 0.0
        thread.nontransient_streak = 0
        self.ctx.circuit.record_success()

    async def _note_reply_failure(self, agent: Agent, thread: ThreadState, exc: Exception) -> None:
        """S1-04. ``has_pending_reply`` stays True (the dispatch set it) and the
        thread backs off. Every exception class backs off; only two consecutive
        failures of the four non-transient classes on THIS thread, while some
        other call succeeded in between and the breaker is closed, abandon it.
        RateLimit, InternalServer (500/503/504), APITimeout, APIConnection and
        Overloaded (529) are examples of the "back off only" set, not an
        allowlist (C25)."""
        now = deps.time.time()
        logger.error(
            "[%s] Phase 4 reply to thread %s failed: %s",
            agent.agent_id, thread.thread_id, exc,
        )
        thread.failed_call_count += 1
        thread.retry_after = now + reply_backoff_seconds(thread.failed_call_count)
        circuit = self.ctx.circuit
        if isinstance(exc, _NON_TRANSIENT_ERRORS):
            if thread.nontransient_streak == 0:
                thread.nontransient_mark = circuit.success_seq
            thread.nontransient_streak += 1
        else:
            thread.nontransient_streak = 0
        circuit.record_failure(thread.thread_id, now)
        if (
            thread.nontransient_streak >= 2
            and circuit.success_seq > thread.nontransient_mark
            and not circuit.is_open(now)
        ):
            thread.has_pending_reply = False
            logger.warning(
                "[%s] Abandoning thread %s after %d consecutive %s failures while "
                "other model calls succeed",
                agent.agent_id, thread.thread_id, thread.nontransient_streak,
                type(exc).__name__,
            )
            caps = capabilities_for(agent.role)
            if caps is not None and caps.captures_verdicts:
                await self._verdicts._record_assessment_drop(
                    agent.agent_id, "reply_failed",
                    subject_agent_id=thread.other_agent_id,
                    thread_id=thread.thread_id,
                    detail=(
                        f"interview abandoned after {thread.nontransient_streak} "
                        f"consecutive non-transient model errors ({type(exc).__name__}) "
                        f"at ordinal {thread.message_count + 1}"
                    ),
                )

    async def _reply_preflight(
        self, agent: Agent, thread: ThreadState, settings: Any
    ) -> list[dict] | None:
        """The checks that run before a reply is composed: evict a thread whose root is
        absent from this run's log, refuse one the agent is not allowed in, and close
        one that is full. Returns the thread history to compose from, or None when
        the turn is over (the thread was evicted, refused or closed)."""
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
            self._threads.mark_closed(thread.thread_id)
            return None

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
            return None

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
            return None
        return thread_history

    def _reply_tool_executor(
        self, agent: Agent, thread: ThreadState
    ) -> Callable[[str, dict], Awaitable[str]]:
        """Build the tool executor bound to this thread's state (with the durable
        consult record and the Slack panel note). Defining the closures has no side
        effect, so building them before the rate-limit reservation is equivalent."""
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
        return tool_executor

    async def _generate_reply(
        self,
        agent: Agent,
        thread: ThreadState,
        system_prompt: str,
        messages: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        reply_thread_phase: str,
        reply_message_ordinal: int,
        stop_reasons: list[str],
        settings: Any,
    ) -> str:
        """The Phase-4 model call. ``stop_reasons`` is filled by the call's
        ``on_stop_reason`` callback; ``should_continue`` reads the run state live."""
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
        return raw_response

    async def _handle_reply_response(
        self, agent: Agent, thread: ThreadState, raw_response: str, stop_reasons: list[str]
    ) -> None:
        """Act on the model's reply: extract the Slack text, back off on an empty or
        suppressed one, mark a truncated one, post it, capture a hub verdict and
        check whether it closes the thread. Called inside ``_reply_to_thread``'s
        ``try``, so its exceptions reach the same handler and its early returns end
        the turn as they did inline."""
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
            thread_ts=thread.thread_id, landed_check=True,
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
