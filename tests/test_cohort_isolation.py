"""Tests for cohort isolation (interaction gate) + the reactive-priority scheduler.

Covers:
- MessageLog sender filtering (get_new_top_level_posts / get_tags_for_agent /
  get_replies_to_agent_posts) with allowed_sender_ids.
- SimulationEngine._recompute_allowed_sender_ids (isolation on/off, uncohorted).
- SimulationEngine._owes_reply and the reactive-priority tier in _select_agent.
See specs/cohort-system.md.
"""

import types
import uuid

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry, MessageLog
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _post(ts, channel, agent_id, name, content, thread_ts=None, is_bot=True):
    return LogEntry(
        ts=ts,
        channel=channel,
        sender_agent_id=agent_id,
        sender_name=name,
        content=content,
        thread_ts=thread_ts,
        posted_at=float(ts),
        is_bot=is_bot,
    )


@pytest.fixture
def log():
    ml = MessageLog()
    ml.set_bot_name_map({
        "subot": "su", "wisemanbot": "wiseman", "cravattbot": "cravatt",
    })
    return ml


# ---------------------------------------------------------------
# MessageLog cohort filter — get_new_top_level_posts
# ---------------------------------------------------------------

class TestTopLevelSenderFilter:
    def test_none_allowed_no_filtering(self, log):
        """allowed_sender_ids=None (isolation off) → backward-compatible, no filter."""
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hi"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "hi"))
        posts = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su", allowed_sender_ids=None
        )
        assert {p.ts for p in posts} == {"1", "2"}

    def test_excludes_non_cohort_sender(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hi"))  # cohort-mate
        log.append(_post("2", "general", "cravatt", "CravattBot", "hi"))  # not a mate
        posts = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids={"wiseman"},
        )
        assert {p.ts for p in posts} == {"1"}

    def test_human_post_always_allowed(self, log):
        # Human PI post has sender_agent_id=None → passes the gate regardless.
        log.append(_post("1", "general", None, "Dr PI", "hello team", is_bot=False))
        log.append(_post("2", "general", "cravatt", "CravattBot", "hi"))
        posts = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids={"wiseman"},
        )
        assert {p.ts for p in posts} == {"1"}

    def test_empty_allowed_set_isolates(self, log):
        """An uncohorted agent (empty set) sees only human posts."""
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hi"))
        posts = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids=set(),
        )
        assert posts == []


# ---------------------------------------------------------------
# MessageLog cohort filter — tags + replies
# ---------------------------------------------------------------

class TestTagAndReplyFilter:
    def test_tags_from_non_cohort_excluded(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hey @SuBot"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "hey @SuBot"))
        tags = log.get_tags_for_agent("SuBot", since=0, allowed_sender_ids={"wiseman"})
        assert {t.ts for t in tags} == {"1"}

    def test_tags_none_allowed_no_filter(self, log):
        log.append(_post("1", "general", "cravatt", "CravattBot", "hey @SuBot"))
        tags = log.get_tags_for_agent("SuBot", since=0, allowed_sender_ids=None)
        assert len(tags) == 1

    def test_replies_from_non_cohort_excluded(self, log):
        log.append(_post("1", "general", "su", "SuBot", "my post"))
        log.append(_post("2", "general", "wiseman", "WisemanBot", "reply", thread_ts="1"))
        log.append(_post("3", "general", "cravatt", "CravattBot", "reply", thread_ts="1"))
        replies = log.get_replies_to_agent_posts(
            "su", since=0, allowed_sender_ids={"wiseman"}
        )
        assert {r.ts for r in replies} == {"2"}


# ---------------------------------------------------------------
# Engine — _recompute_allowed_sender_ids
# ---------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _engine(agent_ids, membership_rows=None, budget_cap=0):
    agents = [Agent(agent_id=a, bot_name=f"{a.capitalize()}Bot", pi_name=f"PI {a}") for a in agent_ids]
    factory = (lambda: _FakeDB(membership_rows)) if membership_rows is not None else None
    return SimulationEngine(
        agents=agents, slack_clients={}, budget_cap=budget_cap, session_factory=factory
    )


def _patch_isolation(monkeypatch, enabled, max_reactive=8):
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: types.SimpleNamespace(
            cohort_isolation_enabled=enabled,
            max_consecutive_reactive_turns=max_reactive,
            turn_delay_seconds=0.0,
        ),
    )


