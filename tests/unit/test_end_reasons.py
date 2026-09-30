"""Spec P0-04: a closed end-reason vocabulary, and where each reason is recorded."""
import inspect
from datetime import UTC, datetime, timedelta

import pytest

import src.agent.main as agent_main
from src.agent.agent import Agent
from src.agent.end_reasons import (
    END_REASON_CLASS,
    FINALIZE,
    HOLD,
    TODAY,
    end_reason_class,
    stronger_reason,
)
from src.agent.simulation import SimulationEngine


def test_the_vocabulary_is_closed_and_classed():
    assert END_REASON_CLASS == {
        "operator": TODAY, "signal": TODAY,
        "time_limit": FINALIZE, "target_drained": FINALIZE,
        "operator_hold": HOLD, "stall": HOLD, "exception": HOLD,
        "start_failed": HOLD, "lock_lost": HOLD,
    }
    with pytest.raises(ValueError):
        end_reason_class("bogus")
    with pytest.raises(ValueError):
        stronger_reason(None, "bogus")


@pytest.mark.parametrize("current,new,expected", [
    (None, "operator", "operator"),
    ("operator", "signal", "operator"),          # first within a class stays
    ("signal", "time_limit", "time_limit"),      # FINALIZE beats TODAY
    ("time_limit", "operator", "time_limit"),    # a later reason never lowers
    ("operator_hold", "signal", "operator_hold"),
    ("signal", "stall", "stall"),                # HOLD beats TODAY
    ("stall", "exception", "stall"),
    ("target_drained", "exception", "exception"),
])
def test_a_later_reason_can_only_raise_the_class(current, new, expected):
    assert stronger_reason(current, new) == expected


def _engine(monkeypatch, tmp_path, agents):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    eng = SimulationEngine(agents=agents, slack_clients={})

    async def _noop(*a, **k):
        return None

    async def _zero(*a, **k):
        return 0

    for name in (
        "_poll_slack_for_bot_messages", "_sync_roster_from_db", "_poll_control_plane",
        "_drain_and_flush", "_sleep",
    ):
        monkeypatch.setattr(eng, name, _noop, raising=False)
    monkeypatch.setattr(eng, "_dispatch_reply_lane", _zero)
    monkeypatch.setattr(eng, "_sync_profiles_from_disk", lambda: None)
    eng._running = True
    return eng


def test_request_stop_records_and_validates(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, [])
    with pytest.raises(ValueError):
        eng.request_stop("bogus")
    assert eng._running is True and eng._end_reason is None, "a bad reason changes nothing"
    eng.request_stop()
    assert eng._end_reason == "operator" and eng._running is False
    eng.request_stop("stall")
    assert eng._end_reason == "stall"


def test_operator_hold_then_signal_still_holds(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, [])
    eng.request_stop("operator_hold")
    eng.request_stop("signal")
    assert eng._end_reason == "operator_hold"


async def test_an_empty_roster_stall_records_stall(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, [])
    await eng._run_main_loop()
    assert eng._end_reason == "stall"


async def test_an_exception_escaping_the_loop_records_exception(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, [Agent("wang", "WangBot", "Wang")])

    async def boom():
        raise RuntimeError("pair selection failed")

    monkeypatch.setattr(eng, "_dispatch_reply_lane", boom)
    with pytest.raises(RuntimeError, match="pair selection failed"):
        await eng._run_main_loop()
    assert eng._end_reason == "exception"


async def test_max_runtime_records_time_limit(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, [Agent("wang", "WangBot", "Wang")])
    eng.max_runtime_minutes = 1
    eng._start_time = datetime.now(UTC) - timedelta(minutes=5)
    await eng._run_main_loop()
    assert eng._end_reason == "time_limit" and eng._running is False


async def test_a_drained_proposal_target_records_target_drained(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, [Agent("wang", "WangBot", "Wang")])
    eng.max_proposals = 1
    eng._proposals_posted = 1
    monkeypatch.setattr(eng, "_select_agent", lambda: None)
    await eng._run_main_loop()
    assert eng._end_reason == "target_drained"


def test_the_signal_and_start_failure_paths_pass_their_reasons():
    assert 'self._run_state.request_stop("signal")' in inspect.getsource(agent_main._EarlySignal)
    assert 'sim_engine.request_stop("start_failed")' in inspect.getsource(
        agent_main._run_simulation_locked
    )
