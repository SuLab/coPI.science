"""The races concurrency introduces, each asserted directly against a real
SimulationEngine wired to a real (throwaway) Postgres — a real DB round trip
end to end, not a mock standing in for one.

None of these is visible in a sequential run: every scenario below relies on
a monkeypatched LLM/Slack call that performs a genuine ``await`` (a real
``asyncio.sleep``), because an uncontended ``asyncio.Lock``/``Semaphore``
never actually suspends — a fake with no real await lets both "concurrent"
tasks race through their whole body in one scheduler step, which is exactly
how three previous tests in this same plan shipped green against locking
code that did not work (a tag-strip test, a lock-ordering test, and a
shutdown test — see task-14-report.md for how each of the tests below was
independently confirmed to fail with its guard removed).

See docs/specs/2026-08-14-two-lane-concurrent-scheduler-design.md §4 (the
races) and §9 (this table of tests).
"""
import asyncio
import time

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.config import get_settings
from src.models import OpportunityAssessment, SimulationRun, ThreadDecision
from tests.fakes import FakeSlackClient
from tests.unit.test_simulation_logic import _seed_thread_history

pytestmark = pytest.mark.integration


async def _make_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _delete_run(factory, run_id) -> None:
    async with factory() as cleanup:
        stale = (await cleanup.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await cleanup.delete(stale)  # cascades to assessments/decisions
            await cleanup.commit()


# A concluding hub reply: the ⏸️ inside <slack_message> is what
# `_check_thread_outcome` looks for to fire `_close_thread`, and the
# <assessment_json> sidecar outside it is what `_capture_hub_assessment`
# persists. Both must fire at most once for the whole race below.
_HUB_CONCLUDE_RESPONSE = (
    "<slack_message>\n"
    ":mag: Closing note — thanks for walking me through this. "
    "⏸️ No viable collaboration at this time.\n"
    "</slack_message>\n\n"
    "<assessment_json>\n"
    '{"subject_agent_id": "wang", "recommendation": "pass", '
    '"scores": {"differentiation": 2}}\n'
    "</assessment_json>"
)


# ---------------------------------------------------------------------------
# 1. Two simultaneous replies into ONE thread -> one CONCLUDE, one assessment,
#    one ThreadDecision (spec §4.1/§4.2). Isolates the THREAD LOCK
#    specifically: `_service_reply` is driven directly, wrapped in the exact
#    lock span `_dispatch_reply_lane._run` uses, bypassing the dispatcher's
#    own in-flight dedup set (that dedup is tested separately, in test 6
#    below) so this test cannot pass merely because the dispatcher never
#    spawned a second attempt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_replies_produce_one_conclude_and_one_assessment(
    engine, monkeypatch,
):
    """Both tasks read prior-count 11, both render MUST CONCLUDE, and
    opportunity_assessments has no uniqueness constraint to save us."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _make_run(factory)

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": client},
        session_factory=factory, simulation_run_id=run_id,
    )
    _seed_thread_history(eng, "t1", "general", 11)
    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    # The ⏸️ in the canned reply below fires _check_thread_outcome ->
    # _close_thread -> _update_agent_memory, which otherwise makes a REAL,
    # unmocked generate_agent_response (network) call — the actual cause of
    # an early flaky run of this exact test (~50% hang rate, confirmed via a
    # throwaway debug script tracing task progress: the hang was always AFTER
    # the fake generate_with_tools returned, inside _close_thread, never a
    # lock wait). Mocked the same way test_cross_agent_close_does_not_deadlock
    # below mocks it.
    async def _fast_memory_update(agent, event, *a, **kw):
        return None

    monkeypatch.setattr(eng, "_update_agent_memory", _fast_memory_update)

    async def _fake_generate(**kwargs):
        # The real await: without it both "concurrent" calls would race
        # through their entire body in one scheduler tick and the thread
        # lock's necessity would never be exercised.
        await asyncio.sleep(0.03)
        return _HUB_CONCLUDE_RESPONSE

    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_generate)

    async def _locked_service():
        # Mirrors _dispatch_reply_lane._run's exact span: the thread lock held
        # across the whole _service_reply call, including the LLM call.
        async with eng._thread_locks.acquire_all(thread.thread_id):
            await eng._service_reply(hub, thread)

    try:
        await asyncio.wait_for(
            asyncio.gather(_locked_service(), _locked_service()), timeout=10.0,
        )

        assert len(client.posted) == 1, (
            f"{len(client.posted)} replies posted for one interview turn"
        )
        async with factory() as check:
            rows = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
            decisions = (await check.execute(
                select(ThreadDecision).where(ThreadDecision.thread_id == "t1")
            )).scalars().all()
        assert len(rows) == 1, f"{len(rows)} assessments written for one interview"
        assert len(decisions) == 1, f"thread closed {len(decisions)} times"
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# 2. Concurrent Phase 5 for one lab must respect lab_daily_post_cap (spec
#    §4.5) — drives the REAL _count_today_posts/_post_message pipeline, not a
#    stubbed count, so the agent lock around the whole `_phase5_new_post` turn
#    is what's under test, not a mock standing in for it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_phase5_respects_the_daily_cap(engine, monkeypatch):
    """lab_daily_post_cap = 1; two concurrent Phase 5s both see 0 posts today
    unless the agent lock serialises them."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _make_run(factory)

    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab_client = FakeSlackClient(agent_id="wang")
    # FakeSlackClient mints ts values starting from a fixed 2023 constant, but
    # `_count_today_posts` compares `posted_at` against the REAL wall-clock
    # Pacific "today" boundary — left at the default, a post's ts is always
    # in the past and never counts as "today" regardless of locking, which
    # would make this test pass for the wrong reason (nothing ever hits the
    # cap). Anchor it to now so a real post is genuinely counted.
    lab_client._ts = int(time.time())
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"wang": lab_client, "blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "lab_daily_post_cap", 1)
    monkeypatch.setattr(settings, "phase5_skip_probability", 0.0)
    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr(lab, "build_phase5_prompt", lambda **kw: ("sys", []))

    async def _fake_generate(**kwargs):
        await asyncio.sleep(0.03)  # real yield forces genuine overlap
        return (
            '```json\n'
            '{"action": "new_post", "channel": "general", "post_type": "pitch"}\n'
            '```\n\n'
            '<slack_message>New idea: repurposing an existing compound.</slack_message>'
        )

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    try:
        await asyncio.wait_for(
            asyncio.gather(eng._phase5_new_post(lab), eng._phase5_new_post(lab)),
            timeout=10.0,
        )
        assert len(lab_client.posted) == 1, (
            f"{len(lab_client.posted)} pitches posted against a daily cap of 1"
        )
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# 3. A cross-agent close in both directions must complete, not deadlock (spec
#    §3.2/§4). `_close_thread` mutates the OTHER agent's state, so hub-closes-
#    against-lab and lab-closes-against-hub acquire the SAME two agent locks;
#    real contention (the two Opus-shaped `_update_agent_memory` calls slowed
#    down) forces the second close to genuinely suspend waiting on the first,
#    exactly the case sorted acquisition (§3.2) exists for. asyncio.wait_for
#    turns a hang into a failure rather than a stuck suite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_agent_close_does_not_deadlock(engine, monkeypatch):
    """_close_thread writes the other agent's state; opposite orders must not
    deadlock. wait_for turns a hang into a failure."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _make_run(factory)

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")

    # Interview 1, from the hub's side.
    thread_ab = ThreadState(thread_id="tAB", channel="general", other_agent_id="wang")
    hub.state.active_threads["tAB"] = thread_ab
    lab.state.active_threads["tAB"] = ThreadState(
        thread_id="tAB", channel="general", other_agent_id="blackbird",
    )
    # Interview 2, closed from the LAB's side — the opposite direction.
    thread_ba = ThreadState(thread_id="tBA", channel="general", other_agent_id="blackbird")
    lab.state.active_threads["tBA"] = thread_ba
    hub.state.active_threads["tBA"] = ThreadState(
        thread_id="tBA", channel="general", other_agent_id="wang",
    )

    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird"),
                       "wang": FakeSlackClient(agent_id="wang")},
        session_factory=factory, simulation_run_id=run_id,
    )

    async def _slow_memory_update(agent, event, *a, **kw):
        # Real await, genuinely holding both agent locks open while it runs —
        # long enough that the second close, if it can run at all, is forced
        # to actually wait rather than merely appear to by scheduling luck.
        await asyncio.sleep(0.05)

    monkeypatch.setattr(eng, "_update_agent_memory", _slow_memory_update)

    try:
        close_ab = asyncio.ensure_future(
            eng._close_thread(hub, thread_ab, "no_proposal")
        )
        # Let close_ab actually acquire both agent locks and reach its first
        # genuine suspension (inside the mocked _update_agent_memory) before
        # the opposite-direction close starts.
        await asyncio.sleep(0.001)

        close_ba = asyncio.ensure_future(
            eng._close_thread(lab, thread_ba, "no_proposal")
        )
        await asyncio.sleep(0.001)  # let it register as a genuine waiter

        await asyncio.wait_for(asyncio.gather(close_ab, close_ba), timeout=5.0)

        assert thread_ab.status == "closed"
        assert thread_ba.status == "closed"
        async with factory() as check:
            decisions = (await check.execute(
                select(ThreadDecision).where(
                    ThreadDecision.thread_id.in_(["tAB", "tBA"])
                )
            )).scalars().all()
        assert {d.thread_id for d in decisions} == {"tAB", "tBA"}, (
            "both closes should have recorded their own decision"
        )
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# 4. The reservation limiter's allowance must never be exceeded under N
#    concurrent callers (spec §5/§4.6) — driven through the REAL
#    `_dispatch_reply_lane` -> `_service_reply` -> `_reply_to_thread` call
#    path (Agent.try_reserve, called immediately before the LLM call), not a
#    bare call to `try_reserve` in isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reservation_limiter_holds_under_n_concurrent_repliers(
    engine, monkeypatch,
):
    """10 distinct threads (so the THREAD lock never serialises them) for one
    pi_lab agent, allowance clamped to 3 — at most 3 of the 10 concurrent
    reply attempts may reach the LLM."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _make_run(factory)

    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    n = 10
    for i in range(n):
        lab.state.active_threads[f"t{i}"] = ThreadState(
            thread_id=f"t{i}", channel="general", other_agent_id="blackbird",
            has_pending_reply=True,
        )
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"wang": FakeSlackClient(agent_id="wang"),
                       "blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    eng._running = True
    eng._reply_sem = asyncio.Semaphore(n)  # let all N genuinely race together

    settings = get_settings()
    monkeypatch.setattr(settings, "active_thread_threshold", 3)
    monkeypatch.setattr(settings, "llm_calls_per_load_per_window", 1)
    monkeypatch.setattr(settings, "llm_rate_window_seconds", 600)
    monkeypatch.setattr(lab, "build_phase4_prompt", lambda **kw: ("sys", []))

    calls = []

    async def _fake_generate(**kwargs):
        calls.append(1)
        await asyncio.sleep(0.02)  # real yield: all N genuinely overlap
        return "<slack_message>Sounds interesting, tell me more.</slack_message>"

    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_generate)

    allowance = eng._allowance_for(lab)
    assert allowance == 3, f"test fixture assumption broke: allowance={allowance}"

    try:
        await asyncio.wait_for(eng._dispatch_reply_lane(), timeout=10.0)
        assert len(calls) == allowance, (
            f"{len(calls)} LLM calls made against an allowance of {allowance}"
        )
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# 5. The event loop must stay responsive during a Slack post (spec §6.1),
#    driven through the engine's real `_post_message` call path (not just
#    AgentSlackClient.apost_message in isolation, which
#    tests/unit/test_slack_off_loop.py already covers) — tick-counting, as
#    tests/unit/test_llm_event_loop.py does for the LLM side of the same fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_a_slack_post(monkeypatch):
    _BLOCK_S = 0.30
    _TICK_S = 0.01

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    client = FakeSlackClient(agent_id="blackbird")

    def _blocking_post(channel, text, thread_ts=None):
        time.sleep(_BLOCK_S)  # imitates a real, slow Slack HTTP call
        return {"ts": "999.0", "channel": channel}

    monkeypatch.setattr(client, "post_message", _blocking_post)

    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": client}, session_factory=None,
        simulation_run_id=None,
    )

    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(_TICK_S)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # let the ticker reach its first await
    result = await eng._post_message("blackbird", "general", "hello")
    stop = True
    await t

    assert result == "999.0"
    # A free loop fits ~30 ticks into 0.30s; a pinned one manages the single
    # tick that ran before the post started.
    assert ticks > 5, (
        f"event loop was blocked for the whole Slack post (only {ticks} tick(s))"
    )


