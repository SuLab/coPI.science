"""Restart fidelity of SimulationEngine._rebuild_agent_state, offline.

`test_full_run_live.py::test_sigterm_and_restart_lose_nothing_and_duplicate_nothing`
covers this through a real SIGTERM against real Slack with real LLM turns. That
test needs workspace credentials and costs money, so the invariants it asserts
about *conversational state* are pinned here as well, with Slack off and no LLM:

* an open thread stored in `agent_messages` before a restart is back in
  `agent.state.active_threads` after the rebuild, exactly once (the live test's
  "no open thread survived the restart" assertion);
* a thread with a `ThreadDecision` is NOT reopened (the live test's "a concluded
  thread was reopened by the rebuild" assertion);
* running the rebuild twice changes nothing (`start()` calls it once today, so
  this is the property that keeps a second caller from silently double-counting).

The engine is driven at the same seam the live test's phase B uses — the real
`_rebuild_state_from_db()` then the real `_rebuild_agent_state()` — because
`_rebuild_agent_state` reads `self.message_log`, not `agent_messages`: the DB
pass is what puts the rows in the log, so testing the second without the first
would test a rebuild of an empty log.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.agent.transport import NullTransport
from src.config import get_settings
from tests import factories

pytestmark = pytest.mark.integration

AGENT_IDS = ("su", "wiseman")


class _FrozenClock:
    """Stand-in for the module-level `datetime` name in src.agent.simulation.

    Step 4b's cutoff is `datetime.now(UTC) - timedelta(...)`; by inspection,
    neither `_rebuild_state_from_db` nor `_rebuild_agent_state` calls
    `datetime` anywhere else, so stubbing just `.now()` pins the cutoff to an
    exact instant and lets the boundary test assert `>=` inclusivity without
    racing the real wall clock.
    """

    def __init__(self, fixed_now):
        self._fixed_now = fixed_now

    def now(self, tz=None):
        return self._fixed_now


class _FixtureSessionFactory:
    """Route the engine's self-opened sessions at the rolled-back test session.

    Same shim as `test_message_persistence.py` uses, and for the same reason: the
    rebuild does ``async with self.session_factory() as db:`` and must see the
    rows this test wrote inside its own (rolled-back) transaction. __aexit__ must
    NOT close the fixture-owned session.
    """

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


def _engine_for(session, run_id, agent_ids=AGENT_IDS):
    """A real SimulationEngine with Slack off and no budget."""
    agents = [
        Agent(agent_id=a, bot_name=f"{a.capitalize()}Bot", pi_name=f"PI {a}")
        for a in agent_ids
    ]
    return SimulationEngine(
        agents=agents,
        slack_clients={a: NullTransport(a) for a in agent_ids},
        budget_cap=0,
        session_factory=_FixtureSessionFactory(session),
        simulation_run_id=run_id,
        slack_enabled=False,
    )


async def _stored_thread(session, run, *, root="su", replier="wiseman",
                         channel="general", replies=3):
    """Write one root post + `replies` replies as rows from a previous process.

    Timestamps are anchored to now: `_rebuild_state_from_db` windows the load to
    REBUILD_WINDOW_S (14 days) OR-ed with "has no ThreadDecision", so an
    epoch-1970 ts would be rescued by the OR clause in the open-thread tests and
    silently dropped in the closed-thread one. Anchoring to now removes the
    window as a variable.
    """
    base = round(time.time(), 4)
    root_ts = f"{base:.6f}"
    await factories.make_agent_message(
        session, run=run, agent_id=root,
        channel_id="C1", channel_name=channel,
        message_ts=root_ts, thread_ts=None, posted_at=base,
        content=f"root post by {root}", sender_name=f"{root.capitalize()}Bot",
        is_bot=True,
    )
    for i in range(replies):
        ts = f"{base + i + 1:.6f}"
        await factories.make_agent_message(
            session, run=run, agent_id=replier,
            channel_id="C1", channel_name=channel,
            message_ts=ts, thread_ts=root_ts, posted_at=base + i + 1,
            content=f"reply {i} by {replier}",
            sender_name=f"{replier.capitalize()}Bot", is_bot=True,
        )
    await session.flush()
    return root_ts


async def test_an_open_thread_survives_a_rebuild_exactly_once(db_session):
    run = await factories.make_simulation_run(db_session)
    root_ts = await _stored_thread(db_session, run, replies=3)

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    su = eng.agents["su"]
    assert list(su.state.active_threads) == [root_ts], (
        f"expected exactly the one open thread, got {list(su.state.active_threads)}"
    )
    t = su.state.active_threads[root_ts]
    assert t.other_agent_id == "wiseman"
    assert t.channel == "general"
    assert t.message_count == 4, (
        f"the whole thread must be restored, not just the root: {t.message_count}"
    )
    assert t.has_pending_reply is True, (
        "the last message was the partner's, so su still owes a reply — losing this "
        "is how a restart ghosts a conversation"
    )
    # The partner side too: both participants track the thread.
    assert list(eng.agents["wiseman"].state.active_threads) == [root_ts]

    # A second rebuild must be idempotent — restart is not always one-shot.
    await eng._rebuild_agent_state()
    assert list(su.state.active_threads) == [root_ts], (
        f"a second rebuild changed the thread set: {list(su.state.active_threads)}"
    )
    assert su.state.active_threads[root_ts].message_count == 4


async def test_a_decided_thread_is_not_reopened_by_a_rebuild(db_session):
    """The live test asserts `not (restored & decided_a)`. Pinned offline."""
    run = await factories.make_simulation_run(db_session)
    root_ts = await _stored_thread(db_session, run, replies=2)
    await factories.make_thread_decision(
        db_session, run=run, thread_id=root_ts, channel="general",
        agent_a="su", agent_b="wiseman", outcome="no_proposal",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    assert eng.agents["su"].state.active_threads == {}, (
        "a thread with a ThreadDecision was reopened by the rebuild: "
        f"{list(eng.agents['su'].state.active_threads)}"
    )
    assert root_ts in eng._closed_thread_ids


async def test_a_second_rebuild_does_not_duplicate_restored_proposals(db_session):
    """`pending_proposals` is a list and step 3 appends to it without clearing.

    Every unreviewed entry blocks the owning agent, so a duplicated one is not
    cosmetic: it survives the single pop that reviewing it performs.
    """
    run = await factories.make_simulation_run(db_session)
    td = await factories.make_thread_decision(
        db_session, run=run, thread_id="1500.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="proposal",
        summary_text="a shared aim",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    su = eng.agents["su"]
    assert [p.thread_id for p in su.state.pending_proposals] == [td.thread_id]

    await eng._rebuild_agent_state()
    assert [p.thread_id for p in su.state.pending_proposals] == [td.thread_id], (
        "a second rebuild duplicated the restored proposal: "
        f"{[p.thread_id for p in su.state.pending_proposals]}"
    )


async def test_a_second_rebuild_does_not_duplicate_prior_thread_context(db_session):
    """`_prior_threads` is the Phase 5 dedup context, and step 1 appends to it.

    Duplicated entries are fed to the model as "you already discussed this N
    times", which is a prompt corruption rather than a crash — so it needs a
    test, not a reader.
    """
    run = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=run, thread_id="1600.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="no_proposal",
        summary_text="did not converge",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()
    assert len(eng._prior_threads[("su", "wiseman")]) == 1

    await eng._rebuild_agent_state()
    assert len(eng._prior_threads[("su", "wiseman")]) == 1, (
        "a second rebuild duplicated the prior-thread dedup context: "
        f"{eng._prior_threads[('su', 'wiseman')]}"
    )


async def test_call_times_rebuilds_from_the_window_and_api_call_count_stays_all_time(
    db_session, monkeypatch,
):
    """DB round trip through the REAL step 4b query — not a hand-built deque.

    The unit tests in test_hub_budget_scheduler.py::TestRestartRebuild hand-populate
    `call_times` and re-check `_within_rate_limit`/`_turn_eligible`; they never invoke
    the query itself. This test seeds `llm_call_logs` rows straddling the rate-limit
    window boundary for one agent — including a row exactly ON the cutoff, to pin the
    `>=` in the WHERE clause — and asserts the rebuilt `call_times` holds only the
    in-window rows, oldest-first (`.order_by(created_at)` is load-bearing: the
    rate limiter prunes with `popleft()` and assumes oldest-first).

    `api_call_count` must still reflect every row, including the out-of-window one:
    step 4 (lifetime COUNT(*)) and step 4b (windowed call_times) read the same table
    but must stay independent, or a restart would either bench an agent that isn't
    actually over budget, or silently forgive one that is.
    """
    run = await factories.make_simulation_run(db_session)
    window = get_settings().llm_rate_window_seconds
    frozen_now = datetime(2026, 1, 1, tzinfo=UTC)
    cutoff = frozen_now - timedelta(seconds=window)

    outside = cutoff - timedelta(seconds=1)  # just before cutoff: excluded
    boundary = cutoff  # exactly on cutoff: included (>=)
    inside_older = cutoff + timedelta(seconds=100)
    inside_newer = cutoff + timedelta(seconds=500)

    for ts in (outside, boundary, inside_older, inside_newer):
        await factories.make_llm_call_log(
            db_session, run=run, agent_id="su", created_at=ts,
        )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    monkeypatch.setattr("src.agent.simulation.datetime", _FrozenClock(frozen_now))
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    su = eng.agents["su"]
    assert list(su.state.call_times) == pytest.approx([
        boundary.timestamp(), inside_older.timestamp(), inside_newer.timestamp(),
    ]), (
        "call_times must hold only the in-window rows, oldest first: "
        f"{list(su.state.call_times)}"
    )
    # step 4's lifetime COUNT(*) counts all 4 rows, unaffected by the window
    # filter that gated call_times above.
    assert su.api_call_count == 4


async def test_the_rebuild_counts_api_calls_not_rows(db_session, monkeypatch):
    """Steps 4 and 4b must be per-CALL now that live booking is (A3.7).

    `_on_llm_call` books the extra tool ROUNDS of a multi-round turn, so live
    `api_call_count`/`call_times` are per-API-call. If the restart rebuild stayed
    per-ROW, every restart would silently loosen the throttle by the exact ratio
    of calls to turns (78.6% of stored `thread_reply` rows are 2+ calls).

    And it must be `COALESCE(jsonb_array_length(call_stats), 1)`, not a bare
    `jsonb_array_length`: 4,650 of the 5,771 stored rows have `call_stats IS
    NULL` (the column arrived in migration 0032), and a NULL there collapses the
    whole sum to NULL — loosening the throttle in the opposite direction.
    """
    run = await factories.make_simulation_run(db_session)
    frozen_now = datetime(2026, 1, 1, tzinfo=UTC)
    inside = frozen_now - timedelta(seconds=10)

    # One four-call turn (3 rounds + final)...
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="su", created_at=inside,
        call_stats=[
            {"seq": 1, "kind": "round"},
            {"seq": 2, "kind": "round"},
            {"seq": 3, "kind": "round"},
            {"seq": 4, "kind": "final"},
        ],
    )
    # ...and one pre-0032 row that records nothing, which is worth exactly 1.
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="su", created_at=inside, call_stats=None,
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    monkeypatch.setattr("src.agent.simulation.datetime", _FrozenClock(frozen_now))
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    su = eng.agents["su"]
    assert su.api_call_count == 5, (
        "the lifetime rebuild counted ROWS (2) rather than calls (4 + 1): "
        f"{su.api_call_count}"
    )
    assert len(su.state.call_times) == 5, (
        "the window rebuild counted ROWS rather than calls, so a restart "
        f"loosens the throttle: {list(su.state.call_times)}"
    )


async def test_a_second_rebuild_does_not_duplicate_call_times(db_session, monkeypatch):
    """Step 4b clears each agent's ledger before repopulating it.

    Same idempotency concern the pending_proposals and _prior_threads rebuilds above
    document, applied to the sliding-window ledger: a plain, unguarded append would
    duplicate every in-window entry on a second rebuild call and could throttle an
    agent that is not actually over its allowance. Only one call site exists today
    (`start()`), so this is latent, not live — pinned here so it stays that way.
    """
    run = await factories.make_simulation_run(db_session)
    frozen_now = datetime(2026, 1, 1, tzinfo=UTC)
    await factories.make_llm_call_log(
        db_session, run=run, agent_id="su",
        created_at=frozen_now - timedelta(seconds=10),
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    monkeypatch.setattr("src.agent.simulation.datetime", _FrozenClock(frozen_now))
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    su = eng.agents["su"]
    assert len(su.state.call_times) == 1

    await eng._rebuild_agent_state()
    assert len(su.state.call_times) == 1, (
        "a second rebuild duplicated the call_times ledger: "
        f"{list(su.state.call_times)}"
    )


async def test_the_rebuild_ignores_another_runs_thread_decisions(db_session):
    """A --fresh run must not inherit prior runs' interview outcomes: an
    unfiltered thread_decisions read fed every earlier run's closing summaries
    into Phase-5 prompts as 'you already pitched this' (audit F2)."""
    run = await factories.make_simulation_run(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=other, thread_id="9999.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="no_proposal",
        summary_text="FOREIGN-RUN-SUMMARY",
    )
    # Same-run control: over-filtering would be its own regression.
    await factories.make_thread_decision(
        db_session, run=run, thread_id="7777.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="no_proposal",
        summary_text="THIS-RUN-SUMMARY",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    pair = tuple(sorted(["su", "wiseman"]))
    summaries = [t["summary"] for t in eng._prior_threads.get(pair, [])]
    assert summaries == ["THIS-RUN-SUMMARY"], summaries
    assert "7777.000100" in eng._closed_thread_ids
    assert "9999.000100" not in eng._closed_thread_ids, (
        "another run's closed-thread ids leaked into this run's closed set"
    )


async def test_the_rebuild_ignores_another_runs_proposals(db_session):
    """A prior run's unreviewed proposal must not bench a fresh run's agent,
    and a prior run's collab_private proposal must not pre-finalize a
    same-named private channel (audit F3)."""
    run = await factories.make_simulation_run(db_session)
    other = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=other, thread_id="8888.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="proposal",
    )
    await factories.make_thread_decision(
        db_session, run=other, thread_id="8888.000200", channel="prv-su-wiseman",
        agent_a="su", agent_b="wiseman", outcome="proposal",
        origin_visibility="collab_private",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    assert eng.agents["su"].state.pending_proposals == []
    assert eng.agents["wiseman"].state.pending_proposals == []
    assert "prv-su-wiseman" not in eng._finalized_private_channels


async def test_the_rebuild_still_loads_this_runs_proposals(db_session):
    """Positive control for the new filter."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(
        db_session, run=run, thread_id="6666.000100", channel="general",
        agent_a="su", agent_b="wiseman", outcome="proposal",
    )
    await db_session.flush()

    eng = _engine_for(db_session, run.id)
    await eng._rebuild_state_from_db()
    await eng._rebuild_agent_state()

    assert [p.thread_id for p in eng.agents["su"].state.pending_proposals] == ["6666.000100"]