class TestRecomputeAllowedSenderIds:
    async def test_disabled_sets_none(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False)
        engine = _engine(["su", "wiseman"], membership_rows=[])
        await engine._recompute_allowed_sender_ids()
        assert all(a.allowed_sender_ids is None for a in engine.agents.values())

    async def test_enabled_computes_cohort_mates(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=True)
        c1 = uuid.uuid4()
        # su + wiseman share cohort c1; cravatt is uncohorted.
        rows = [(c1, "su"), (c1, "wiseman")]
        engine = _engine(["su", "wiseman", "cravatt"], membership_rows=rows)
        await engine._recompute_allowed_sender_ids()
        assert engine.agents["su"].allowed_sender_ids == {"su", "wiseman"}
        assert engine.agents["wiseman"].allowed_sender_ids == {"su", "wiseman"}
        # uncohorted → empty set (isolated)
        assert engine.agents["cravatt"].allowed_sender_ids == set()

    async def test_multi_cohort_union(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=True)
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        rows = [(c1, "su"), (c1, "wiseman"), (c2, "su"), (c2, "cravatt")]
        engine = _engine(["su", "wiseman", "cravatt"], membership_rows=rows)
        await engine._recompute_allowed_sender_ids()
        # su belongs to both cohorts → union of mates
        assert engine.agents["su"].allowed_sender_ids == {"su", "wiseman", "cravatt"}


# ---------------------------------------------------------------
# Engine — _owes_reply + reactive-priority scheduler
# ---------------------------------------------------------------

def _thread(agent, thread_id, other, pending=False):
    agent.state.active_threads[thread_id] = ThreadState(
        thread_id=thread_id, channel="general", other_agent_id=other,
        has_pending_reply=pending,
    )


class TestOwesReply:
    def test_true_when_pending_flag(self):
        engine = _engine(["su", "wiseman"])
        su = engine.agents["su"]
        _thread(su, "t1", "wiseman", pending=True)
        assert engine._owes_reply(su) is True

    def test_true_when_new_reply_from_other(self):
        engine = _engine(["su", "wiseman"])
        su = engine.agents["su"]
        _thread(su, "1", "wiseman", pending=False)
        # other agent posted in the thread after su's cursor
        engine.message_log.append(_post("1", "general", "su", "SuBot", "root"))
        engine.message_log.append(_post("2", "general", "wiseman", "WisemanBot", "reply", thread_ts="1"))
        su.state.last_seen_cursor = 0.0
        assert engine._owes_reply(su) is True

    def test_false_when_no_pending_and_no_new(self):
        engine = _engine(["su", "wiseman"])
        su = engine.agents["su"]
        _thread(su, "t1", "wiseman", pending=False)
        assert engine._owes_reply(su) is False

    def test_false_when_thread_not_active(self):
        engine = _engine(["su", "wiseman"])
        su = engine.agents["su"]
        _thread(su, "t1", "wiseman", pending=True)
        su.state.active_threads["t1"].status = "closed"
        assert engine._owes_reply(su) is False


class TestReactivePriority:
    def test_owed_agent_selected_first(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False)
        engine = _engine(["su", "wiseman", "cravatt"])
        # wiseman owes a reply; the others don't.
        _thread(engine.agents["wiseman"], "t1", "su", pending=True)
        assert engine._select_agent().agent_id == "wiseman"
        assert engine._reactive_streak == 1

    def test_oldest_waiting_owed_agent_wins(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False)
        engine = _engine(["su", "wiseman"])
        _thread(engine.agents["su"], "t1", "wiseman", pending=True)
        _thread(engine.agents["wiseman"], "t2", "su", pending=True)
        engine.agents["su"].state.last_selected = 100.0     # went recently
        engine.agents["wiseman"].state.last_selected = 5.0  # waiting longest
        assert engine._select_agent().agent_id == "wiseman"

    def test_excludes_last_llm_caller(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False)
        engine = _engine(["su", "wiseman"])
        _thread(engine.agents["su"], "t1", "wiseman", pending=True)
        _thread(engine.agents["wiseman"], "t2", "su", pending=True)
        # su is older (would win) but it just called — must yield to wiseman.
        engine.agents["su"].state.last_selected = 1.0
        engine.agents["wiseman"].state.last_selected = 50.0
        engine._last_llm_caller = "su"
        assert engine._select_agent().agent_id == "wiseman"

    def test_valve_forces_proactive_at_cap(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False, max_reactive=3)
        engine = _engine(["su", "wiseman"])
        _thread(engine.agents["wiseman"], "t1", "su", pending=True)
        engine._reactive_streak = 3  # at cap
        picked = engine._select_agent()
        assert picked is not None
        # Proactive path was taken → streak reset.
        assert engine._reactive_streak == 0

    def test_proactive_when_no_owed(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False)
        engine = _engine(["su", "wiseman"])
        # nobody owes a reply → weighted-random proactive path
        picked = engine._select_agent()
        assert picked.agent_id in {"su", "wiseman"}
        assert engine._reactive_streak == 0

    def test_no_candidates_returns_none(self, monkeypatch):
        _patch_isolation(monkeypatch, enabled=False)
        engine = _engine([])
        assert engine._select_agent() is None
