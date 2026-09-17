"""GrantBot uid-probe token resolution.

``_resolve_service_bot_uids`` must resolve grantbot's Slack token DB-first
(``get_agent_bot_token(db, "grantbot")``, the same precedence grantbot.py
itself uses), falling back to the dedicated ``slack_bot_token_grantbot``
settings field ONLY — never to ``get_agent_bot_token``'s own internal
``.env`` fallback (``Settings.get_slack_tokens()``), which has no "grantbot"
key and would silently regress .env-only deployments.

``_sync_roster_from_db`` must also notice a DB-side rotation of grantbot's
token (grantbot never carries a status=='active' AgentRegistry row, so it
never appears in the roster diff) and re-run the probe.
"""

import types
from unittest.mock import AsyncMock

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScalarResult:
    """Stand-in for the SQLAlchemy Result of a single-column scalar select."""
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScalarDB:
    """Serves exactly one query: get_agent_bot_token's scalar select."""
    def __init__(self, token):
        self._token = token

    async def execute(self, _stmt):
        return _ScalarResult(self._token)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RosterAndGrantbotDB:
    """Serves both queries _sync_roster_from_db issues on the SAME session:
    the multi-column roster select and get_agent_bot_token's single-column
    scalar select. Discriminates by column count, not call order — a fresh
    instance is created per ``session_factory()`` call (once for the outer
    roster sync, again inside a re-probe's own session), so call-count
    bookkeeping would attribute the wrong result to the wrong query.
    """
    def __init__(self, rows, grantbot_token):
        self._rows = rows
        self._grantbot_token = grantbot_token

    async def execute(self, stmt):
        if len(stmt.selected_columns) == 1:
            return _ScalarResult(self._grantbot_token)
        return _RowsResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _row(agent_id, token="xoxb-real", role="pi_lab"):
    return types.SimpleNamespace(
        agent_id=agent_id, bot_name=f"{agent_id.capitalize()}Bot",
        pi_name=f"PI {agent_id}", slack_bot_token=token, role=role,
    )


class _ProbeClient:
    """Stand-in for the throwaway AgentSlackClient in _resolve_service_bot_uids.

    Also stands in for a roster client when built with agent_id != "grantbot"
    (test_rotation below rebuilds no roster clients, but shares the same
    patched import), so bot_user_id is derived from agent_id for that case.

    ``dead_tokens`` simulates a valid-prefix-but-dead token (Slack rejects
    auth.test, e.g. invalid_auth) — connect() returns False and no uid is
    ever learned, the same shape AgentSlackClient.connect() itself returns
    for that failure mode.
    """
    built: list["_ProbeClient"] = []
    dead_tokens: set[str] = set()

    def __init__(self, agent_id, bot_token):
        self.agent_id = agent_id
        self.bot_token = bot_token
        self.bot_user_id = None
        type(self).built.append(self)

    def connect(self):
        if self.bot_token in type(self).dead_tokens:
            return False
        self.bot_user_id = (
            "U_GRANTBOT_PROBE" if self.agent_id == "grantbot" else f"U_{self.agent_id}"
        )
        return True


def _patch_probe(monkeypatch):
    _ProbeClient.built = []
    _ProbeClient.dead_tokens = set()
    monkeypatch.setattr("src.agent.slack_client.AgentSlackClient", _ProbeClient)
    return _ProbeClient.built


def _patch_settings(monkeypatch, grantbot_field="", tokens_map=None):
    """One fake object serves both call sites: src.agent.simulation.get_settings
    (read for slack_bot_token_grantbot) and src.services.slack_tokens.get_settings
    (read by env_token's Settings.get_slack_tokens(), inside get_agent_bot_token's
    own fallback) — separate name bindings from the same `from src.config import
    get_settings`, so both need patching independently.
    """
    fake = types.SimpleNamespace(
        slack_bot_token_grantbot=grantbot_field,
        get_slack_tokens=lambda: dict(tokens_map or {}),
        # _recompute_allowed_sender_ids (called unconditionally by
        # _sync_roster_from_db before the early return) reads this too; off
        # short-circuits it without needing the rest of the cohort settings.
        cohort_isolation_enabled=False,
    )
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: fake)
    monkeypatch.setattr("src.services.slack_tokens.get_settings", lambda: fake)