async def test_a_thread_with_no_root_in_the_log_is_evicted_not_replied(
    db_session, monkeypatch
):
    """A reply ingested into a thread whose parent this run never saw (e.g. a
    foreign bot's thread_broadcast into a PREVIOUS run's interview) must be
    evicted, not answered: get_thread_history would hand the LLM a one-message
    'history' and restart a concluded interview at ordinal 2 (audit F4).

    Nothing else on the unguarded path stops this turn — the participation
    check waves a missing root through (allowed is None) and budget_cap=0 is
    the INERT legacy cap, not zero budget — so without the monkeypatches the
    RED run would reach a real Anthropic call.
    """
    run = await factories.make_simulation_run(db_session)
    eng = _engine_for(db_session, run.id)
    su = eng.agents["su"]

    monkeypatch.setattr(su, "build_phase4_prompt", lambda **kw: ("sys", []))

    async def _must_not_run(**kwargs):
        raise AssertionError("an orphan thread must never reach the model")

    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _must_not_run)

    orphan_root = "1111.000100"
    eng.message_log.append(LogEntry(
        ts="1111.000200", channel="general",
        sender_agent_id="wiseman", sender_name="WisemanBot",
        content="reply into a previous run's thread",
        thread_ts=orphan_root, posted_at=1111.0002, is_bot=True,
    ))
    thread = ThreadState(
        thread_id=orphan_root, channel="general", other_agent_id="wiseman",
        message_count=1, has_pending_reply=True,
    )
    su.state.active_threads[orphan_root] = thread

    await eng._reply_to_thread(su, thread)

    assert orphan_root not in su.state.active_threads, "orphan thread not evicted"
    assert orphan_root in eng._closed_thread_ids, (
        "eviction must pin the id closed or Phase 3 re-activates it next tick"
    )
