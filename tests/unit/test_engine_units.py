"""Each spec §7.1 unit exists, is wired into the orchestrator, and owns its state."""

import ast
import inspect
import textwrap

import pytest

import src.agent.simulation as sim
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine():
    return SimulationEngine(agents=[Agent("su", "SuBot", "PI su")], slack_clients={})


def test_persistence_is_a_unit():
    from src.agent.engine.persistence import Persistence

    eng = _engine()
    assert sim._UNIT_CLASSES["persistence"] is Persistence
    assert isinstance(eng.persistence, Persistence)
    assert eng._pending_persist is eng.persistence._pending_persist
    assert eng._persist_flush_lock is eng.persistence._persist_flush_lock
    assert eng.persistence.ctx.channel_id_resolver("nope") is None


def test_channel_directory_is_a_unit_and_the_resolver():
    from src.agent.engine.channel_directory import ChannelDirectory

    eng = _engine()
    assert sim._UNIT_CLASSES["channel_directory"] is ChannelDirectory
    eng._channel_id_map = {"general": "C123"}
    assert eng.channel_directory._channel_id_map == {"general": "C123"}
    assert eng.ctx.channel_id_resolver("general") == "C123"
    assert eng.ctx.channel_id_resolver == eng.channel_directory.channel_id_for


def test_memory_is_a_unit_and_enqueue_appends():
    from src.agent.engine.memory import Memory

    eng = _engine()
    assert sim._UNIT_CLASSES["memory"] is Memory
    eng.memory.enqueue(("su", "closed", "public", None))
    assert eng._pending_memory_events == [("su", "closed", "public", None)]


def test_control_is_a_unit_and_stops_through_run_state():
    from src.agent.engine.control import Control

    eng = _engine()
    assert sim._UNIT_CLASSES["control"] is Control
    eng._running = True
    eng.control.request_stop()
    assert eng._running is False and eng._end_reason == "operator"


def test_llm_log_is_a_unit():
    from src.agent.engine.llm_log import LlmLog

    eng = _engine()
    assert sim._UNIT_CLASSES["llm_log"] is LlmLog
    assert eng._on_llm_call.__self__ is eng.llm_log
    assert eng.llm_log._recover_rows_individually.__self__ is eng.persistence


@pytest.mark.asyncio
async def test_slack_io_is_a_unit_and_reports_gone_threads_through_the_port(monkeypatch):
    from src.agent.engine.slack_io import SlackIO

    eng = _engine()
    assert sim._UNIT_CLASSES["slack_io"] is SlackIO
    seen = []

    async def gone(thread_id):
        seen.append(thread_id)

    monkeypatch.setattr(eng, "_evict_dead_thread", gone)
    await eng.slack_io._on_thread_gone("1.2")
    assert seen == ["1.2"]
    eng.slack_io.seed_cursor("C1", "5.0")
    eng.slack_io.seed_cursor("C1", "4.0")
    assert eng._poll_cursors["C1"] == "5.0"


def test_scheduler_is_a_unit():
    from src.agent.engine.scheduler import Scheduler

    eng = SimulationEngine(agents=[Agent("su", "SuBot", "PI su")], slack_clients={},
                           max_runtime_minutes=7, budget_cap=3)
    assert sim._UNIT_CLASSES["scheduler"] is Scheduler
    assert eng.scheduler.max_runtime_minutes == 7 and eng.budget_cap == 3
    assert eng.is_within_time_limit is eng.scheduler.is_within_time_limit


def test_panel_is_a_unit():
    from src.agent.engine.panel import Panel

    eng = _engine()
    assert sim._UNIT_CLASSES["panel"] is Panel
    assert eng._specialist_consults is eng.panel._specialist_consults
    assert eng.panel._post_message.__self__ is eng.slack_io


def test_every_self_name_in_a_unit_resolves():
    """A moved body's ``self.<name>`` must be an own member, an owned attribute, a
    ``via`` alias, a holder or a port. A missing alias would otherwise only log,
    because many moved bodies sit inside ``except Exception``."""
    problems = []
    for holder, cls in sim._UNIT_CLASSES.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
        known = set(vars(cls)) | set(cls.OWNED_STATE)
        known |= {
            n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "self" and isinstance(n.ctx, (ast.Store, ast.Del))
        }
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id == "self" and isinstance(n.ctx, ast.Load)
                    and n.attr not in known):
                problems.append(f"{holder}:{n.lineno} self.{n.attr}")
    assert problems == [], "\n".join(problems)