# ---------------------------------------------------------------------------
# 6. Dispatcher dedupe: a (agent, thread) pair already in flight must not be
#    spawned a second time by an overlapping `_dispatch_reply_lane` call
#    (spec §4.3) — this is the property the in-flight set adds ON TOP OF the
#    thread lock (test 1 above already proves the lock alone prevents a
#    second CONCLUDE/assessment/decision even without this set): without it,
#    the real `_service_reply` still gets ENTERED a second time (it just
#    no-ops quickly once inside, via its own closed/evicted guard), burning a
#    second Phase-3-reactivation pass and a second task-scheduling slot. The
#    spy below wraps (not replaces) the real method, so this counts actual
#    entries rather than final DB state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pair_already_in_flight_is_not_spawned_twice(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _make_run(factory)

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": client},
        session_factory=factory, simulation_run_id=run_id,
    )
    eng._running = True
    _seed_thread_history(eng, "t1", "general", 3)
    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _fake_generate(**kwargs):
        await asyncio.sleep(0.03)
        return "<slack_message>Tell me more about the mechanism.</slack_message>"

    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_generate)

    entries = []
    real_service_reply = eng._service_reply

    async def _spy(agent, th):
        entries.append(1)
        return await real_service_reply(agent, th)

    monkeypatch.setattr(eng, "_service_reply", _spy)

    try:
        await asyncio.wait_for(
            asyncio.gather(eng._dispatch_reply_lane(), eng._dispatch_reply_lane()),
            timeout=10.0,
        )
        assert entries == [1], (
            f"_service_reply was entered {len(entries)} times for one pair "
            "across two overlapping dispatch calls"
        )
        assert len(client.posted) == 1
    finally:
        await _delete_run(factory, run_id)
