"""The reply lane services every pending pair without pacing, and the post lane
no longer has a reactive tier.

Task 11 splits the single turn-based pool into two lanes: a paced post lane
(``_run_post_turn`` — Phase 1 + Phase 5) and an unpaced reply lane (this file).
Nothing else calls ``_phase3_activate_threads`` once ``_run_post_turn`` stops
doing it, so ``_dispatch_reply_lane`` runs it for every agent before computing
the pending-pairs queue — otherwise a brand-new @-mention or reply would never
open a thread at all.

Fix round 1 (task review) added the C2/I3/I4/I5 sections below. `_dispatch_
reply_lane` now checks ``self._running`` between pairs (I5), which is False
by default on a freshly constructed engine (it only becomes True inside
``start()``) — so every test that drives the real dispatch sets it explicitly.
"""
import asyncio
import time

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient


async def _noop_async(*_a, **_kw):
    return None


def _engine_with_pending(n):
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    for i in range(n):
        hub.state.active_threads[f"t{i}"] = ThreadState(
            thread_id=f"t{i}", channel="general", other_agent_id=f"pi{i}",
            message_count=1, has_pending_reply=True,
        )
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng._running = True  # I5: the servicing loop now checks this between pairs
    return eng, hub


def test_pending_pairs_lists_every_owed_reply():
    eng, hub = _engine_with_pending(5)
    pairs = eng._pending_reply_pairs()
    assert {t.thread_id for _a, t in pairs} == {f"t{i}" for i in range(5)}
    assert all(a.agent_id == "blackbird" for a, _t in pairs)


@pytest.mark.asyncio
async def test_dispatch_services_all_pending_pairs(monkeypatch):
    eng, hub = _engine_with_pending(5)
    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert n == 5
    assert sorted(served) == [f"t{i}" for i in range(5)]


def test_the_reactive_tier_is_gone():
    import inspect

    src = inspect.getsource(SimulationEngine._select_agent)
    assert "_owes_reply" not in src, "the post lane must not do reactive selection"
    assert "_reactive_streak" not in src


# ---------------------------------------------------------------------------
# Phase 3 (thread activation) has no other caller once _run_post_turn drops it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_activates_newly_tagged_threads(monkeypatch):
    """Nothing but _dispatch_reply_lane calls _phase3_activate_threads now that
    _run_post_turn is Phase 1 + 5 only — without this, a brand-new @-mention
    would never open a thread at all."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng._running = True
    eng.message_log.set_bot_name_map({"blackbirdbot": "blackbird"})
    eng.message_log.append(LogEntry(
        ts="1.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="hey @BlackbirdBot", thread_ts=None, posted_at=1.0, is_bot=True,
    ))

    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)

    n = await eng._dispatch_reply_lane()

    assert "1.0" in hub.state.active_threads, "Phase 3 never activated the new tag"
    assert served == ["1.0"]
    assert n == 1


@pytest.mark.asyncio
async def test_dispatch_isolates_one_agents_phase3_failure_from_the_others(
    monkeypatch,
):
    """NEW Important, fix round 2: `_phase3_activate_threads` runs once per
    agent, so one agent's activation bug must not stop every OTHER agent's
    from running too — the same shape of silent, tick-forever failure the
    two Criticals in this same review round fixed elsewhere."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
    )
    eng._running = True
    eng.message_log.set_bot_name_map({"wangbot": "wang"})
    eng.message_log.append(LogEntry(
        ts="1.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="hey @WangBot", thread_ts=None, posted_at=1.0, is_bot=True,
    ))
    monkeypatch.setattr(eng, "_service_reply", _noop_async)

    real_phase3 = eng._phase3_activate_threads

    def _phase3(agent):
        if agent.agent_id == "blackbird":
            raise RuntimeError("boom")
        return real_phase3(agent)

    monkeypatch.setattr(eng, "_phase3_activate_threads", _phase3)

    await eng._dispatch_reply_lane()

    assert "1.0" in lab.state.active_threads, (
        "the hub's activation failure must not stop wang's own Phase 3 from "
        "running"
    )


