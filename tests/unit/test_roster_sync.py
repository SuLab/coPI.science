"""Tests for the DB-backed agent roster: token resolution + live roster sync."""

import types
from unittest.mock import AsyncMock

import pytest

from src.agent.simulation import SimulationEngine
from src.services import slack_tokens

# ---------------------------------------------------------------
# Token resolution helpers (src.services.slack_tokens)
# ---------------------------------------------------------------

class TestTokenHelpers:
    def test_is_valid_token(self):
        assert slack_tokens.is_valid_token("xoxb-real-token") is True
        assert slack_tokens.is_valid_token(None) is False
        assert slack_tokens.is_valid_token("") is False
        assert slack_tokens.is_valid_token("xoxb-placeholder-su") is False

    def _patch_env(self, monkeypatch, mapping):
        fake_settings = types.SimpleNamespace(get_slack_tokens=lambda: mapping)
        monkeypatch.setattr(slack_tokens, "get_settings", lambda: fake_settings)

    def test_token_for_agent_row_prefers_db(self, monkeypatch):
        self._patch_env(monkeypatch, {"su": "xoxb-env-su"})
        agent = types.SimpleNamespace(agent_id="su", slack_bot_token="xoxb-db-su")
        assert slack_tokens.token_for_agent_row(agent) == "xoxb-db-su"

    def test_token_for_agent_row_falls_back_to_env(self, monkeypatch):
        self._patch_env(monkeypatch, {"su": "xoxb-env-su"})
        agent = types.SimpleNamespace(agent_id="su", slack_bot_token=None)
        assert slack_tokens.token_for_agent_row(agent) == "xoxb-env-su"

    def test_token_for_agent_row_placeholder_db_falls_back(self, monkeypatch):
        self._patch_env(monkeypatch, {"su": "xoxb-env-su"})
        agent = types.SimpleNamespace(agent_id="su", slack_bot_token="xoxb-placeholder-su")
        assert slack_tokens.token_for_agent_row(agent) == "xoxb-env-su"

    def test_token_for_agent_row_none_when_no_source(self, monkeypatch):
        self._patch_env(monkeypatch, {})
        agent = types.SimpleNamespace(agent_id="su", slack_bot_token=None)
        assert slack_tokens.token_for_agent_row(agent) is None


# ---------------------------------------------------------------
# Live roster sync (_sync_roster_from_db)
# ---------------------------------------------------------------

