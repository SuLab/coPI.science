"""The orchestrator facade: every moved member is still engine.<name>, reads, writes
and patches reach its one owner, and every unit sees a patch."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import src.agent.simulation as sim
from src.agent.agent import Agent
from src.agent.engine.context import EngineContext, RunState, _Via
from src.agent.simulation import SimulationEngine


def _engine() -> SimulationEngine:
    return SimulationEngine(agents=[Agent("su", "SuBot", "PI su")], slack_clients={})


def test_context_members_live_on_ctx_and_run_state():
    eng = _engine()
    assert isinstance(eng.ctx, EngineContext) and isinstance(eng.run_state, RunState)
    assert eng.agents is eng.ctx.agents
    assert eng._thread_locks is eng.ctx.thread_locks
    assert eng._agent_locks is eng.ctx.agent_locks
    sentinel = object()
    eng.session_factory = sentinel
    assert eng.ctx.session_factory is sentinel
    eng._running = True
    assert eng.run_state.running is True
    assert "_running" not in vars(eng) and "session_factory" not in vars(eng)


def test_request_stop_is_run_state_and_keeps_its_semantics():
    eng = _engine()
    eng._running = True
    eng.request_stop()
    assert eng.run_state.running is False
    assert eng.run_state.stop_event.is_set()
    assert eng._end_reason == "operator"
    with pytest.raises(ValueError):
        eng.request_stop("bogus")


def test_an_unknown_attribute_still_raises():
    with pytest.raises(AttributeError):
        _engine().no_such_member  # noqa: B018


def _engine_name_for(holder_attr: str, name: str) -> str | None:
    engine_holder = {"ctx": "ctx", "_run_state": "run_state"}.get(holder_attr, holder_attr.lstrip("_"))
    for engine_name, (h, target) in sim._FORWARD.items():
        if h == engine_holder and target == name:
            return engine_name
    return None


def test_a_patch_on_the_engine_reaches_every_unit(monkeypatch):
    eng = _engine()
    checked = 0
    for holder, cls in sim._UNIT_CLASSES.items():
        unit = getattr(eng, holder)
        for attr, value in vars(cls).items():
            if not isinstance(value, _Via):
                continue
            engine_name = _engine_name_for(value.holder, value.name)
            if engine_name is None:
                continue
            owner_holder, target = sim._FORWARD[engine_name]
            if isinstance(getattr(type(getattr(eng, owner_holder)), target, None), property):
                continue
            sentinel = object()
            monkeypatch.setattr(eng, engine_name, sentinel)
            assert getattr(unit, attr) is sentinel, (holder, attr, engine_name)
            monkeypatch.undo()
            checked += 1
    if sim._UNIT_CLASSES:
        assert checked > 0, "no via alias was exercised: the check is vacuous"


def test_patch_object_undo_restores_the_unit_method():
    eng = _engine()
    for name, (holder, target) in sim._FORWARD.items():
        owner = getattr(eng, holder)
        cls_attr = getattr(type(owner), target, None)
        if not callable(cls_attr) or isinstance(cls_attr, property):
            continue
        with patch.object(eng, name) as mock:
            assert getattr(owner, target) is mock, name
        assert target not in vars(owner), name
