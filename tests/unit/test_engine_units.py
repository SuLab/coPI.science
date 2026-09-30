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


def test_headlines_and_verdicts_are_units_joined_by_the_ledger():
    from src.agent.engine.headlines import Headlines
    from src.agent.engine.helpers import _HeldVerdict
    from src.agent.engine.verdicts import Verdicts

    eng = _engine()
    assert sim._UNIT_CLASSES["headlines"] is Headlines
    assert sim._UNIT_CLASSES["verdicts"] is Verdicts
    assert eng.headlines._ledger is eng.verdicts
    held = _HeldVerdict(ordinal=3, final=False, slack_ts="1.1")
    eng._assessed_threads["t1"] = held
    assert eng.verdicts.held_for("t1") is held
    assert eng.verdicts.unannounced_thread_ids() == ["t1"]
    eng.verdicts.mark_announced("t1", held)
    assert eng._assessed_threads["t1"] == held._replace(announced=True)
    assert eng.verdicts.is_announced("t1") and eng.verdicts.unannounced_thread_ids() == []
    eng._pending_assessments.append({"thread_id": "t1"})
    eng.verdicts.patch_pending_summary("t1", "NOW")
    assert eng._pending_assessments == [{"thread_id": "t1", "summary_posted_at": "NOW"}]
    eng.headlines.enqueue("t2")
    eng.headlines.enqueue("t2")
    assert eng._pending_headlines == ["t2"]


@pytest.mark.asyncio
async def test_unbound_unit_calls_accept_an_engine():
    """Review Focus 2: tests call Verdicts._persist_assessment(engine, ...) with an
    engine as self; the split helpers must resolve through the facade too."""
    from src.agent.engine.verdicts import Verdicts

    eng = _engine()  # no database: the method must answer (False, None) as today
    held, row_id = await Verdicts._persist_assessment(eng, "blackbird", "general", {"scores": {}})
    assert (held, row_id) == (False, None)


def test_roster_is_a_unit_and_reads_rejections_through_the_port():
    from src.agent.engine.roster import Roster

    eng = _engine()
    assert sim._UNIT_CLASSES["roster"] is Roster
    assert eng._bot_name_to_id == {"subot": "su"}
    eng._post_type_rejections["pitch"] = 2
    assert eng.cohort_topology_snapshot()["counters"]["post_type_rejections"] == {"pitch": 2}


def test_run_announcer_is_a_unit():
    from src.agent.engine.run_announcer import RunAnnouncer

    eng = _engine()
    assert sim._UNIT_CLASSES["run_announcer"] is RunAnnouncer
    assert eng.run_announcer.max_runtime_minutes == eng.max_runtime_minutes


def test_threads_is_a_unit_with_the_rule_1_owner_api():
    from src.agent.engine.constants import PRIOR_THREADS_KEPT_PER_PAIR
    from src.agent.engine.threads import Threads

    eng = _engine()
    assert sim._UNIT_CLASSES["threads"] is Threads
    eng.threads.mark_closed("a", "b")
    assert {"a", "b"} <= eng._closed_thread_ids
    for i in range(PRIOR_THREADS_KEPT_PER_PAIR + 2):
        eng.threads.restore_prior(("su", "wu"), {"channel": "c", "outcome": str(i), "summary": None})
    kept = eng._prior_threads[("su", "wu")]
    assert len(kept) == PRIOR_THREADS_KEPT_PER_PAIR and kept[-1]["outcome"] == str(PRIOR_THREADS_KEPT_PER_PAIR + 1)
    t = eng.threads.activate_thread(
        eng.agents["su"], "9.1", channel="general", other_agent_id="wu",
        message_count=2, has_pending_reply=True, floor_armed=False,
    )
    assert eng.agents["su"].state.active_threads["9.1"] is t
    assert (t.message_count, t.has_pending_reply, t.floor_armed) == (2, True, False)


def test_post_lane_is_a_unit():
    from src.agent.engine.post_lane import PostLane

    eng = SimulationEngine(agents=[Agent("su", "SuBot", "PI su")], slack_clients={}, max_proposals=4)
    assert sim._UNIT_CLASSES["post_lane"] is PostLane
    assert eng.post_lane.max_proposals == 4
    assert eng.post_lane._strip_disallowed_tags.__self__ is eng.roster


def test_reply_lane_is_a_unit():
    from src.agent.engine.reply_lane import ReplyLane

    eng = _engine()
    assert sim._UNIT_CLASSES["reply_lane"] is ReplyLane
    eng._running = True
    assert eng.reply_lane._running is True
    assert eng._reply_sem is eng.reply_lane._reply_sem


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
