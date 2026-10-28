"""Research rounds (src/services/project_research.py ``research_round``): lanes that take
research steps at once and back to back, with ``research_projects`` replaced by a fake."""

import asyncio
from types import SimpleNamespace

import pytest

from src.services import project_research as pr

FACTORY = object()


@pytest.mark.asyncio
async def test_lanes_run_at_once_and_take_steps_back_to_back(monkeypatch):
    work = {"left": 6, "taken": 0, "in_flight": 0, "peak": 0}

    async def step(session_factory):
        assert session_factory is FACTORY
        if work["left"] == 0:
            return False
        work["left"] -= 1
        work["in_flight"] += 1
        work["peak"] = max(work["peak"], work["in_flight"])
        await asyncio.sleep(0.01)
        work["in_flight"] -= 1
        work["taken"] += 1
        return True

    monkeypatch.setattr(pr, "research_projects", step)

    assert await pr.research_round(FACTORY, slots=3) is True
    assert work["taken"] == 6
    assert work["peak"] == 3


@pytest.mark.asyncio
async def test_a_round_ends_at_its_deadline(monkeypatch):
    calls = []

    async def step(session_factory):
        calls.append(None)
        return True

    monkeypatch.setattr(pr, "research_projects", step)
    monkeypatch.setattr(pr, "RESEARCH_ROUND_SECONDS", 0.0)

    assert await pr.research_round(FACTORY, slots=2) is True
    assert len(calls) == 2  # one step per lane, then the deadline


@pytest.mark.asyncio
async def test_nothing_due_is_one_look_per_lane(monkeypatch):
    calls = []

    async def step(session_factory):
        calls.append(None)
        return False

    monkeypatch.setattr(pr, "research_projects", step)

    assert await pr.research_round(FACTORY, slots=3) is False
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_lane_that_raises_ends_alone(monkeypatch):
    calls = []

    async def step(session_factory):
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError("a bug in one lane")
        return len(calls) <= 4

    monkeypatch.setattr(pr, "research_projects", step)

    assert await pr.research_round(FACTORY, slots=3) is True
    assert len(calls) >= 4


@pytest.mark.asyncio
async def test_a_round_takes_no_step_once_told_to_stop(monkeypatch):
    stop = {"now": False}
    calls = []

    async def step(session_factory):
        calls.append(None)
        stop["now"] = True  # the worker's SIGTERM lands during the first step
        return True

    monkeypatch.setattr(pr, "research_projects", step)

    assert await pr.research_round(FACTORY, slots=1, should_continue=lambda: not stop["now"])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slots_are_capped(monkeypatch):
    calls = []

    async def step(session_factory):
        calls.append(None)
        return False

    monkeypatch.setattr(pr, "research_projects", step)

    assert await pr.research_round(FACTORY, slots=50) is False
    assert len(calls) == pr.RESEARCH_MAX_LANES


@pytest.mark.asyncio
async def test_slots_default_to_the_setting(monkeypatch):
    calls = []

    async def step(session_factory):
        calls.append(None)
        return False

    monkeypatch.setattr(pr, "research_projects", step)
    monkeypatch.setattr(
        pr, "get_settings", lambda: SimpleNamespace(project_research_concurrency=2)
    )

    assert await pr.research_round(FACTORY) is False
    assert len(calls) == 2