def _engine(session_factory=None):
    return SimulationEngine(
        agents=[Agent(agent_id="su", bot_name="SuBot", pi_name="PI su")],
        slack_clients={},
        session_factory=session_factory,
    )


# ---------------------------------------------------------------------------
# Token resolution order
# ---------------------------------------------------------------------------


class TestServiceBotTokenResolution:
    async def test_probe_uses_db_token_when_present(self, monkeypatch):
        _patch_settings(monkeypatch, grantbot_field="")
        built = _patch_probe(monkeypatch)
        engine = _engine(session_factory=lambda: _ScalarDB("xoxb-db-grantbot"))

        await engine._resolve_service_bot_uids()

        assert len(built) == 1
        assert built[0].bot_token == "xoxb-db-grantbot"
        assert engine._service_bot_uids == {"U_GRANTBOT_PROBE": "grantbot"}
        assert engine._service_bot_tokens["grantbot"] == "xoxb-db-grantbot"

    async def test_falls_back_to_settings_field_when_db_has_no_token(self, monkeypatch):
        """get_agent_bot_token's OWN .env fallback (Settings.get_slack_tokens())
        has no "grantbot" key — deliberately left empty here — so the dedicated
        slack_bot_token_grantbot field must be what the probe actually uses.
        """
        _patch_settings(monkeypatch, grantbot_field="xoxb-env-grantbot", tokens_map={})
        built = _patch_probe(monkeypatch)
        engine = _engine(session_factory=lambda: _ScalarDB(None))

        await engine._resolve_service_bot_uids()

        assert built[0].bot_token == "xoxb-env-grantbot"
        assert engine._service_bot_tokens["grantbot"] == "xoxb-env-grantbot"

    async def test_no_session_factory_uses_settings_field_without_raising(self, monkeypatch):
        _patch_settings(monkeypatch, grantbot_field="xoxb-env-grantbot")
        built = _patch_probe(monkeypatch)
        engine = _engine(session_factory=None)

        await engine._resolve_service_bot_uids()  # must not raise

        assert built[0].bot_token == "xoxb-env-grantbot"
        assert engine._service_bot_tokens["grantbot"] == "xoxb-env-grantbot"


# ---------------------------------------------------------------------------
# Re-probe on DB-side rotation, via _sync_roster_from_db
# ---------------------------------------------------------------------------


class TestServiceBotReprobeOnRotation:
    async def test_rotation_reprobes_and_flushes_message_log_uid_map(self, monkeypatch):
        _patch_settings(monkeypatch, grantbot_field="")
        _patch_probe(monkeypatch)
        engine = _engine(
            session_factory=lambda: _RosterAndGrantbotDB(
                rows=[_row("su")], grantbot_token="xoxb-new-grantbot",
            ),
        )
        engine.slack_clients["su"] = _ProbeClient("su", "xoxb-real")
        engine.slack_clients["su"].bot_user_id = "U_su"
        engine._load_pi_mappings = AsyncMock()
        engine._load_publication_records = AsyncMock()
        engine._build_lab_directories = lambda: None
        # Simulate a previous successful probe on a now-stale token.
        engine._service_bot_tokens["grantbot"] = "xoxb-old-grantbot"
        engine._service_bot_uids = {"U_OLD_GRANTBOT_PROBE": "grantbot"}

        await engine._sync_roster_from_db()

        assert engine._service_bot_tokens["grantbot"] == "xoxb-new-grantbot"
        assert engine._service_bot_uids.get("U_GRANTBOT_PROBE") == "grantbot"
        # The commit-1 flush: message_log's uid-map COPY must reflect it too.
        assert engine.message_log._bot_uid_to_agent.get("U_GRANTBOT_PROBE") == "grantbot"

    async def test_no_rotation_does_not_reprobe(self, monkeypatch):
        _patch_settings(monkeypatch, grantbot_field="")
        built = _patch_probe(monkeypatch)
        engine = _engine(
            session_factory=lambda: _RosterAndGrantbotDB(
                rows=[_row("su")], grantbot_token="xoxb-same-grantbot",
            ),
        )
        engine.slack_clients["su"] = _ProbeClient("su", "xoxb-real")
        engine.slack_clients["su"].bot_user_id = "U_su"
        built.clear()  # drop the client above from the recorded list
        engine._load_pi_mappings = AsyncMock()
        engine._load_publication_records = AsyncMock()
        engine._build_lab_directories = lambda: None
        engine._service_bot_tokens["grantbot"] = "xoxb-same-grantbot"
        engine._service_bot_uids = {"U_OLD_GRANTBOT_PROBE": "grantbot"}

        await engine._sync_roster_from_db()

        assert built == []  # no probe (and no roster rebuild — su's token is unchanged)
        assert engine._service_bot_uids == {"U_OLD_GRANTBOT_PROBE": "grantbot"}


