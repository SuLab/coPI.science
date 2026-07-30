"""Cohort interaction gate + reactive-priority scheduler.

Implements the test plan in .notes/cohort-system-v2.md §15. Organised by spec
section so a failure names the rule it broke:

- TestGateHelper            §5.1  the per-entry decision table
- TestComputeGates          §5.2  policy semantics, shared by engine and admin
- TestPreflight             §5.3  refusing to silence a roster
- TestGatedReads            §6    MessageLog read filtering
- TestReadPathInventory     §6    every public read method is classified
- TestStatePruning          §6.1  stale interesting_posts
- TestDbPrimaryPaths        §6.2  ingestion is never gated; is_bot keying
- TestPrivateChannels       §7    PI pairings outrank the gate
- TestGrandfathering        §8    resumed runs, conclude-but-deprioritise
- TestTagHygiene            §9    outbound mention stripping
- TestScheduler             §10   eligibility, fairness valve, ratio counters
- TestTopologySnapshot      §13.1 provenance
- TestMigrationHygiene      §14   single head, no duplicate revision ids
"""

import inspect
import pathlib
import re
import types
import uuid

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry, MessageLog, _entry_allowed
from src.agent.simulation import SimulationEngine
from src.agent.state import PostRef, ThreadState
from src.services.cohorts import (
    POLICY_ISOLATED,
    POLICY_OPEN,
    compute_gates,
    preflight_reason,
    summarise_gates,
)
from src.visibility import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(
    ts, channel, agent_id, name, content,
    thread_ts=None, is_bot=True, visibility=VISIBILITY_PUBLIC,
):
    return LogEntry(
        ts=ts,
        channel=channel,
        sender_agent_id=agent_id,
        sender_name=name,
        content=content,
        thread_ts=thread_ts,
        posted_at=float(ts),
        is_bot=is_bot,
        visibility=visibility,
    )


