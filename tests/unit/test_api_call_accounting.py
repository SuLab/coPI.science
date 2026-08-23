"""The throttle and the run summary must count API CALLS, not turns.

`Agent.record_api_call` already books six sites — the two reserved turns,
specialist consults, truncation retries, the working-memory update. What was
never booked is the extra TOOL ROUNDS inside `generate_with_tools`: a turn that
used three rounds before its final text call made four real, billed API calls
and was booked as one.

The obvious fix — "book `len(call_stats)`" — is wrong in the other direction. A
retry already fires `on_retry=agent.record_api_call`, and the two reserved sites
already book their own terminating call, so counting every entry double-books
both. The `kind` discriminator `call_stats` already carries is what separates
them: only `round` entries are unbooked.
"""
import types

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _settings(**kw):
    base = dict(
        cohort_isolation_enabled=False,
        cohort_default_policy="open",
        turn_delay_seconds=0.0,
        active_thread_threshold=12,
        llm_rate_window_seconds=600,
        llm_calls_per_load_per_window=8,
        reply_lane_max_in_flight=1,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())
    agent = Agent("hub", "HubBot", "PI hub")
    eng = SimulationEngine(agents=[agent], slack_clients={})
    # Nothing may be flushed: the buffer threshold would spawn a task and this
    # test has no database.
    eng._llm_log_flush_size = 10_000
    return eng


def _reserve(agent):
    """What `_reply_to_thread`/`_phase5_new_post` do before the LLM call."""
    assert agent.try_reserve(allowance=1000, window_s=600)
    agent.record_api_call(already_reserved=True)


def test_the_window_counts_every_api_call_in_a_multi_round_turn(engine):
    agent = engine.agents["hub"]
    _reserve(agent)

    # One `generate_with_tools` turn: three tool rounds, then the terminating
    # text call. FOUR real billed calls; the reservation booked one.
    engine._on_llm_call({
        "agent_id": "hub",
        "phase": "thread_reply",
        "call_stats": [
            {"seq": 1, "kind": "round"},
            {"seq": 2, "kind": "round"},
            {"seq": 3, "kind": "round"},
            {"seq": 4, "kind": "final"},
        ],
    })

    assert agent.api_call_count == 4, (
        "a 4-call turn was metered as "
        f"{agent.api_call_count} — the tool rounds are invisible to the "
        "throttle and to SimulationRun.total_api_calls"
    )
    assert len(agent.state.call_times) == 4


def test_a_retry_is_not_double_booked(engine):
    """The trap inside the trap: `len(call_stats)` would book this turn twice over."""
    agent = engine.agents["hub"]
    _reserve(agent)
    # llm.py fires `on_retry=agent.record_api_call` for the second real call.
    agent.record_api_call()

    engine._on_llm_call({
        "agent_id": "hub",
        "phase": "thread_reply",
        "call_stats": [
            {"seq": 1, "kind": "final"},
            {"seq": 2, "kind": "retry"},
        ],
    })

    assert agent.api_call_count == 2, (
        "a truncated-and-retried turn made TWO real calls; booking "
        f"len(call_stats) on top of the reservation and on_retry gives 4, "
        f"got {agent.api_call_count}"
    )
    assert len(agent.state.call_times) == 2


def test_the_forced_final_path_is_metered_call_for_call(engine):
    """`forced_final` terminates the loop in place of `final`; same arithmetic."""
    agent = engine.agents["hub"]
    _reserve(agent)
    agent.record_api_call()  # the forced-final retry's on_retry

    engine._on_llm_call({
        "agent_id": "hub",
        "phase": "thread_reply",
        "call_stats": [
            {"seq": 1, "kind": "round"},
            {"seq": 2, "kind": "round"},
            {"seq": 3, "kind": "forced_final"},
            {"seq": 4, "kind": "retry"},
        ],
    })

    assert agent.api_call_count == 4
    assert len(agent.state.call_times) == 4


def test_a_row_with_no_call_stats_books_nothing_extra(engine):
    """4,650 of 5,771 stored rows predate the column; they must not be re-counted."""
    agent = engine.agents["hub"]
    _reserve(agent)

    engine._on_llm_call({"agent_id": "hub", "phase": "thread_reply"})
    engine._on_llm_call({"agent_id": "hub", "phase": "thread_reply",
                         "call_stats": None})

    assert agent.api_call_count == 1
    assert len(agent.state.call_times) == 1


def test_an_unknown_agent_id_is_not_booked_anywhere(engine):
    """Consults and memory calls log under a real agent_id; a stray must not crash."""
    engine._on_llm_call({
        "agent_id": "not-on-the-roster",
        "call_stats": [{"seq": 1, "kind": "round"}, {"seq": 2, "kind": "final"}],
    })
    assert engine.agents["hub"].api_call_count == 0


def test_a_malformed_call_stats_payload_is_ignored(engine):
    """This runs inside a logging callback; it must never raise into the turn."""
    agent = engine.agents["hub"]
    for payload in ("not-a-list", [None, 3, "round"], [{"kind": None}], {}):
        engine._on_llm_call({"agent_id": "hub", "call_stats": payload})
    assert agent.api_call_count == 0


# ----------------------------------------------------------------------
# The units change has to reach a HUMAN. `SimulationRun.total_api_calls` is
# rendered in three admin templates; a staff member comparing the next run
# against 8b64a0e0 reads a number whose meaning silently changed.
# ----------------------------------------------------------------------


def test_the_startup_banner_declares_the_api_call_units(caplog):
    """An operator reads the startup banner, not a source comment.

    This sits beside `Screening rubric: version X (content hash Y)` for the same
    reason that line exists: it is the one place a change of meaning is visible
    to the person who has to interpret the numbers afterwards.
    """
    import logging

    from src.agent.main import _log_api_call_units

    with caplog.at_level(logging.INFO, logger="src.agent.main"):
        _log_api_call_units()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "total_api_calls" in text, f"the banner does not name the column: {text!r}"
    assert "api call" in text.lower(), f"the banner does not say what it counts: {text!r}"
    assert "turn" in text.lower(), (
        f"the banner must say what the number STOPPED counting: {text!r}"
    )
    assert "not comparable" in text.lower(), (
        f"the banner does not say the number broke comparability: {text!r}"
    )
    assert "llm_call_logs" in text and "COUNT(*)" in text, (
        "the banner must say how to recover the OLD figure, or an operator "
        f"cannot reconstruct a historical comparison: {text!r}"
    )


def test_the_banner_is_actually_logged_at_startup():
    """Parsed, not grepped — a mention in a comment must not satisfy this.

    Same lesson as `test_no_flusher_falls_back_on_a_bare_exception`: review
    defeated a substring assertion with a comment carrying the same text.
    """
    import ast
    import inspect
    import textwrap

    from src.agent import main as agent_main

    src = textwrap.dedent(inspect.getsource(agent_main._run_simulation))
    called = {
        node.func.id for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_log_api_call_units" in called, (
        "_run_simulation never calls the units banner, so no operator ever "
        "sees it"
    )