@pytest.mark.asyncio
async def test_dispatch_advances_the_cursor_so_phase3_does_not_rescan_forever(
    monkeypatch,
):
    """Mirrors Task 6's snapshot-then-assign cursor invariant, applied to the
    reply lane: Phase 3 depends on last_seen_cursor to bound its "since cursor"
    scans (get_tags_for_agent / get_replies_to_agent_posts /
    get_new_top_level_posts are all O(len(log)) linear scans — see
    message_log.py). Without this, every agent the post lane does not happen to
    pick would rescan the entire message log from turn zero on every single
    main-loop tick, forever.
    """
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng._running = True
    eng.message_log.append(LogEntry(
        ts="5.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="unrelated post", thread_ts=None, posted_at=5.0, is_bot=True,
    ))
    monkeypatch.setattr(eng, "_service_reply", _noop_async)

    await eng._dispatch_reply_lane()

    assert hub.state.last_seen_cursor == 5.0


@pytest.mark.asyncio
async def test_dispatch_does_not_hide_a_reply_that_arrives_during_its_own_pass(
    monkeypatch,
):
    """The cursor must advance from a snapshot taken BEFORE Phase 3 runs, not
    after — advancing early would make _pending_reply_pairs' own
    has_new_reply_from_other check compare against a cursor that already
    covers the very reply it is trying to detect."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    eng._running = True
    eng.message_log.append(LogEntry(
        ts="1.0", channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="root", thread_ts=None,
        posted_at=1.0, is_bot=True,
    ))
    hub.state.active_threads["1.0"] = ThreadState(
        thread_id="1.0", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=False,
    )
    eng.message_log.append(LogEntry(
        ts="2.0", channel="general", sender_agent_id="pi0", sender_name="Pi0Bot",
        content="a reply", thread_ts="1.0", posted_at=2.0, is_bot=True,
    ))

    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)

    n = await eng._dispatch_reply_lane()

    assert served == ["1.0"], "a reply already in the log before this pass started was missed"
    assert n == 1


# ---------------------------------------------------------------------------
# Carry forward from Task 10: Phase 4 activity must never stamp
# last_phase5_action_time — that stamp lived inside _run_turn's Phase-4
# block, which no longer exists now that Phase 4 moved into _service_reply
# entirely.
#
# Fix round 1 (task review, Important I3) moved the skip-backoff reset out of
# _service_reply (per pair) and into _dispatch_reply_lane (once per engaged
# agent per tick), on the theory that batching would fix the cross-lane
# coupling. Fix round 2 (task review, Ruling R10) found that idempotent — a
# reset that fires 5 times a tick and one that fires once produce identical
# final state, so an agent holding an open pending thread was STILL zeroed
# every single tick either way, permanently disabling `_select_agent`'s
# `skips >= 3` de-weighting. Ruling R10 deletes the reply lane's ownership of
# this counter entirely: it is now wholly post-lane-owned (`_phase5_new_post`
# increments it on a skip/rejection, resets it on a genuinely successful
# post — see tests/unit/test_phase5_actions.py). See the two tests right
# below.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_reply_does_not_stamp_the_spontaneous_timer(monkeypatch):
    hub, _thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    stale_time = time.time() - 10**9
    hub.state.last_phase5_action_time = stale_time
    monkeypatch.setattr(eng, "_reply_to_thread", _noop_async)

    await eng._service_reply(hub, _thread)

    assert hub.state.last_phase5_action_time == stale_time, (
        "only a real Phase 5 action (inside _phase5_new_post) may stamp this — "
        "reply-lane activity conflating replying with posting is exactly the "
        "cross-lane coupling Task 10 removed"
    )


def test_reply_lane_never_touches_the_skip_streak():
    """Structural pin for Ruling R10: neither `_service_reply` nor
    `_dispatch_reply_lane` may reference `consecutive_phase5_skips` at all —
    it is wholly post-lane-owned now."""
    import inspect

    assert "consecutive_phase5_skips =" not in inspect.getsource(
        SimulationEngine._service_reply
    )
    assert "consecutive_phase5_skips =" not in inspect.getsource(
        SimulationEngine._dispatch_reply_lane
    )


@pytest.mark.asyncio
async def test_dispatch_does_not_reset_the_skip_streak_for_an_engaged_agent(
    monkeypatch,
):
    """The regression test for Ruling R10: an agent that engages in reply-lane
    activity (has a pending pair serviced this tick) must NOT have its
    skip-backoff streak touched — a reply-lane write of a post-lane pacing
    variable is the cross-lane coupling this feature exists to remove, and
    the reset is idempotent so "once per tick" is no fix at all."""
    eng, hub = _engine_with_pending(1)
    hub.state.consecutive_phase5_skips = 3
    monkeypatch.setattr(eng, "_service_reply", _noop_async)

    await eng._dispatch_reply_lane()

    assert hub.state.consecutive_phase5_skips == 3, (
        "the reply lane must not reset consecutive_phase5_skips at all"
    )


# ---------------------------------------------------------------------------
# C2 — one failing pair must not abort the sweep, and every pair's retry
# signal must be promoted for the WHOLE batch before any servicing runs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_pair_does_not_abort_the_sweep(monkeypatch):
    """Before this fix, the servicing loop was a bare ``for ... await`` with
    no try/except, and its call site in _run_main_loop was the only await in
    the main-loop body not wrapped in one either — one pair raising took down
    the rest of the sweep (and, uncaught, the whole tick).

    ``n`` here is the ATTEMPTED count — `_dispatch_reply_lane`'s return value
    (kept as a log/metric, see its docstring). It does NOT mean "3 units of
    real work happened" and does not drive the main loop's idle backoff any
    more (fix round 2, NEW Critical): that decision is spend-based
    (`api_call_count` before/after) and tested separately in
    test_hub_budget_scheduler.py's ``TestReplyLaneIsNotPacedByTheIdleBackoff``,
    including the regression case where every pair is attempted but none of
    them spend anything (a rate-limited pending pair).
    """
    eng, hub = _engine_with_pending(3)
    served = []

    async def _serve(agent, thread):
        if thread.thread_id == "t1":
            raise RuntimeError("boom")
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert sorted(served) == ["t0", "t2"], (
        "a sibling pair's failure must not abort the rest of the sweep"
    )
    assert n == 3, "n counts ATTEMPTS (including the one that raised), not spend"


# ---------------------------------------------------------------------------
# I4 — a thread closed (or evicted) by a sibling pair's call, mid-sweep, must
# not be serviced.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_reply_skips_a_thread_closed_mid_sweep(monkeypatch):
    """`pairs` is snapshotted once by _dispatch_reply_lane before any of its
    (possibly many, possibly slow) LLM calls run, and _close_thread can pop
    this exact thread out from under a sibling pair's call in the same
    sweep. Without a guard, the agent spends a real Opus call replying into
    an already-closed thread."""
    hub, thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    thread.status = "closed"
    hub.state.active_threads.pop(thread.thread_id, None)

    called = []

    async def _fake_reply(_a, _t):
        called.append(True)

    monkeypatch.setattr(eng, "_reply_to_thread", _fake_reply)

    await eng._service_reply(hub, thread)

    assert called == [], (
        "must not call _reply_to_thread for a thread closed mid-sweep"
    )


@pytest.mark.asyncio
async def test_service_reply_skips_a_thread_evicted_from_active_threads(monkeypatch):
    """Other half of the guard: a ThreadState still reporting status=="active"
    but removed from agent.state.active_threads entirely (e.g. evicted) must
    also not be serviced."""
    hub, thread = _hub_with_one_pending_thread()
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    hub.state.active_threads.pop(thread.thread_id, None)  # status left "active"

    called = []

    async def _fake_reply(_a, _t):
        called.append(True)

    monkeypatch.setattr(eng, "_reply_to_thread", _fake_reply)

    await eng._service_reply(hub, thread)

    assert called == []


# ---------------------------------------------------------------------------
# I5 — a shutdown request must be honoured within one reply's worth of
# latency, not after draining the whole sweep.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_stops_early_when_the_engine_stops_mid_sweep(monkeypatch):
    """Fix round 3 (task review, Critical): the mock MUST contain a real
    `await` — production `_service_reply` awaits a 10-60s Opus call, and a
    mock with no `await` at all cannot exercise (or fail against) the bug
    this test guards: `asyncio.gather` schedules every pair's task up front,
    and an uncontended `Semaphore`/`Lock.acquire` never actually suspends, so
    with no real await anywhere, every task races through its whole body in
    one scheduler step before the shutdown flag set by an earlier pair is
    ever visible to a later one. `await asyncio.sleep(0)` is the minimal real
    yield point that reproduces the production task-scheduling gap."""
    eng, hub = _engine_with_pending(5)
    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)
        if len(served) == 2:
            eng._running = False  # shutdown requested mid-sweep
        await asyncio.sleep(0)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert served == ["t0", "t1"], (
        "must stop as soon as _running goes False, not drain the rest of the sweep"
    )
    assert n == 2
    # C2: the pairs never reached this tick must still carry a retry signal —
    # has_pending_reply was promoted for the WHOLE batch before servicing
    # started, independent of how far the loop actually got.
    for i in (2, 3, 4):
        assert hub.state.active_threads[f"t{i}"].has_pending_reply is True, (
            f"t{i} was never reached this tick but must still retry next dispatch"
        )


def _hub_with_one_pending_thread():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="pi0",
        message_count=1, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    return hub, thread


# ---------------------------------------------------------------------------
# Task 13 — concurrent reply lane behind reply_lane_max_in_flight.
#
# The default is 1 (concurrency OFF); the two tests below that actually
# exercise overlap skip under that default and must be re-run with
# REPLY_LANE_MAX_IN_FLIGHT=4 (or any cap >= 2) to be exercised at all — see
# the task-13-report.md TDD evidence section for that run's output.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_runs_pairs_concurrently_up_to_the_cap(monkeypatch):
    from src.config import get_settings

    cap = get_settings().reply_lane_max_in_flight
    if cap < 2:
        pytest.skip("concurrency disabled by configuration")

    eng, hub = _engine_with_pending(cap * 3)
    live = 0
    peak = 0

    async def _serve(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    monkeypatch.setattr(eng, "_service_reply", _serve)
    await eng._dispatch_reply_lane()

    assert peak > 1, "reply lane did not overlap"
    assert peak <= cap


@pytest.mark.asyncio
async def test_a_pair_already_in_flight_is_not_spawned_twice(monkeypatch):
    eng, hub = _engine_with_pending(1)
    starts = 0

    async def _serve(agent, thread):
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.02)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    await asyncio.gather(eng._dispatch_reply_lane(), eng._dispatch_reply_lane())

    assert starts == 1, "the same (agent, thread) was serviced twice concurrently"


@pytest.mark.asyncio
async def test_the_fanout_bound_is_global_not_per_turn(monkeypatch):
    """Ruling R7 / spec §6.3, ported from the deleted
    tests/unit/test_phase4_concurrency.py (Task 11 deleted its only subject,
    ``_phase4_reply_threads``, along with the whole file). The property:
    N concurrent turns must share ONE budget, not each get their own cap — a
    semaphore constructed per-call (as ``_llm_fanout_sem`` effectively was,
    being sized from a per-turn-fanout setting) bounds each call's own
    fan-out separately, so two overlapping calls together could spend up to
    2x cap. ``_reply_sem`` is constructed exactly once, in ``__init__``, for
    exactly this reason.

    Two full ``_dispatch_reply_lane()`` sweeps overlapping is exactly the
    scenario ``_dispatch_reply_lane``'s own docstring (fix round 1, I5) calls
    out as possible: a sweep slow enough that the next tick's call starts
    before it finishes. Engineered contention (real overlap, not scheduling
    luck — same technique as
    test_engine_locks.test_acquire_all_does_not_deadlock_under_genuine_contention):
    the first sweep is allowed to run until its ``cap`` pairs are genuinely
    parked mid-reply, holding ``_reply_sem``, before a SECOND, disjoint set of
    pending pairs (a different agent, added only now) is handed to a second
    concurrent sweep. If the two sweeps had independent semaphores, the
    second sweep's pairs would run immediately, pushing peak concurrency past
    ``cap``; because they share one, the second sweep's pairs must wait.
    """
    from src.config import get_settings

    # Deliberately NOT skipped at cap < 2 (unlike
    # test_dispatch_runs_pairs_concurrently_up_to_the_cap above, which
    # legitimately needs `peak > 1` to prove overlap happened at all): this
    # test's property — a SHARED budget, not a per-call one — holds and
    # discriminates correctly even at cap=1. A per-call semaphore bug would
    # let sweep two's disjoint pair run under its own fresh allowance WHILE
    # sweep one's pair is still asleep holding the (buggy, non-shared) first
    # one, pushing peak to 2 against a cap of 1; the real (shared) `_reply_
    # sem` keeps peak at 1. Verified empirically before this test was written
    # (see task-13-report.md's TDD evidence) — this is exactly the property
    # that must run in the default-config gate, not skip in it.
    cap = get_settings().reply_lane_max_in_flight

    eng, hub = _engine_with_pending(cap)
    live = 0
    peak = 0

    async def _serve(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.05)
        live -= 1

    monkeypatch.setattr(eng, "_service_reply", _serve)

    sweep_one = asyncio.ensure_future(eng._dispatch_reply_lane())
    # Real wall-clock wait, not a bare yield: give sweep one's `cap` sub-tasks
    # time to actually acquire _reply_sem and settle into `_serve`'s sleep —
    # only then are they GENUINELY holding the semaphore rather than merely
    # scheduled.
    await asyncio.sleep(0.01)
    assert live == cap, "sweep one did not reach the cap before sweep two started"

    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    for i in range(cap):
        lab.state.active_threads[f"L{i}"] = ThreadState(
            thread_id=f"L{i}", channel="general", other_agent_id="blackbird",
            message_count=1, has_pending_reply=True,
        )
    eng.agents["wang"] = lab

    sweep_two = asyncio.ensure_future(eng._dispatch_reply_lane())

    await asyncio.wait_for(asyncio.gather(sweep_one, sweep_two), timeout=5.0)

    assert peak <= cap, (
        f"two overlapping sweeps reached {peak}, above the shared cap {cap} — "
        "the semaphore is no longer global"
    )


# ---------------------------------------------------------------------------
# Ruling R11 — lock order. `_close_thread` and `_evict_dead_thread` both take
# an AGENT lock (or several, sorted) while the reply lane's THREAD lock is
# already held around the whole `_service_reply` call (see `_dispatch_reply_
# lane`'s `_run`). `_phase5_new_post` takes only an agent lock and never a
# thread lock. The documented, enforced-by-convention invariant is: thread
# lock before agent lock, NEVER the reverse — see the note on `_thread_locks`
# in `SimulationEngine.__init__`.
# ---------------------------------------------------------------------------


def test_no_call_site_bypasses_acquire_all():
    """spec §3.2: acquire_all is the only sanctioned way to take more than
    one lock at a time. A direct `.get(key).acquire()` on either registry
    would let a call site invent its own (possibly unsorted, possibly
    inverted) acquisition order outside that discipline.

    NOTE (task review, Important 3): this only guards against bypassing the
    registry's own sorted multi-key acquisition — it does NOT by itself
    catch a call site that correctly uses `acquire_all` for each registry but
    nests them in the wrong order (agent lock outer, thread lock inner). See
    `test_agent_lock_never_precedes_thread_lock_in_the_same_function_scope`
    below for that.
    """
    import inspect

    src = inspect.getsource(SimulationEngine)
    assert "_agent_locks.get(" not in src, (
        "a call site is bypassing acquire_all for _agent_locks"
    )
    assert "_thread_locks.get(" not in src, (
        "a call site is bypassing acquire_all for _thread_locks"
    )


def test_agent_lock_never_precedes_thread_lock_in_the_same_function_scope():
    """Task review, Important 3: the previous test greps for registry
    BYPASSES, not order INVERSIONS — a call site written as
    ``async with self._agent_locks.acquire_all(a): async with
    self._thread_locks.acquire_all(t): ...`` passes it cleanly and still
    deadlocks against the reply lane's thread-lock-then-agent-lock nesting.

    This is a real static check (line-order via the AST), not a grep: for
    every method of `SimulationEngine` (and every function nested inside one,
    checked as its OWN separate scope — a nested function's own calls must
    not be misattributed to its enclosing method, and vice versa), collect
    every `self._agent_locks.acquire_all(...)` / `self._thread_locks.
    acquire_all(...)` call textually in that scope and assert no agent-lock
    call has a smaller line number than a later thread-lock call. Line order
    within one scope is a reasonable proxy for acquisition order here — every
    real call site in this file acquires at most one of the two locks
    directly (the other, if any, is nested one level down. inside a call this
    scope makes to a DIFFERENT method), so there is no control-flow subtlety
    (branching, loops) between sibling `acquire_all` calls in any one scope
    today for this to falsely wave through.

    Verified this actually catches the counterexample above: parsing that
    exact snippet in isolation reports one violation.
    """
    import ast
    import inspect

    import src.agent.simulation as sim_module

    source = inspect.getsource(sim_module)
    tree = ast.parse(source)

    class_node = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "SimulationEngine"
    )

    def _lock_calls(node, calls, top):
        # Do NOT descend into a nested function's body when collecting for
        # the OUTER scope -- it is checked separately, as its own scope.
        if not top and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "acquire_all":
                obj = func.value
                if (
                    isinstance(obj, ast.Attribute)
                    and isinstance(obj.value, ast.Name)
                    and obj.value.id == "self"
                ):
                    if obj.attr == "_agent_locks":
                        calls.append((node.lineno, "agent"))
                    elif obj.attr == "_thread_locks":
                        calls.append((node.lineno, "thread"))
        for child in ast.iter_child_nodes(node):
            _lock_calls(child, calls, top=False)

    violations: list[str] = []

    def _check_scope(func_node, path):
        calls: list[tuple[int, str]] = []
        _lock_calls(func_node, calls, top=True)
        seen_agent_at = None
        for lineno, kind in sorted(calls):
            if kind == "agent":
                seen_agent_at = lineno
            elif kind == "thread" and seen_agent_at is not None:
                violations.append(
                    f"{path}: thread lock acquire_all at line {lineno} "
                    f"follows an agent lock acquire_all at line "
                    f"{seen_agent_at} in the SAME function scope — thread "
                    "lock must be acquired before agent lock, never after"
                )
        for child in ast.walk(func_node):
            if child is not func_node and isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                _check_scope(child, f"{path}.{child.name}")

    for item in class_node.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            _check_scope(item, item.name)

    assert not violations, "\n".join(violations)


@pytest.mark.asyncio
async def test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller(
    monkeypatch,
):
    """Drives the two REAL nesting paths this ruling added to the brief:
    `_close_thread` takes agent locks while the reply lane's thread lock is
    already held (simulated here exactly as `_dispatch_reply_lane._run`
    holds it), concurrently with `_phase5_new_post`, which takes ONLY an
    agent lock and never a thread lock.

    Engineered contention: `_update_agent_memory` (awaited twice inside
    `_close_thread`, once per agent) is slowed down so the close call is
    still holding BOTH agent locks — "blackbird" and "wang" — when the
    concurrent `_phase5_new_post(wang)` call attempts to acquire "wang" and
    is forced to genuinely block on it. If thread-lock-then-agent-lock
    nesting could deadlock against an agent-lock-only caller, this hangs;
    `asyncio.wait_for` turns that into a test failure instead.
    """
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    wang = Agent("wang", "WangBot", "Wang", role="pi_lab")

    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        message_count=3, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    # wang's own view of the same interview.
    wang.state.active_threads["t1"] = ThreadState(
        thread_id="t1", channel="general", other_agent_id="blackbird",
    )

    eng = SimulationEngine(agents=[hub, wang], slack_clients={})

    async def _slow_memory_update(agent, event, *a, **kw):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(eng, "_update_agent_memory", _slow_memory_update)
    # Force _phase5_new_post to return (inside its own agent-lock span, right
    # after the rate-limit check) without ever reaching a real LLM call —
    # this test is about lock nesting, not Phase 5's own behaviour.
    monkeypatch.setattr(wang, "try_reserve", lambda *a, **kw: False)

    async def _close_under_thread_lock():
        # Mirrors _dispatch_reply_lane._run: thread lock acquired first,
        # _close_thread (agent lock(s)) nested inside it.
        async with eng._thread_locks.acquire_all(thread.thread_id):
            await eng._close_thread(hub, thread, "no_proposal")

    close_task = asyncio.ensure_future(_close_under_thread_lock())
    # Let the close task run up to its first genuine suspension point (inside
    # the mocked _update_agent_memory) — by then it holds BOTH agent locks.
    await asyncio.sleep(0.001)

    phase5_task = asyncio.ensure_future(eng._phase5_new_post(wang))
    # Let phase5 register as a genuine waiter on wang's agent lock.
    await asyncio.sleep(0.001)

    await asyncio.wait_for(asyncio.gather(close_task, phase5_task), timeout=2.0)

    # Sanity: the close actually ran (not a false pass from both tasks no-op
    # returning without ever contending for anything).
    assert thread.status == "closed"