class _FakeResult:
    """Mimics the slice of sqlalchemy Result the engine actually calls."""

    def __init__(self, rows, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _FakeDB:
    """Returns membership rows for the membership select, a count for the count."""

    def __init__(self, rows, cohort_count=None):
        self._rows = rows
        self._cohort_count = (
            cohort_count if cohort_count is not None
            else len({c for c, _ in rows})
        )
        self.added: list = []
        self.committed = False

    async def execute(self, stmt):
        text = str(stmt).lower()
        if "count" in text:
            return _FakeResult([], scalar=self._cohort_count)
        return _FakeResult(self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _engine(agent_ids, membership_rows=None, budget_cap=0, cohort_count=None):
    agents = [
        Agent(agent_id=a, bot_name=f"{a.capitalize()}Bot", pi_name=f"PI {a}")
        for a in agent_ids
    ]
    db = _FakeDB(membership_rows or [], cohort_count=cohort_count)
    factory = (lambda: db) if membership_rows is not None else None
    eng = SimulationEngine(
        agents=agents, slack_clients={}, budget_cap=budget_cap, session_factory=factory
    )
    name_map = {f"{a}bot": a for a in agent_ids}
    eng.message_log.set_bot_name_map(name_map)
    eng._bot_name_to_id = dict(name_map)
    eng._fake_db = db  # test handle
    return eng


def _settings(**kw):
    base = dict(
        cohort_isolation_enabled=False,
        cohort_default_policy=POLICY_OPEN,
        max_consecutive_reactive_turns=3,
        turn_delay_seconds=0.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _patch(monkeypatch, **kw):
    monkeypatch.setattr(
        "src.agent.simulation.get_settings", lambda: _settings(**kw)
    )


def _thread(agent, thread_id, other, pending=False, channel="general", grandfathered=False):
    agent.state.active_threads[thread_id] = ThreadState(
        thread_id=thread_id, channel=channel, other_agent_id=other,
        has_pending_reply=pending, grandfathered=grandfathered,
    )
    return agent.state.active_threads[thread_id]


# ---------------------------------------------------------------------------
# §5.1 — the per-entry decision table
# ---------------------------------------------------------------------------


class TestGateHelper:
    def test_gate_off_passes_everything(self):
        assert _entry_allowed(_post("1", "c", "z", "ZBot", "hi"), None) is True

    def test_human_always_passes(self):
        e = _post("1", "c", None, "Dr PI", "hello", is_bot=False)
        assert _entry_allowed(e, set()) is True
        assert _entry_allowed(e, {"su"}) is True

    def test_human_with_agent_id_still_passes(self):
        """is_bot is the human signal, not a NULL agent_id."""
        e = _post("1", "c", "su", "Dr PI", "hello", is_bot=False)
        assert _entry_allowed(e, set()) is True

    def test_bot_with_null_agent_id_fails_closed(self):
        """agent_messages.agent_id is nullable; an unattributable BOT row must not
        slip through the human bypass. This is the hole that keying on
        `sender_agent_id is None` opened."""
        e = _post("1", "c", None, "bot", "hi", is_bot=True)
        assert _entry_allowed(e, {"su"}) is False
        assert _entry_allowed(e, set()) is False

    def test_private_channel_always_passes(self):
        e = _post("1", "priv", "cravatt", "CravattBot", "hi",
                  visibility=VISIBILITY_COLLAB_PRIVATE)
        assert _entry_allowed(e, set()) is True
        assert _entry_allowed(e, {"su"}) is True

    def test_cohort_mate_passes_non_mate_does_not(self):
        assert _entry_allowed(_post("1", "c", "su", "SuBot", "hi"), {"su"}) is True
        assert _entry_allowed(_post("1", "c", "z", "ZBot", "hi"), {"su"}) is False

    def test_empty_set_blocks_all_bots(self):
        assert _entry_allowed(_post("1", "c", "su", "SuBot", "hi"), set()) is False


# ---------------------------------------------------------------------------
# §5.2 — policy semantics (the rule v1 documented and the code inverted)
# ---------------------------------------------------------------------------


class TestComputeGates:
    def test_isolation_disabled_gates_are_none(self):
        gates, reason = compute_gates(
            membership_rows=[], agent_ids=["su", "wiseman"],
            isolation_enabled=False, policy=POLICY_OPEN, cohort_count=0,
        )
        assert reason is None
        assert gates == {"su": None, "wiseman": None}

    def test_open_policy_zero_cohorts_is_a_no_op(self):
        """The contract v1 published: enabling isolation with no cohorts defined
        behaves exactly like all-vs-all."""
        gates, reason = compute_gates(
            membership_rows=[], agent_ids=["su", "wiseman", "cravatt"],
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=0,
        )
        assert reason is None
        assert all(g is None for g in gates.values())

    def test_open_policy_uncohorted_agent_is_unrestricted(self):
        """Under `open`, unrestricted has to mean both directions.

        This assertion originally read `gates["su"] == {"su", "wiseman"}`, which pinned
        a bug: it left the uncohorted agent reachable by nobody, so it could react and
        never be replied to. See test_open_policy_is_symmetric_with_uncohorted_agents.
        """
        c1 = uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "su"), (c1, "wiseman")],
            agent_ids=["su", "wiseman", "cravatt"],
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=1,
        )
        assert gates["su"] == {"su", "wiseman", "cravatt"}
        assert gates["wiseman"] == {"su", "wiseman", "cravatt"}
        assert gates["cravatt"] is None, "uncohorted agent must not be silenced"

    def test_open_policy_is_symmetric_with_uncohorted_agents(self):
        """Regression: under policy=open an uncohorted agent must be reachable, not
        merely able to reach.

        Its own gate is None so it may act on anyone; but a cohorted agent's gate is
        the union of its co-members, which would not contain it. The result was an
        agent that could react and never be replied to — it could not hold a
        conversation, which is the opposite of "unrestricted", and it contradicts the
        §5.1 row "`A` has no cohort memberships, policy = open -> Yes".

        Found by a real multi-turn run: the uncohorted agent opened two threads and no
        cohorted agent ever replied. Every gate-computation test passed, and the
        symmetry test skipped the case because it only compared pairs where both gates
        were sets.
        """
        c1 = uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "su"), (c1, "wiseman")],
            agent_ids=["su", "wiseman", "cravatt"],
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=1,
        )
        assert gates["cravatt"] is None, "the uncohorted agent stays unrestricted"
        assert "cravatt" in gates["su"], (
            "a cohorted agent must be able to act on an uncohorted one under "
            f"policy=open, else they can never converse. su gate={gates['su']}"
        )
        assert "cravatt" in gates["wiseman"]

    def test_isolated_policy_does_not_add_uncohorted_agents(self):
        """The fix must not leak into policy=isolated, where uncohorted means excluded."""
        c1 = uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "su"), (c1, "wiseman")],
            agent_ids=["su", "wiseman", "cravatt"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
        )
        assert gates["su"] == {"su", "wiseman"}
        assert "cravatt" not in gates["su"]
        assert gates["cravatt"] == set()

    def test_isolated_policy_uncohorted_agent_gets_empty_set(self):
        c1 = uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "su"), (c1, "wiseman")],
            agent_ids=["su", "wiseman", "cravatt"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
        )
        assert gates["cravatt"] == set()

    def test_multi_cohort_union(self):
        c1, c2 = uuid.uuid4(), uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "su"), (c1, "wiseman"), (c2, "su"), (c2, "cravatt")],
            agent_ids=["su", "wiseman", "cravatt"],
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=2,
        )
        assert gates["su"] == {"su", "wiseman", "cravatt"}
        assert gates["wiseman"] == {"su", "wiseman"}
        assert gates["cravatt"] == {"su", "cravatt"}

    def test_membership_for_offline_agent_is_inert(self):
        """A membership naming an agent the engine isn't running must not appear in
        anyone's mate set as a live sender, but must not break the computation."""
        c1 = uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "su"), (c1, "ghost")],
            agent_ids=["su"],
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=1,
        )
        assert "ghost" not in gates
        # 'ghost' is still a co-member of the cohort, so su may act on it if it
        # ever comes online — the roster, not the gate, decides who is running.
        assert gates["su"] == {"su", "ghost"}

    def test_relation_is_symmetric(self):
        c1 = uuid.uuid4()
        gates, _ = compute_gates(
            membership_rows=[(c1, "a"), (c1, "b")], agent_ids=["a", "b"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
        )
        assert ("b" in gates["a"]) and ("a" in gates["b"])

    def test_summarise_gates(self):
        s = summarise_gates({"a": None, "b": set(), "c": {"c", "d"}})
        assert s["total"] == 3
        assert s["gated"] == 2
        assert s["isolated"] == ["b"]
        assert s["unrestricted"] == ["a"]


# ---------------------------------------------------------------------------
# §5.3 — preflight: never silently silence a roster
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_isolated_policy_with_zero_cohorts_is_refused(self):
        reason = preflight_reason(
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=0, has_db=True
        )
        assert reason and "roster-wide silence" in reason

    def test_open_policy_with_zero_cohorts_is_fine(self):
        assert preflight_reason(
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=0, has_db=True
        ) is None

    def test_isolated_policy_with_a_cohort_is_fine(self):
        assert preflight_reason(
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
            has_db=True, live_members=2,
        ) is None

    def test_isolated_policy_with_an_empty_cohort_is_refused(self):
        """Regression: the check must count live members, not cohorts. Creating a
        cohort and never adding anyone to it silences the whole roster just as
        surely as defining no cohorts at all."""
        reason = preflight_reason(
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
            has_db=True, live_members=0,
        )
        assert reason and "no live agent is a member" in reason

    def test_compute_gates_refuses_an_empty_cohort_under_isolated_policy(self):
        gates, reason = compute_gates(
            membership_rows=[], agent_ids=["su", "wiseman"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
        )
        assert reason is not None
        assert all(g is None for g in gates.values())

    def test_compute_gates_refuses_when_only_offline_agents_are_members(self):
        """A cohort containing only agents the engine isn't running leaves every
        live agent uncohorted."""
        c1 = uuid.uuid4()
        gates, reason = compute_gates(
            membership_rows=[(c1, "ghost")], agent_ids=["su"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
        )
        assert reason is not None and gates["su"] is None

    def test_one_live_member_is_enough_to_proceed(self):
        c1 = uuid.uuid4()
        gates, reason = compute_gates(
            membership_rows=[(c1, "su")], agent_ids=["su", "wiseman"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=1,
        )
        assert reason is None
        assert gates["su"] == {"su"} and gates["wiseman"] == set()

    def test_no_database_is_refused(self):
        reason = preflight_reason(
            isolation_enabled=True, policy=POLICY_OPEN, cohort_count=3, has_db=False
        )
        assert reason and "silently do nothing" in reason

    def test_disabled_isolation_never_refuses(self):
        assert preflight_reason(
            isolation_enabled=False, policy=POLICY_ISOLATED, cohort_count=0, has_db=False
        ) is None

    def test_refusal_forces_every_gate_open(self):
        gates, reason = compute_gates(
            membership_rows=[], agent_ids=["su", "wiseman"],
            isolation_enabled=True, policy=POLICY_ISOLATED, cohort_count=0,
        )
        assert reason is not None
        assert all(g is None for g in gates.values()), "must fail OPEN, not closed"

    async def test_engine_logs_error_and_disables(self, monkeypatch, caplog):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        eng = _engine(["su", "wiseman"], membership_rows=[], cohort_count=0)
        with caplog.at_level("ERROR"):
            await eng._recompute_allowed_sender_ids()
        assert eng._cohort_preflight_error is not None
        assert all(a.allowed_sender_ids is None for a in eng.agents.values())
        assert any("forced OFF" in r.getMessage() for r in caplog.records)

    async def test_engine_without_session_factory_disables(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True)
        eng = _engine(["su", "wiseman"])  # membership_rows=None -> no factory
        await eng._recompute_allowed_sender_ids()
        assert eng._cohort_preflight_error is not None
        assert all(a.allowed_sender_ids is None for a in eng.agents.values())

    async def test_transient_db_error_leaves_gates_in_place(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman"], membership_rows=[(c1, "su"), (c1, "wiseman")])
        await eng._recompute_allowed_sender_ids()
        assert eng.agents["su"].allowed_sender_ids == {"su", "wiseman"}

        class _Boom:
            async def execute(self, _s):
                raise RuntimeError("connection reset")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

        eng.session_factory = lambda: _Boom()
        await eng._recompute_allowed_sender_ids()
        assert eng.agents["su"].allowed_sender_ids == {"su", "wiseman"}, (
            "a DB blip must not flap the gate open"
        )


# ---------------------------------------------------------------------------
# §6 — gated reads
# ---------------------------------------------------------------------------


class TestGatedReads:
    @pytest.fixture
    def log(self):
        ml = MessageLog()
        ml.set_bot_name_map({"subot": "su", "wisemanbot": "wiseman", "cravattbot": "cravatt"})
        return ml

    def test_top_level_posts_filtered(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hi"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "hi"))
        log.append(_post("3", "general", None, "Dr PI", "hi", is_bot=False))
        got = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids={"wiseman"},
        )
        assert {p.ts for p in got} == {"1", "3"}

    def test_top_level_posts_unfiltered_when_gate_off(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hi"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "hi"))
        got = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su", allowed_sender_ids=None
        )
        assert {p.ts for p in got} == {"1", "2"}

    def test_tags_filtered(self, log):
        log.append(_post("1", "general", "wiseman", "WisemanBot", "hey @SuBot"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "hey @SuBot"))
        log.append(_post("3", "general", None, "Dr PI", "hey @SuBot", is_bot=False))
        got = log.get_tags_for_agent("SuBot", since=0, allowed_sender_ids={"wiseman"})
        assert {t.ts for t in got} == {"1", "3"}

    def test_replies_filtered(self, log):
        log.append(_post("1", "general", "su", "SuBot", "root"))
        log.append(_post("2", "general", "wiseman", "WisemanBot", "r", thread_ts="1"))
        log.append(_post("3", "general", "cravatt", "CravattBot", "r", thread_ts="1"))
        got = log.get_replies_to_agent_posts("su", since=0, allowed_sender_ids={"wiseman"})
        assert {r.ts for r in got} == {"2"}

    def test_has_new_reply_from_other_is_gated(self, log):
        log.append(_post("1", "general", "su", "SuBot", "root"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "r", thread_ts="1"))
        assert log.has_new_reply_from_other("1", "su", 0.0) is True
        assert log.has_new_reply_from_other(
            "1", "su", 0.0, allowed_sender_ids={"wiseman"}
        ) is False

    def test_has_new_reply_ignores_own_messages(self, log):
        """Regression: the original returned True for the agent's own reply when the
        sender check was ordered after the early return."""
        log.append(_post("1", "general", "su", "SuBot", "root"))
        log.append(_post("2", "general", "su", "SuBot", "own follow-up", thread_ts="1"))
        assert log.has_new_reply_from_other("1", "su", 0.0) is False

    def test_ungated_methods_take_no_gate_parameter(self):
        """Asserted explicitly so widening one becomes a deliberate act."""
        for name in (
            "get_thread_history", "get_thread_message_count",
            "get_agent_top_level_posts", "get_last_bot_sender_in_channel",
            "get_thread_allowed_agents", "is_funding_thread", "get_entry",
        ):
            sig = inspect.signature(getattr(MessageLog, name))
            assert "allowed_sender_ids" not in sig.parameters, name

    def test_gated_methods_take_the_gate_parameter(self):
        for name in (
            "get_new_top_level_posts", "get_replies_to_agent_posts",
            "get_tags_for_agent", "has_new_reply_from_other",
        ):
            sig = inspect.signature(getattr(MessageLog, name))
            assert "allowed_sender_ids" in sig.parameters, name


class TestReadPathInventory:
    """Guard: a new public read method must declare its cohort classification.

    Without this the §6 inventory rots the first time someone adds a reader and
    forgets the gate — which is exactly how has_new_reply_from_other was missed.
    """

    def test_every_public_read_method_is_classified(self):
        unclassified = []
        for name, obj in vars(MessageLog).items():
            if name.startswith("_"):
                continue
            if not re.match(r"^(get|has|is)_", name) and name != "latest_timestamp":
                continue
            fn = obj.fget if isinstance(obj, property) else obj
            doc = inspect.getdoc(fn) or ""
            if "COHORT-GATE: GATED" not in doc and "COHORT-GATE: UNGATED" not in doc:
                unclassified.append(name)
        assert not unclassified, (
            "MessageLog read methods missing a 'COHORT-GATE: GATED|UNGATED' marker "
            f"in their docstring: {unclassified}. See .notes/cohort-system-v2.md §6."
        )

    def test_writes_are_not_gated(self):
        for name in ("append", "load_entry", "_record"):
            sig = inspect.signature(getattr(MessageLog, name))
            assert "allowed_sender_ids" not in sig.parameters, (
                f"{name} must never take a gate: the log is shared by every agent "
                "in the process, so filtering at write filters for all of them"
            )


# ---------------------------------------------------------------------------
# §6.1 — stale banked posts
# ---------------------------------------------------------------------------


class TestStatePruning:
    async def test_interesting_posts_pruned_on_resync(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman", "cravatt"],
                      membership_rows=[(c1, "su"), (c1, "wiseman")])
        su = eng.agents["su"]
        su.state.interesting_posts = [
            PostRef(post_id="1", channel="general", sender_agent_id="wiseman",
                    content_snippet="mate", posted_at=1.0),
            PostRef(post_id="2", channel="general", sender_agent_id="cravatt",
                    content_snippet="non-mate", posted_at=2.0),
        ]
        await eng._recompute_allowed_sender_ids()
        assert [p.post_id for p in su.state.interesting_posts] == ["1"]

    async def test_pruning_keeps_human_authored_posts(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman"], membership_rows=[(c1, "su"), (c1, "wiseman")])
        su = eng.agents["su"]
        su.state.interesting_posts = [
            PostRef(post_id="h", channel="general", sender_agent_id="",
                    content_snippet="from a PI", posted_at=1.0),
        ]
        await eng._recompute_allowed_sender_ids()
        assert [p.post_id for p in su.state.interesting_posts] == ["h"]

    async def test_no_pruning_when_gate_off(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=False)
        eng = _engine(["su"], membership_rows=[])
        eng.agents["su"].state.interesting_posts = [
            PostRef(post_id="1", channel="general", sender_agent_id="anyone",
                    content_snippet="x", posted_at=1.0),
        ]
        await eng._recompute_allowed_sender_ids()
        assert len(eng.agents["su"].state.interesting_posts) == 1


# ---------------------------------------------------------------------------
# §6.2 — DB-primary read paths
# ---------------------------------------------------------------------------


class TestDbPrimaryPaths:
    def test_ingestion_is_complete_while_reads_are_filtered(self):
        """_poll_inbound_from_db feeds a log shared by every agent. The shared log
        must stay complete; only the per-agent read is filtered."""
        log = MessageLog()
        log.append(_post("1", "general", "wiseman", "WisemanBot", "a"))
        log.append(_post("2", "general", "cravatt", "CravattBot", "b"))
        assert len(log) == 2, "ingestion must not drop anything"
        gated = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids={"wiseman"},
        )
        ungated = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids=None,
        )
        assert len(gated) == 1 and len(ungated) == 2

    def test_null_agent_id_bot_row_does_not_leak(self):
        """The shape _poll_inbound_from_db produces from a nullable agent_id."""
        log = MessageLog()
        log.append(_post("1", "general", None, "bot", "unattributable", is_bot=True))
        got = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids={"wiseman"},
        )
        assert got == []

    def test_agent_message_agent_id_is_nullable(self):
        """Pins the schema fact the is_bot keying exists for. If this ever becomes
        NOT NULL, the fail-closed branch is still correct but no longer load-bearing."""
        from src.models.agent_activity import AgentMessage
        assert AgentMessage.__table__.c.agent_id.nullable is True

    def test_log_entry_carries_persisted_visibility(self):
        """§7 reads LogEntry.visibility rather than the engine's in-memory channel
        map, so it must survive ingestion from another process."""
        from src.models.agent_activity import AgentMessage
        assert "visibility" in AgentMessage.__table__.c
        assert "visibility" in {f.name for f in __import__("dataclasses").fields(LogEntry)}


# ---------------------------------------------------------------------------
# §7 — PI-created private channels outrank the gate
# ---------------------------------------------------------------------------


class TestPrivateChannels:
    def test_partner_visible_in_pi_created_private_channel(self):
        log = MessageLog()
        log.append(_post("1", "collab-priv", None, "Dr PI", "work together",
                         is_bot=False, visibility=VISIBILITY_COLLAB_PRIVATE))
        log.append(_post("2", "collab-priv", "cravatt", "CravattBot", "my angle",
                         visibility=VISIBILITY_COLLAB_PRIVATE))
        got = log.get_new_top_level_posts(
            since=0, channels={"collab-priv"}, exclude_agent_id="su",
            allowed_sender_ids=set(),  # maximally isolated
        )
        assert {p.ts for p in got} == {"1", "2"}, (
            "an explicit PI pairing must not be vetoed by a cohort"
        )

    def test_public_channel_from_same_partner_is_still_filtered(self):
        log = MessageLog()
        log.append(_post("1", "general", "cravatt", "CravattBot", "public post"))
        got = log.get_new_top_level_posts(
            since=0, channels={"general"}, exclude_agent_id="su",
            allowed_sender_ids=set(),
        )
        assert got == []

    def test_private_channel_tags_pass(self):
        log = MessageLog()
        log.set_bot_name_map({"subot": "su"})
        log.append(_post("1", "collab-priv", "cravatt", "CravattBot", "hey @SuBot",
                         visibility=VISIBILITY_COLLAB_PRIVATE))
        got = log.get_tags_for_agent("SuBot", since=0, allowed_sender_ids=set())
        assert len(got) == 1

    async def test_private_channel_thread_is_never_grandfathered(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        eng._channel_visibility["collab-priv"] = VISIBILITY_COLLAB_PRIVATE
        t = _thread(eng.agents["su"], "1", "cravatt", channel="collab-priv")
        await eng._recompute_allowed_sender_ids()
        assert t.grandfathered is False


# ---------------------------------------------------------------------------
# §8 — grandfathering
# ---------------------------------------------------------------------------


class TestGrandfathering:
    async def test_membership_change_grandfathers_the_thread(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        t = _thread(eng.agents["su"], "1", "cravatt")
        await eng._recompute_allowed_sender_ids()
        assert t.grandfathered is True

    async def test_resumed_run_grandfathers_rebuilt_threads(self, monkeypatch):
        """The rebuild is gate-blind by construction, so the first recompute is
        where a restart's inherited partnerships get marked."""
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman", "cravatt"],
                      membership_rows=[(c1, "su"), (c1, "wiseman")])
        legal = _thread(eng.agents["su"], "1", "wiseman")
        inherited = _thread(eng.agents["su"], "2", "cravatt")
        assert eng.agents["su"].allowed_sender_ids is None  # pre-recompute: blind
        await eng._recompute_allowed_sender_ids()
        assert legal.grandfathered is False
        assert inherited.grandfathered is True

    async def test_re_permission_clears_the_flag(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        t = _thread(eng.agents["su"], "1", "cravatt")
        await eng._recompute_allowed_sender_ids()
        assert t.grandfathered is True
        eng._fake_db._rows = [(c1, "su"), (c1, "cravatt")]
        await eng._recompute_allowed_sender_ids()
        assert t.grandfathered is False

    async def test_disabling_isolation_clears_the_flag(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        t = _thread(eng.agents["su"], "1", "cravatt")
        await eng._recompute_allowed_sender_ids()
        assert t.grandfathered is True
        _patch(monkeypatch, cohort_isolation_enabled=False)
        await eng._recompute_allowed_sender_ids()
        assert t.grandfathered is False

    async def test_grandfathered_thread_loses_reactive_priority(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        _thread(eng.agents["su"], "1", "cravatt", pending=True)
        await eng._recompute_allowed_sender_ids()
        assert eng._owes_reply(eng.agents["su"]) is False

    async def test_permitted_thread_keeps_reactive_priority(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman"], membership_rows=[(c1, "su"), (c1, "wiseman")])
        _thread(eng.agents["su"], "1", "wiseman", pending=True)
        await eng._recompute_allowed_sender_ids()
        assert eng._owes_reply(eng.agents["su"]) is True

    async def test_non_cohort_third_party_cannot_manufacture_priority(self, monkeypatch):
        """A funding thread is open to all, so a non-cohort agent can post into an
        otherwise legal thread. That must not create reactive priority."""
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman", "cravatt"],
                      membership_rows=[(c1, "su"), (c1, "wiseman")])
        _thread(eng.agents["su"], "1", "wiseman")
        eng.message_log.append(_post("1", "general", "su", "SuBot", ":moneybag: FOA"))
        eng.message_log.append(
            _post("2", "general", "cravatt", "CravattBot", "me too", thread_ts="1")
        )
        eng.agents["su"].state.last_seen_cursor = 0.0
        await eng._recompute_allowed_sender_ids()
        assert eng._owes_reply(eng.agents["su"]) is False

    def test_phase4_reads_ungated_so_threads_can_conclude(self):
        """Phase 4 must see a grandfathered partner's reply — the thread is open and
        entitled to finish. Pinned on the call site, since the whole point of §8 is
        that Phase 4 and the scheduler deliberately differ."""
        src = inspect.getsource(SimulationEngine._phase4_reply_threads)
        assert "allowed_sender_ids=None" in src
        assert "entitled to conclude" in src

    async def test_closed_thread_is_not_grandfathered_or_owed(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        t = _thread(eng.agents["su"], "1", "cravatt", pending=True)
        t.status = "closed"
        await eng._recompute_allowed_sender_ids()
        assert eng._owes_reply(eng.agents["su"]) is False


# ---------------------------------------------------------------------------
# §9 — outbound mention hygiene
# ---------------------------------------------------------------------------


class TestTagHygiene:
    def _eng(self, monkeypatch, allowed):
        _patch(monkeypatch, cohort_isolation_enabled=True)
        eng = _engine(["su", "wiseman", "cravatt"])
        eng.agents["su"].allowed_sender_ids = allowed
        return eng

    def test_no_op_when_gate_off(self, monkeypatch):
        eng = self._eng(monkeypatch, None)
        text = "Hey @WisemanBot, thoughts?"
        assert eng._strip_disallowed_tags(text, eng.agents["su"]) == text

    def test_cohort_mate_mention_survives(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su", "wiseman"})
        text = "Hey @WisemanBot, thoughts?"
        assert eng._strip_disallowed_tags(text, eng.agents["su"]) == text

    def test_non_mate_mention_is_removed_not_de_atted(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su", "wiseman"})
        out = eng._strip_disallowed_tags("Great point @CravattBot, shall we?",
                                         eng.agents["su"])
        assert "CravattBot" not in out
        assert "@" not in out
        assert out == "Great point, shall we?", out

    def test_self_mention_survives(self, monkeypatch):
        eng = self._eng(monkeypatch, set())
        assert eng._strip_disallowed_tags("as @SuBot said", eng.agents["su"]) == (
            "as @SuBot said"
        )

    def test_unknown_bot_name_is_left_alone_and_warned(self, monkeypatch, caplog):
        eng = self._eng(monkeypatch, set())
        with caplog.at_level("WARNING"):
            out = eng._strip_disallowed_tags("ping @GhostBot", eng.agents["su"])
        assert out == "ping @GhostBot"
        assert any("unknown bot name" in r.getMessage().lower() for r in caplog.records)

    def test_strips_are_counted_per_agent(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su"})
        eng._strip_disallowed_tags("@WisemanBot @CravattBot hi", eng.agents["su"])
        assert eng._cohort_tags_stripped["su"] == 2

    def test_no_count_when_nothing_stripped(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su", "wiseman"})
        eng._strip_disallowed_tags("@WisemanBot hi", eng.agents["su"])
        assert "su" not in eng._cohort_tags_stripped

    def test_empty_and_none_text(self, monkeypatch):
        eng = self._eng(monkeypatch, set())
        assert eng._strip_disallowed_tags(None, eng.agents["su"]) is None
        assert eng._strip_disallowed_tags("", eng.agents["su"]) == ""

    def test_all_outbound_paths_are_covered(self):
        """The strip lives in _post_message, so every caller inherits it — Phase 4
        replies included, which the original Phase-5-only placement missed."""
        assert "_strip_disallowed_tags" in inspect.getsource(
            SimulationEngine._post_message
        )

    def test_indentation_and_code_blocks_survive(self, monkeypatch):
        """Regression: an earlier global whitespace normalisation stripped leading
        indentation on every line, mangling code blocks and nested bullet lists."""
        eng = self._eng(monkeypatch, {"su", "wiseman"})
        text = (
            "Proposal:\n\n```python\n    def f():\n        return 1\n```\n\n"
            "- point one\n  - nested\ncc @CravattBot"
        )
        out = eng._strip_disallowed_tags(text, eng.agents["su"])
        assert "    def f():" in out
        assert "        return 1" in out
        assert "  - nested" in out
        assert "CravattBot" not in out

    def test_mention_at_line_start_leaves_no_leading_space(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su"})
        assert eng._strip_disallowed_tags("@CravattBot hi", eng.agents["su"]) == "hi"

    def test_trailing_mention_leaves_no_trailing_space(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su"})
        assert eng._strip_disallowed_tags("cc @CravattBot", eng.agents["su"]) == "cc"

    def test_email_and_url_are_not_mangled(self, monkeypatch):
        """The strip now runs on every outbound message, so a bare '@' inside an
        address or URL path must not be read as a mention."""
        eng = self._eng(monkeypatch, {"su"})
        for text in ("mail a@cravattbot.example", "see http://x/@cravattbot"):
            assert eng._strip_disallowed_tags(text, eng.agents["su"]) == text

    def test_mention_needs_a_word_boundary(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su"})
        assert eng._strip_disallowed_tags("@CravattBotly", eng.agents["su"]) == (
            "@CravattBotly"
        )

    def test_idempotent(self, monkeypatch):
        eng = self._eng(monkeypatch, {"su"})
        once = eng._strip_disallowed_tags("hi @CravattBot there", eng.agents["su"])
        twice = eng._strip_disallowed_tags(once, eng.agents["su"])
        assert once == twice


# ---------------------------------------------------------------------------
# §10 — scheduler
# ---------------------------------------------------------------------------


class TestScheduler:
    def test_owed_agent_selected_first(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["su", "wiseman", "cravatt"])
        _thread(eng.agents["wiseman"], "t1", "su", pending=True)
        assert eng._select_agent().agent_id == "wiseman"
        assert eng._reactive_streak == 1

    def test_oldest_waiting_owed_agent_wins(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["su", "wiseman"])
        _thread(eng.agents["su"], "t1", "wiseman", pending=True)
        _thread(eng.agents["wiseman"], "t2", "su", pending=True)
        eng.agents["su"].state.last_selected = 100.0
        eng.agents["wiseman"].state.last_selected = 5.0
        assert eng._select_agent().agent_id == "wiseman"

    def test_excludes_last_llm_caller(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["su", "wiseman"])
        _thread(eng.agents["su"], "t1", "wiseman", pending=True)
        _thread(eng.agents["wiseman"], "t2", "su", pending=True)
        eng.agents["su"].state.last_selected = 1.0
        eng.agents["wiseman"].state.last_selected = 50.0
        eng._last_llm_caller = "su"
        assert eng._select_agent().agent_id == "wiseman"

    def test_no_candidates_returns_none(self, monkeypatch):
        _patch(monkeypatch)
        assert _engine([])._select_agent() is None

    def test_per_agent_cooldown_is_enforced(self, monkeypatch):
        """turn_delay_seconds is a per-agent cooldown at selection time, not a
        global sleep."""
        import time
        _patch(monkeypatch, turn_delay_seconds=10_000.0)
        eng = _engine(["su"])
        eng.agents["su"].state.last_selected = time.time()
        assert eng._select_agent() is None

    def test_cooldown_only_sidelines_the_agent_that_just_ran(self, monkeypatch):
        import time
        _patch(monkeypatch, turn_delay_seconds=10_000.0)
        eng = _engine(["su", "wiseman"])
        eng.agents["su"].state.last_selected = time.time()
        eng.agents["wiseman"].state.last_selected = 0.0
        picked = eng._select_agent()
        assert picked is not None and picked.agent_id == "wiseman"

    def test_cooldown_applies_to_the_reactive_tier_too(self, monkeypatch):
        import time
        _patch(monkeypatch, turn_delay_seconds=10_000.0)
        eng = _engine(["su", "wiseman"])
        _thread(eng.agents["su"], "t1", "wiseman", pending=True)
        eng.agents["su"].state.last_selected = time.time()
        eng.agents["wiseman"].state.last_selected = 0.0
        assert eng._select_agent().agent_id == "wiseman"

    def test_global_sleep_removed_from_main_loop(self):
        # The main loop lives in start().
        src = inspect.getsource(SimulationEngine.start)
        assert "_sleep(settings.turn_delay_seconds)" not in src
        assert "enforced at selection time in _turn_eligible" in src

    def test_valve_forces_proactive_at_cap(self, monkeypatch):
        _patch(monkeypatch, max_consecutive_reactive_turns=3)
        eng = _engine(["su", "wiseman"])
        _thread(eng.agents["wiseman"], "t1", "su", pending=True)
        eng._reactive_streak = 3
        assert eng._select_agent() is not None
        assert eng._reactive_streak == 0

    def test_default_valve_is_three(self):
        from src.config import Settings
        assert Settings.model_fields["max_consecutive_reactive_turns"].default == 3

    def test_valve_caps_starvation_at_three_to_one(self, monkeypatch):
        """At the original default of 8 a live pair took 24 of 27 turns.

        Models the real loop: `start()` advances `last_selected` after every turn,
        which is what lets the staleness-weighted proactive tier favour the agents
        the reactive pair has been starving. A fake clock is required — with wall
        time every delta is sub-second and `max(now - last_selected, 1.0)` clamps
        every weight to 1.0, making the proactive tier uniform and the assertion
        meaningless.
        """
        import random

        import src.agent.simulation as sim

        # The proactive tier is random.choices. Seed it so this bound is a fact
        # about the scheduler rather than about today's RNG state.
        random.seed(20260730)
        _patch(monkeypatch, max_consecutive_reactive_turns=3)
        clock = [1000.0]
        monkeypatch.setattr(sim.time, "time", lambda: clock[0])

        eng = _engine(["su", "wiseman", "a", "b", "c"])
        _thread(eng.agents["su"], "t1", "wiseman", pending=True)
        _thread(eng.agents["wiseman"], "t2", "su", pending=True)
        picks = []
        for _ in range(40):
            got = eng._select_agent()
            picks.append(got.agent_id)
            eng._last_llm_caller = got.agent_id
            got.state.last_selected = clock[0]   # as start() does
            clock[0] += 10.0
        pair = sum(1 for p in picks if p in {"su", "wiseman"})
        assert pair <= 32, f"{pair}/40 went to the live pair: {picks}"
        assert pair >= 24, "the reactive tier should still dominate"
        starved = {a for a in ("a", "b", "c") if a in picks}
        assert starved == {"a", "b", "c"}, (
            f"every idle agent must get a turn within 40 selections, got {starved}"
        )

    def test_selection_counters_advance(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["su", "wiseman"])
        _thread(eng.agents["wiseman"], "t1", "su", pending=True)
        eng._select_agent()
        assert eng._reactive_selections == 1 and eng._proactive_selections == 0
        eng.agents["wiseman"].state.active_threads.clear()
        eng._select_agent()
        assert eng._proactive_selections == 1

    def test_budget_still_filters(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["su"], budget_cap=1)
        eng.agents["su"].api_call_count = 5
        assert eng._select_agent() is None


# ---------------------------------------------------------------------------
# §13.1 — provenance
# ---------------------------------------------------------------------------


class TestTopologySnapshot:
    async def test_snapshot_records_the_applied_gate(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        c1 = uuid.uuid4()
        eng = _engine(["su", "wiseman", "cravatt"],
                      membership_rows=[(c1, "su"), (c1, "wiseman")])
        await eng._recompute_allowed_sender_ids()
        snap = eng.cohort_topology_snapshot()
        assert snap["cohort_isolation_enabled"] is True
        assert snap["cohort_default_policy"] == POLICY_ISOLATED
        assert snap["agents"]["su"] == ["su", "wiseman"]
        assert snap["agents"]["cravatt"] == []
        assert snap["preflight_error"] is None

    async def test_snapshot_records_a_preflight_override(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True,
               cohort_default_policy=POLICY_ISOLATED)
        eng = _engine(["su"], membership_rows=[], cohort_count=0)
        await eng._recompute_allowed_sender_ids()
        snap = eng.cohort_topology_snapshot()
        assert snap["preflight_error"] is not None
        assert snap["agents"]["su"] is None

    async def test_snapshot_carries_counters(self, monkeypatch):
        _patch(monkeypatch, cohort_isolation_enabled=True)
        c1 = uuid.uuid4()
        eng = _engine(["su", "cravatt"], membership_rows=[(c1, "su")])
        await eng._recompute_allowed_sender_ids()
        eng._cohort_tags_stripped["su"] = 4
        _thread(eng.agents["su"], "1", "cravatt", grandfathered=True)
        c = eng.cohort_topology_snapshot()["counters"]
        assert c["tags_stripped"] == {"su": 4}
        assert c["grandfathered_threads"] == ["su:1"]

    async def test_snapshot_is_json_serialisable(self, monkeypatch):
        import json
        _patch(monkeypatch, cohort_isolation_enabled=True)
        c1 = uuid.uuid4()
        eng = _engine(["su"], membership_rows=[(c1, "su")])
        await eng._recompute_allowed_sender_ids()
        json.dumps(eng.cohort_topology_snapshot())


# ---------------------------------------------------------------------------
# §14 — migration hygiene
# ---------------------------------------------------------------------------


class TestMigrationHygiene:
    VERSIONS = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions"

    def _revisions(self):
        out = {}
        for f in sorted(self.VERSIONS.glob("*.py")):
            m = re.search(r'^revision:?\s*(?::\s*str\s*)?=\s*["\'](.+?)["\']',
                          f.read_text(), re.M)
            if m:
                out.setdefault(m.group(1), []).append(f.name)
        return out

    def test_no_duplicate_revision_ids(self):
        dupes = {r: fs for r, fs in self._revisions().items() if len(fs) > 1}
        assert not dupes, (
            f"duplicate alembic revision ids {dupes} — Alembic keeps only the "
            "last-sorted file, silently skipping the other while stamping the DB "
            "as fully migrated. See .notes/cohort-system-v2.md §14."
        )

    def test_exactly_one_head(self):
        revs, downs = {}, set()
        for f in sorted(self.VERSIONS.glob("*.py")):
            src = f.read_text()
            r = re.search(r'^revision:?\s*(?::\s*str\s*)?=\s*["\'](.+?)["\']', src, re.M)
            d = re.search(r'^down_revision[^=]*=\s*["\'](.+?)["\']', src, re.M)
            if r:
                revs[r.group(1)] = f.name
            if d:
                downs.add(d.group(1))
        heads = sorted(set(revs) - downs)
        assert len(heads) == 1, f"expected 1 alembic head, found {heads}"

    def test_cohort_migration_is_on_the_current_head(self):
        f = self.VERSIONS / "0022_add_cohorts.py"
        assert f.exists(), "cohort migration must be renumbered to 0022"
        src = f.read_text()
        assert 'revision: str = "0022"' in src
        assert 'down_revision: Union[str, None] = "0021"' in src

    def test_cohort_downgrade_is_idempotent(self):
        """A rollback must not wedge on an object a partial upgrade never created."""
        src = (self.VERSIONS / "0022_add_cohorts.py").read_text()
        downgrade = src[src.index("def downgrade"):]
        drops = re.findall(r"op\.drop_(?:table|index)\(", downgrade)
        assert downgrade.count("if_exists=True") == len(drops), (
            "every drop in the cohort downgrade needs if_exists=True"
        )