def _row(agent_id, token="xoxb-real", role="pi_lab"):
    return types.SimpleNamespace(
        agent_id=agent_id, bot_name=f"{agent_id.capitalize()}Bot",
        pi_name=f"PI {agent_id}", slack_bot_token=token, role=role,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self._call_count = 0

    async def execute(self, _stmt):
        # _sync_roster_from_db now issues a second query inside the same
        # session block (_load_publication_records' AgentRegistry/Publication
        # join). Serve the roster rows on the first call and treat every
        # later call as "no publication rows" — these tests aren't seeding
        # any, and _agent_publications isn't part of what they assert.
        self._call_count += 1
        if self._call_count == 1:
            return _FakeResult(self._rows)
        return _FakeResult([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory_for(rows):
    """Return a session_factory callable yielding a fake DB serving `rows`."""
    return lambda: _FakeDB(rows)


class _FakeSlackClient:
    """Stand-in for AgentSlackClient — connect() always succeeds."""
    def __init__(self, agent_id, bot_token):
        self.agent_id = agent_id
        self.bot_token = bot_token

    def connect(self):
        return True


def _make_engine(active_rows, existing_agents=()):
    from src.agent.agent import Agent
    engine = SimulationEngine(
        agents=[Agent(agent_id=a, bot_name=f"{a.capitalize()}Bot", pi_name=f"PI {a}")
                for a in existing_agents],
        slack_clients={a: _FakeSlackClient(a, "xoxb-real") for a in existing_agents},
        session_factory=_factory_for(active_rows),
    )
    # Isolate the unit under test from cross-agent rebuild side effects.
    engine._load_pi_mappings = AsyncMock()
    engine._build_lab_directories = lambda: None
    return engine


def _patch_client(monkeypatch):
    monkeypatch.setattr("src.agent.slack_client.AgentSlackClient", _FakeSlackClient)


class TestSyncRosterFromDb:
    async def test_adds_newly_active_agent(self, monkeypatch):
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su"), _row("wiseman")], existing_agents=["su"])
        await engine._sync_roster_from_db()
        assert set(engine.agents) == {"su", "wiseman"}
        assert "wiseman" in engine.slack_clients
        assert engine._bot_name_to_id["wisemanbot"] == "wiseman"

    async def test_removes_inactivated_agent(self, monkeypatch):
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su")], existing_agents=["su", "wiseman"])
        await engine._sync_roster_from_db()
        assert set(engine.agents) == {"su"}
        assert "wiseman" not in engine.slack_clients
        assert "wisemanbot" not in engine._bot_name_to_id

    async def test_skips_active_agent_without_token(self, monkeypatch):
        _patch_client(monkeypatch)
        # newly-active but tokenless (DB null) and no env token configured
        monkeypatch.setattr(slack_tokens, "get_settings",
                            lambda: types.SimpleNamespace(get_slack_tokens=lambda: {}))
        engine = _make_engine([_row("su"), _row("newbie", token=None)], existing_agents=["su"])
        await engine._sync_roster_from_db()
        assert "newbie" not in engine.agents
        assert set(engine.agents) == {"su"}

    async def test_surviving_agent_that_gains_a_token_gets_a_client(self, monkeypatch):
        """Regression: a roster agent provisioned AFTER startup stayed Slack-less.

        Measured 2026-08-06 on the blackbird deployment: 48 bots were installed
        while the engine ran, their tokens landed in AgentRegistry, and not one
        of them ever connected — ``Connected as`` stayed at the 7 that had tokens
        at process start. Cause: ``main.py`` puts EVERY active agent into
        ``self.agents`` regardless of token, so a later-provisioned agent is in
        neither ``to_add`` nor ``to_remove``, the sync early-returns, and clients
        are only ever built in the ``to_add`` loop. The docstring's promise that
        "a freshly provisioned token is picked up on the next tick" held only for
        an agent *entering* the roster.
        """
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su"), _row("late")], existing_agents=["su", "late"])
        # Reproduce the startup state: on the roster, but tokenless then, so
        # main.py never built it a client.
        del engine.slack_clients["late"]

        await engine._sync_roster_from_db()

        assert "late" in engine.slack_clients, (
            "an agent already on the roster that later gains a token must be "
            "given a Slack client without a process restart"
        )
        assert engine.slack_clients["late"].bot_token == "xoxb-real"

    async def test_surviving_agent_without_a_token_gets_no_client(self, monkeypatch):
        """The adopt path must not invent a client for a still-tokenless agent."""
        _patch_client(monkeypatch)
        monkeypatch.setattr(slack_tokens, "get_settings",
                            lambda: types.SimpleNamespace(get_slack_tokens=lambda: {}))
        engine = _make_engine([_row("su"), _row("late", token=None)],
                              existing_agents=["su", "late"])
        del engine.slack_clients["late"]

        await engine._sync_roster_from_db()

        assert "late" not in engine.slack_clients

    async def test_existing_client_is_not_rebuilt(self, monkeypatch):
        """Adoption must be idempotent — no reconnect churn every 30s."""
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su")], existing_agents=["su"])
        before = engine.slack_clients["su"]

        await engine._sync_roster_from_db()

        assert engine.slack_clients["su"] is before

    async def test_throttle_skips_within_interval(self, monkeypatch):
        _patch_client(monkeypatch)
        import time
        engine = _make_engine([_row("su"), _row("wiseman")], existing_agents=["su"])
        engine._last_roster_poll = time.time()  # just polled — should early-return
        await engine._sync_roster_from_db()
        assert set(engine.agents) == {"su"}  # no change

    async def test_no_session_factory_is_noop(self):
        from src.agent.agent import Agent
        engine = SimulationEngine(
            agents=[Agent(agent_id="su", bot_name="SuBot", pi_name="PI su")],
            slack_clients={},
            session_factory=None,
        )
        await engine._sync_roster_from_db()  # must not raise
        assert set(engine.agents) == {"su"}


# ---------------------------------------------------------------
# Admin self-service provisioning callback (src.services.admin_provisioning)
# ---------------------------------------------------------------

class TestAdminProvisioning:
    async def test_complete_provisioning_rejects_unknown_state(self):
        from src.services.admin_provisioning import (
            ProvisioningError,
            complete_provisioning,
        )

        db = _FakeDB([])  # no SlackAppProvision row matches the state
        with pytest.raises(ProvisioningError):
            await complete_provisioning(db, state="bogus", code="abc")