# ---------------------------------------------------------------------------
# A dead (valid-prefix, Slack-rejected) token must be probed once, not forever
# ---------------------------------------------------------------------------


class TestServiceBotDeadTokenSuppression:
    async def test_dead_token_probed_once_across_three_ticks_one_warning(
        self, monkeypatch, caplog,
    ):
        """Before the fix, a dead grantbot DB token was recorded in
        _service_bot_tokens only on SUCCESS, so `grantbot_db_token !=
        self._service_bot_tokens.get("grantbot")` stayed True forever and
        _sync_roster_from_db re-probed (and re-logged the same warning) on
        every single tick.
        """
        import logging

        _patch_settings(monkeypatch, grantbot_field="")
        _patch_probe(monkeypatch)
        _ProbeClient.dead_tokens = {"xoxb-dead-grantbot"}
        engine = _engine(
            session_factory=lambda: _RosterAndGrantbotDB(
                rows=[_row("su")], grantbot_token="xoxb-dead-grantbot",
            ),
        )
        engine.slack_clients["su"] = _ProbeClient("su", "xoxb-real")
        engine.slack_clients["su"].bot_user_id = "U_su"
        engine._load_pi_mappings = AsyncMock()
        engine._load_publication_records = AsyncMock()
        engine._build_lab_directories = lambda: None

        caplog.set_level(logging.WARNING, logger="src.agent.simulation")
        for _ in range(3):
            engine._last_roster_poll = 0.0  # bypass the 30s roster-poll throttle
            await engine._sync_roster_from_db()

        grantbot_attempts = [c for c in _ProbeClient.built if c.agent_id == "grantbot"]
        assert len(grantbot_attempts) == 1
        grantbot_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "grantbot" in r.getMessage().lower()
        ]
        assert len(grantbot_warnings) == 1
        assert engine._service_bot_uids == {}
        assert engine._service_bot_tokens["grantbot"] == "xoxb-dead-grantbot"

    async def test_probe_retried_once_the_db_token_changes(self, monkeypatch):
        """A previously recorded FAILED attempt on a different, now-stale
        token must not suppress a probe of a genuinely new token."""
        _patch_settings(monkeypatch, grantbot_field="")
        _patch_probe(monkeypatch)
        engine = _engine(
            session_factory=lambda: _RosterAndGrantbotDB(
                rows=[_row("su")], grantbot_token="xoxb-new-grantbot",
            ),
        )
        engine.slack_clients["su"] = _ProbeClient("su", "xoxb-real")
        engine.slack_clients["su"].bot_user_id = "U_su"
        engine._load_pi_mappings = AsyncMock()
        engine._load_publication_records = AsyncMock()
        engine._build_lab_directories = lambda: None
        engine._service_bot_tokens["grantbot"] = "xoxb-dead-grantbot"

        await engine._sync_roster_from_db()

        grantbot_attempts = [c for c in _ProbeClient.built if c.agent_id == "grantbot"]
        assert len(grantbot_attempts) == 1
        assert engine._service_bot_uids.get("U_GRANTBOT_PROBE") == "grantbot"
        assert engine._service_bot_tokens["grantbot"] == "xoxb-new-grantbot"
