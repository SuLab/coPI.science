"""Tests for the DB-backed agent roster: token resolution + live roster sync."""

import types
from unittest.mock import AsyncMock

import pytest

from src.agent.authorship_rules import LabPublicationRecord
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


class _FakeDBGrantbotTokenRaises:
    """Serves the roster query and the publication join normally, but the
    grantbot uid-reprobe's single-column token query (get_agent_bot_token)
    raises — used to prove that failure is isolated to its own try/except
    and cannot abort the add/remove diff sharing the same DB session.

    Discriminated by column count (real SQLAlchemy Select objects), not call
    order, since production issues all three queries on one open session:
    the 5-column roster select, the 2-column publication join, and the
    1-column grantbot token select.
    """
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        n = len(stmt.selected_columns)
        if n == 1:
            raise RuntimeError("grantbot token query failed")
        return _FakeResult(self._rows if n > 2 else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSlackClient:
    """Stand-in for AgentSlackClient.

    bot_user_id is derived from the token so a rotation (a new token) yields a
    distinct uid, the way a re-provisioned Slack app would — needed to tell
    apart the uid map before/after a rebuild.
    """
    def __init__(self, agent_id, bot_token, connect_result=True):
        self.agent_id = agent_id
        self.bot_token = bot_token
        self.bot_user_id = f"U_{bot_token}"
        self._connect_result = connect_result

    def connect(self):
        return self._connect_result


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

    async def test_a_newly_added_agent_gets_its_state_rebuilt(self, monkeypatch):
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su"), _row("wiseman")], existing_agents=["su"])
        engine._rebuild_one_agent_state = AsyncMock()

        await engine._sync_roster_from_db()

        engine._rebuild_one_agent_state.assert_awaited_once_with("wiseman")

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

    async def test_surviving_agent_bot_name_and_pi_name_edits_go_live(self, monkeypatch):
        """DOC-B residual: renaming a live agent's bot_name/pi_name in AgentRegistry
        must be picked up without a restart. Before the fix, the surviving-agent
        loop diffed `role` only; bot_name/pi_name were read only in the `to_add`
        branch, so a DB rename was invisible until the process restarted.
        """
        _patch_client(monkeypatch)
        renamed = _row("su")
        renamed.bot_name = "NewSuBot"
        renamed.pi_name = "New Name"
        engine = _make_engine([renamed], existing_agents=["su"])

        await engine._sync_roster_from_db()

        agent = engine.agents["su"]
        assert agent.bot_name == "NewSuBot"
        assert agent.pi_name == "New Name"
        assert engine._bot_name_to_id.get("newsubot") == "su"
        assert "subot" not in engine._bot_name_to_id

    async def test_rename_away_from_service_bot_name_reseeds_the_seed(self, monkeypatch):
        """#26 DOC-B follow-up: a roster PI's bot_name legitimately overrides the
        SERVICE_AGENT_IDS seed for "grantbot" while it owns that name (see
        __init__'s comment — the roster answer must win). But the old
        incremental rename logic just popped the owned key on a rename away
        from it, permanently deleting the shared seed entry instead of
        restoring it — leaving GrantBot's own posts unattributable
        thereafter. A full _rebuild_bot_name_map() re-applies the
        SERVICE_AGENT_IDS setdefault every time, so the seed survives.
        """
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su")], existing_agents=["su"])
        # Simulate a prior tick where this roster agent already claimed the
        # "grantbot" name (no seed entry survives that claim — see __init__).
        engine.agents["su"].bot_name = "GrantBot"
        engine._bot_name_to_id = {"grantbot": "su"}

        await engine._sync_roster_from_db()

        assert engine.agents["su"].bot_name == "SuBot"
        assert engine._bot_name_to_id.get("grantbot") == "grantbot", (
            "renaming a roster agent AWAY from a service-bot name must "
            "reseed the SERVICE_AGENT_IDS entry, not leave it missing"
        )
        # A full rebuild must not lose the renamed agent's OWN new mapping...
        assert engine._bot_name_to_id.get("subot") == "su"
        # ...and the flush (bot_name_changed -> set_bot_name_map) must carry
        # the reseeded "grantbot" entry into message_log's copy too, not just
        # the engine's own _bot_name_to_id.
        assert engine.message_log._bot_name_to_id.get("grantbot") == "grantbot"

    async def test_removal_of_agent_holding_service_bot_name_reseeds_the_seed(self, monkeypatch):
        """Mirrors test_rename_away_from_service_bot_name_reseeds_the_seed
        above, for the REMOVAL path: afba7e8's removal loop searched-and-
        popped the removed agent's OWN _bot_name_to_id key (`next(n for n, a
        in ... if a == aid)`), which permanently deleted the SERVICE_AGENT_IDS
        "grantbot" seed whenever the removed agent happened to be the one
        holding that name — GrantBot's own :moneybag: posts become
        unattributable for the rest of the run after that. ca72c8f's
        _rebuild_bot_name_map() (now also used by the removal branch) reseeds
        it instead, and both the engine's own map and message_log's flushed
        copy must reflect it.
        """
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su")], existing_agents=["su", "grant"])
        engine.agents["grant"].bot_name = "GrantBot"
        engine._rebuild_bot_name_map()

        await engine._sync_roster_from_db()

        assert "grant" not in engine.agents  # removed (not in the DB rows)
        assert engine._bot_name_to_id.get("grantbot") == "grantbot", (
            "removing a roster agent that held a service-bot name must "
            "reseed the SERVICE_AGENT_IDS entry, not leave it missing"
        )
        assert engine.message_log._bot_name_to_id.get("grantbot") == "grantbot"

    async def test_existing_client_is_rebuilt_when_its_token_rotates(self, monkeypatch):
        """Red-team residual: the old `continue` on 'aid in self.slack_clients'
        skipped a TOKEN ROTATION on an already-connected agent entirely — the
        agent kept posting with the stale (about-to-be-revoked) token until a
        restart. Rebuild the client when the desired token no longer matches
        the connected client's token.
        """
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su", token="xoxb-rotated")], existing_agents=["su"])
        before = engine.slack_clients["su"]

        await engine._sync_roster_from_db()

        after = engine.slack_clients["su"]
        assert after is not before
        assert after.bot_token == "xoxb-rotated"

    async def test_message_log_name_map_flushed_after_rename(self, monkeypatch):
        """#26 DOC-B fix round 1: the engine's own _bot_name_to_id already
        picked up a rename (test_surviving_agent_bot_name_and_pi_name_edits_go_live
        above), but message_log holds a COPY taken by set_bot_name_map — the
        early-return path must flush that copy too, or the poller (which reads
        message_log's map) keeps resolving the OLD name.
        """
        _patch_client(monkeypatch)
        renamed = _row("su")
        renamed.bot_name = "NewSuBot"
        engine = _make_engine([renamed], existing_agents=["su"])

        await engine._sync_roster_from_db()

        assert engine.message_log._bot_name_to_id.get("newsubot") == "su"
        assert "subot" not in engine.message_log._bot_name_to_id

    async def test_message_log_uid_map_flushed_after_token_rotation(self, monkeypatch):
        """A rotated client gets a NEW bot_user_id (re-provisioned Slack app).
        message_log._bot_uid_to_agent is a copy taken by set_bot_uid_map — the
        early-return path (no add/remove) must flush it too, or <@Unew>
        mentions of the rotated bot never resolve until a restart.
        """
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su", token="xoxb-rotated")], existing_agents=["su"])

        await engine._sync_roster_from_db()

        after = engine.slack_clients["su"]
        assert engine.message_log._bot_uid_to_agent.get(after.bot_user_id) == "su"

    async def test_rotation_with_failed_connect_keeps_old_client(self, monkeypatch):
        """connect() failing on the rebuild attempt must not discard the still-
        working client — the engine keeps posting on the old (soon-to-expire)
        token and retries the rebuild on a later tick."""
        def _factory(agent_id, bot_token):
            return _FakeSlackClient(agent_id, bot_token, connect_result=False)
        monkeypatch.setattr("src.agent.slack_client.AgentSlackClient", _factory)
        engine = _make_engine([_row("su", token="xoxb-rotated")], existing_agents=["su"])
        before = engine.slack_clients["su"]

        await engine._sync_roster_from_db()

        assert engine.slack_clients["su"] is before

    async def test_rotation_does_not_touch_other_agents_client(self, monkeypatch):
        _patch_client(monkeypatch)
        engine = _make_engine(
            [_row("su", token="xoxb-rotated"), _row("wiseman")],
            existing_agents=["su", "wiseman"],
        )
        before = engine.slack_clients["wiseman"]

        await engine._sync_roster_from_db()

        assert engine.slack_clients["wiseman"] is before

    async def test_db_token_cleared_does_not_fall_back_to_env(self, monkeypatch):
        """DB is authoritative: clearing an agent's DB token must not downgrade
        an already-connected agent to a (possibly stale) .env token, and must
        not retry a reconnect every tick against a dead env token."""
        _patch_client(monkeypatch)
        monkeypatch.setattr(
            slack_tokens, "get_settings",
            lambda: types.SimpleNamespace(get_slack_tokens=lambda: {"su": "xoxb-env-old"}),
        )
        engine = _make_engine([_row("su", token=None)], existing_agents=["su"])
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

    async def test_publication_load_failure_does_not_abort_roster_sync(self, monkeypatch):
        """A publications-join failure must be isolated to its own try/except.

        Before the fix, _load_publication_records shared the roster query's
        try/except, so any exception from it (AgentRegistry/Publication join
        failing) was caught by the OUTER handler and silently no-op'd the whole
        tick — new agents never got added, removals never propagated. See
        issue #29 review.
        """
        _patch_client(monkeypatch)
        engine = _make_engine([_row("su"), _row("wiseman")], existing_agents=["su"])
        stale = {"su": LabPublicationRecord(dois={"10.1/stale"}, has_records=True)}
        engine._agent_publications = stale
        engine._load_publication_records = AsyncMock(side_effect=RuntimeError("join failed"))

        await engine._sync_roster_from_db()

        # Roster sync still completed its add/remove work despite the failure.
        assert set(engine.agents) == {"su", "wiseman"}
        assert "wiseman" in engine.slack_clients
        # Stale grounding data is preserved rather than cleared or replaced.
        assert engine._agent_publications == stale

    async def test_grantbot_token_lookup_failure_does_not_abort_roster_sync(self, monkeypatch):
        """The grantbot uid-reprobe token read shares the roster query's DB
        session but must have its own try/except, same rationale as the
        publication-record load just above it: before the fix it shared the
        outer try/except, so a failure there silently no-op'd the whole
        tick — a newly active agent was never added. See #29 review.
        """
        _patch_client(monkeypatch)
        rows = [_row("su"), _row("wiseman")]
        engine = _make_engine(rows, existing_agents=["su"])
        engine.session_factory = lambda: _FakeDBGrantbotTokenRaises(rows)

        await engine._sync_roster_from_db()

        # Roster sync still completed its add/remove work despite the failure.
        assert set(engine.agents) == {"su", "wiseman"}
        assert "wiseman" in engine.slack_clients


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
