"""S1-04: reply LLM failures back off; a systemic failure opens the breaker and
abandons nothing; only a thread-specific non-transient repeat abandons."""
import anthropic
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.engine.context import LlmCircuitBreaker
from src.agent.engine.reply_lane import reply_backoff_seconds
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


def _api_error(cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message=f"{status}", response=httpx.Response(status, request=req), body=None)


class _Clock:
    """Stands in for the `time` module on the deps seam: a settable wall clock,
    everything else delegated to the real module."""

    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def __getattr__(self, name):
        import time as _time

        return getattr(_time, name)


def test_backoff_schedule():
    assert [reply_backoff_seconds(n) for n in (1, 2, 3, 4)] == [60, 240, 960, 1800]


def test_breaker_needs_three_failures_across_two_threads():
    b = LlmCircuitBreaker()
    b.record_failure("t1", 0.0)
    b.record_failure("t1", 1.0)
    b.record_failure("t1", 2.0)
    assert not b.is_open(3.0), "one thread alone never opens the breaker"
    b.record_failure("t2", 4.0)
    assert b.is_open(5.0) and not b.is_open(605.0)
    b.record_success()
    assert b.success_seq == 1


async def _setup(engine, monkeypatch, outcomes):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
                           session_factory=factory, simulation_run_id=run.id)
    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    clock = _Clock()
    # Patch the deps seam's `time` (spec §7.1), never the global time module.
    monkeypatch.setattr("src.agent.engine.deps.time", clock)

    async def _gen(**kw):
        item = outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr("src.agent.engine.deps.generate_with_tools", _gen)
    threads = {}
    for tid in ("A", "B", "C"):
        t = ThreadState(thread_id=tid, channel="c", other_agent_id="lab", has_pending_reply=True)
        hub.state.active_threads[tid] = t
        sim.ctx.message_log.append(LogEntry(ts=tid, channel="c", sender_agent_id="lab", sender_name="L",
                                            content="pitch", thread_ts=None, posted_at=1.0))
        threads[tid] = t
    return factory, run.id, sim, hub, threads, clock


async def _cleanup(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


@pytest.mark.asyncio
async def test_a_systemic_400_opens_the_breaker_and_abandons_nothing(engine, monkeypatch):
    errs = [_api_error(anthropic.BadRequestError, 400) for _ in range(3)]
    factory, run_id, sim, hub, threads, clock = await _setup(engine, monkeypatch, errs)
    try:
        for tid in ("A", "B", "C"):
            await sim.reply_lane._reply_to_thread(hub, threads[tid])
        assert sim.ctx.circuit.is_open(clock.now)
        assert all(t.has_pending_reply for t in threads.values())
        assert sim.reply_lane._pending_reply_pairs() == []
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_request_specific_repeat_400_abandons_one_thread(engine, monkeypatch):
    outcomes = [_api_error(anthropic.BadRequestError, 400), "<slack_message>ok</slack_message>",
                _api_error(anthropic.BadRequestError, 400)]
    factory, run_id, sim, hub, threads, clock = await _setup(engine, monkeypatch, outcomes)
    try:
        await sim.reply_lane._reply_to_thread(hub, threads["A"])
        await sim.reply_lane._reply_to_thread(hub, threads["B"])
        clock.now += 61
        await sim.reply_lane._reply_to_thread(hub, threads["A"])
        assert threads["A"].has_pending_reply is False
        async with factory() as db:
            drops = (await db.execute(select(AssessmentDrop).where(
                AssessmentDrop.simulation_run_id == run_id))).scalars().all()
        assert [d.reason for d in drops] == ["reply_failed"]
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_two_overloaded_errors_on_one_thread_do_not_abandon_it(engine, monkeypatch):
    outcomes = [_api_error(anthropic.OverloadedError, 529), "<slack_message>ok</slack_message>",
                _api_error(anthropic.OverloadedError, 529)]
    factory, run_id, sim, hub, threads, clock = await _setup(engine, monkeypatch, outcomes)
    try:
        await sim.reply_lane._reply_to_thread(hub, threads["A"])
        await sim.reply_lane._reply_to_thread(hub, threads["B"])
        clock.now += 61
        await sim.reply_lane._reply_to_thread(hub, threads["A"])
        assert threads["A"].has_pending_reply is True
        assert threads["A"].failed_call_count == 2
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_transient_error_retries_after_the_backoff(engine, monkeypatch):
    outcomes = [_api_error(anthropic.RateLimitError, 429)]
    factory, run_id, sim, hub, threads, clock = await _setup(engine, monkeypatch, outcomes)
    try:
        await sim.reply_lane._reply_to_thread(hub, threads["A"])
        pairs = {t.thread_id for _, t in sim.reply_lane._pending_reply_pairs()}
        assert "A" not in pairs and {"B", "C"} <= pairs
        clock.now += 61
        assert "A" in {t.thread_id for _, t in sim.reply_lane._pending_reply_pairs()}
    finally:
        await _cleanup(factory, run_id)


def test_reply_lane_did_work_counts_returned_calls():
    import inspect

    from src.agent.simulation import SimulationEngine

    src = inspect.getsource(SimulationEngine._run_main_loop)
    assert "success_seq" in src and "api_call_count" not in src.split("reply_lane_did_work")[1][:400]
