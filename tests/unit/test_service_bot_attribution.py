"""Attribution of service-bot (GrantBot) Slack posts to agent_id "grantbot".

GrantBot posts with its own token, has no ``AgentRegistry`` row and never joins
the engine roster, so none of the roster plumbing knows it exists. Measured in
production: all 315 of its ``:moneybag:`` posts persisted with ``agent_id`` NULL
and ``sender_name`` set to its raw Slack uid. ``_entry_allowed`` fails closed on
a bot row with a NULL agent_id (unattributable ⇒ belongs to no cohort), so with
cohort isolation on, every funding post would be invisible to every gated agent.

Three seams carry the fix, one class each below:

- ``_bot_name_to_id`` — constructor-seeded, used by the live channel poller;
- ``_service_bot_uids`` — uid → agent_id, filled by ``_resolve_service_bot_uids``
  at start() because uid is the only key Slack reliably supplies for grantbot;
- ``_bot_uid_map`` — the roster/service merge the boot rebuild resolves against.

Plus the two ingestion paths end-to-end (live poller, boot rebuild), the
roster-sync remove path (which must not evict the seeded entry), and start()'s
call ORDER — the uid probe is only useful if it runs before the Slack reconcile
that consumes its output.
"""

import inspect
import re
import types
from unittest.mock import AsyncMock

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine

GRANTBOT_UID = "U0AMQGYBFL7"  # the real production uid, for recognisability


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClient:
    """The slice of AgentSlackClient the poller and the boot rebuild call."""

    def __init__(self, agent_id="su", bot_user_id=None, history=None, replies=None):
        self.agent_id = agent_id
        self.bot_user_id = bot_user_id or f"U_{agent_id}"
        self.is_connected = True
        self._history = history or []
        self._replies = replies or {}

    def connect(self):
        return True

    def poll_channel_messages(self, channel_id, oldest="0", limit=100):
        return list(self._history)

    def get_full_channel_history(self, channel_id):
        return list(self._history)

    def get_all_thread_replies(self, channel_id, thread_ts):
        return list(self._replies.get(thread_ts, []))

    def is_bot_user(self, user_id):
        return user_id.startswith("U_") or user_id == GRANTBOT_UID

    def resolve_user_name(self, user_id):
        return f"human-{user_id}"


def _engine(agent_specs=(("su", "SuBot"),), clients=None, **kw):
    """Engine with no DB and no Slack, name map left exactly as constructed.

    Deliberately does *not* overwrite ``_bot_name_to_id`` the way the cohort
    tests' helper does — the constructor's seeding is part of what's under test.
    """
    agents = [
        Agent(agent_id=aid, bot_name=bot_name, pi_name=f"PI {aid}")
        for aid, bot_name in agent_specs
    ]
    return SimulationEngine(agents=agents, slack_clients=clients or {}, **kw)


def _patch_settings(monkeypatch, token):
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: types.SimpleNamespace(slack_bot_token_grantbot=token),
    )


class _ProbeClient:
    """Stand-in for the throwaway AgentSlackClient in _resolve_service_bot_uids."""

    built: list["_ProbeClient"] = []

    def __init__(self, agent_id, bot_token, connect_result=True, raises=None):
        self.agent_id = agent_id
        self.bot_token = bot_token
        self.bot_user_id = None
        self._connect_result = connect_result
        self._raises = raises
        type(self).built.append(self)

    def connect(self):
        if self._raises:
            raise self._raises
        if self._connect_result:
            self.bot_user_id = GRANTBOT_UID
        return self._connect_result


def _patch_probe(monkeypatch, **kw):
    """Install a recording probe class; returns the list of instances built."""
    _ProbeClient.built = []

    def _factory(agent_id, bot_token):
        return _ProbeClient(agent_id, bot_token, **kw)

    monkeypatch.setattr("src.agent.slack_client.AgentSlackClient", _factory)
    return _ProbeClient.built


# ---------------------------------------------------------------------------
# Seam 1: the bot-name map
# ---------------------------------------------------------------------------


class TestNameMapSeeding:
    def test_constructor_seeds_grantbot(self):
        eng = _engine()
        assert eng._bot_name_to_id["grantbot"] == "grantbot"

    def test_seed_reaches_the_message_log_map(self):
        """set_bot_name_map *copies*, so the seed must precede that call."""
        eng = _engine()
        assert eng.message_log._bot_name_to_id["grantbot"] == "grantbot"

    def test_roster_bot_of_the_same_name_wins(self):
        """A PI actually named Grant owns bot_name GrantBot; the roster is truth."""
        eng = _engine(agent_specs=(("su", "SuBot"), ("grant", "GrantBot")))
        assert eng._bot_name_to_id["grantbot"] == "grant"
        assert eng.message_log._bot_name_to_id["grantbot"] == "grant"


# ---------------------------------------------------------------------------
# Seam 2: uid resolution (_resolve_service_bot_uids)
# ---------------------------------------------------------------------------


class TestResolveServiceBotUids:
    async def test_no_probe_when_slack_is_off(self, monkeypatch):
        _patch_settings(monkeypatch, "xoxb-real-grantbot")
        built = _patch_probe(monkeypatch)
        eng = _engine(slack_enabled=False)
        await eng._resolve_service_bot_uids()
        assert eng._service_bot_uids == {}
        assert built == []

    async def test_missing_token_is_skipped(self, monkeypatch):
        _patch_settings(monkeypatch, "")
        built = _patch_probe(monkeypatch)
        eng = _engine()
        await eng._resolve_service_bot_uids()
        assert eng._service_bot_uids == {}
        assert built == []

    async def test_placeholder_token_is_skipped(self, monkeypatch):
        """is_valid_token semantics: a seeded placeholder is not a credential."""
        _patch_settings(monkeypatch, "xoxb-placeholder-grantbot")
        built = _patch_probe(monkeypatch)
        eng = _engine()
        await eng._resolve_service_bot_uids()
        assert eng._service_bot_uids == {}
        assert built == []

    async def test_success_records_uid_without_joining_the_roster(self, monkeypatch):
        _patch_settings(monkeypatch, "xoxb-real-grantbot")
        built = _patch_probe(monkeypatch)
        eng = _engine(clients={"su": _FakeClient("su")})
        await eng._resolve_service_bot_uids()
        assert eng._service_bot_uids == {GRANTBOT_UID: "grantbot"}
        # The probe must never become a pollable/postable client.
        assert set(eng.slack_clients) == {"su"}
        assert len(built) == 1
        assert built[0].agent_id == "grantbot"
        # Its own token only — grantbot.py's SuBot fallback is not reused here,
        # because posts on su's token really do carry su's uid.
        assert built[0].bot_token == "xoxb-real-grantbot"

    async def test_failed_auth_does_not_abort_start(self, monkeypatch):
        _patch_settings(monkeypatch, "xoxb-real-grantbot")
        _patch_probe(monkeypatch, connect_result=False)
        eng = _engine()
        await eng._resolve_service_bot_uids()  # must not raise
        assert eng._service_bot_uids == {}

    async def test_connect_exception_does_not_abort_start(self, monkeypatch):
        """connect() only handles SlackApiError; DNS/SSL/socket errors escape it."""
        _patch_settings(monkeypatch, "xoxb-real-grantbot")
        _patch_probe(monkeypatch, raises=TimeoutError("dns"))
        eng = _engine()
        await eng._resolve_service_bot_uids()  # must not raise
        assert eng._service_bot_uids == {}


# ---------------------------------------------------------------------------
# Seam 3: the roster/service uid merge (_bot_uid_map)
# ---------------------------------------------------------------------------


class TestBotUidMap:
    def test_merges_service_entries_alongside_roster_entries(self):
        eng = _engine(clients={"su": _FakeClient("su")})
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        assert eng._bot_uid_map() == {"U_su": "su", GRANTBOT_UID: "grantbot"}

    def test_roster_wins_on_uid_collision(self):
        """grantbot on SuBot's token resolves to su's uid; those posts are su's."""
        eng = _engine(clients={"su": _FakeClient("su")})
        eng._service_bot_uids = {"U_su": "grantbot"}
        assert eng._bot_uid_map() == {"U_su": "su"}

    def test_tolerates_a_none_client_and_a_uidless_client(self):
        eng = _engine(clients={"su": _FakeClient("su"), "x": None, "y": _FakeClient("y")})
        eng.slack_clients["y"].bot_user_id = None
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        assert eng._bot_uid_map() == {"U_su": "su", GRANTBOT_UID: "grantbot"}


# ---------------------------------------------------------------------------
# Ingestion path: the live channel poller
# ---------------------------------------------------------------------------


def _bot_msg(ts, user, text=":moneybag: *New FOA*", username=None):
    msg = {"ts": ts, "user": user, "bot_id": "B123", "text": text, "thread_ts": None}
    if username is not None:
        msg["username"] = username
    return msg


class TestPollerAttribution:
    def _engine_with(self, history):
        eng = _engine(clients={"su": _FakeClient("su", history=history)})
        eng._channel_id_map = {"funding-opportunities": "C_FUND"}
        return eng

    async def test_uid_fallback_attributes_a_usernameless_post(self):
        """Slack omits `username` on grantbot's posts — uid is the only key left."""
        eng = self._engine_with([_bot_msg("1700000001.000100", GRANTBOT_UID)])
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        await eng._poll_slack_for_pi_messages()
        entry = eng.message_log.get_entry("1700000001.000100")
        assert entry is not None
        assert entry.sender_agent_id == "grantbot"
        assert entry.is_bot is True

    async def test_username_path_still_resolves_roster_bots(self):
        eng = self._engine_with(
            [_bot_msg("1700000002.000100", "U_su", username="SuBot")]
        )
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        await eng._poll_slack_for_pi_messages()
        assert eng.message_log.get_entry("1700000002.000100").sender_agent_id == "su"

    async def test_grantbot_username_resolves_without_the_uid_map(self):
        """The constructor seed alone covers posts that *do* carry a username."""
        eng = self._engine_with(
            [_bot_msg("1700000003.000100", GRANTBOT_UID, username="GrantBot")]
        )
        assert eng._service_bot_uids == {}
        await eng._poll_slack_for_pi_messages()
        assert eng.message_log.get_entry("1700000003.000100").sender_agent_id == "grantbot"

    async def test_unknown_bot_still_ingests_unattributed(self):
        """No silent widening: an unrecognised bot keeps failing closed."""
        eng = self._engine_with([_bot_msg("1700000004.000100", "U_MYSTERY")])
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        await eng._poll_slack_for_pi_messages()
        entry = eng.message_log.get_entry("1700000004.000100")
        assert entry is not None
        assert entry.sender_agent_id is None


# ---------------------------------------------------------------------------
# Ingestion path: the boot rebuild (_rebuild_state_from_slack)
# ---------------------------------------------------------------------------


class TestRebuildAttribution:
    def _engine_with(self, history, replies=None):
        client = _FakeClient("su", history=history, replies=replies)
        eng = _engine(clients={"su": client})
        eng._channel_id_map = {"funding-opportunities": "C_FUND"}
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        return eng

    async def test_channel_history_resolves_by_uid(self):
        eng = self._engine_with([_bot_msg("1700000010.000100", GRANTBOT_UID)])
        await eng._rebuild_state_from_slack()
        entry = eng.message_log.get_entry("1700000010.000100")
        assert entry is not None
        assert entry.sender_agent_id == "grantbot"
        # sender_name still falls back to the uid; the fix is the agent_id.
        assert entry.sender_name == GRANTBOT_UID

    async def test_thread_replies_resolve_by_uid(self):
        """The same merged map has to reach the reply loop, not just the roots."""
        root = _bot_msg("1700000020.000100", "U_su", username="SuBot")
        root["reply_count"] = 1
        reply = _bot_msg("1700000020.000200", GRANTBOT_UID, text=":moneybag: reprint")
        eng = self._engine_with([root], replies={"1700000020.000100": [root, reply]})
        await eng._rebuild_state_from_slack()
        assert eng.message_log.get_entry("1700000020.000200").sender_agent_id == "grantbot"

    async def test_roster_uid_is_not_shadowed_by_a_service_entry(self):
        eng = self._engine_with([_bot_msg("1700000030.000100", "U_su")])
        eng._service_bot_uids = {"U_su": "grantbot"}
        await eng._rebuild_state_from_slack()
        assert eng.message_log.get_entry("1700000030.000100").sender_agent_id == "su"


# ---------------------------------------------------------------------------
# The seed must survive roster churn (_sync_roster_from_db)
# ---------------------------------------------------------------------------


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
    """Roster rows on the first query; nothing after (publications, etc.)."""

    def __init__(self, rows):
        self._rows = rows
        self._calls = 0

    async def execute(self, _stmt):
        self._calls += 1
        return _FakeResult(self._rows if self._calls == 1 else [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestRosterSyncKeepsTheSeed:
    def _engine(self, rows, existing):
        eng = _engine(
            agent_specs=tuple((a, f"{a.capitalize()}Bot") for a in existing),
            clients={a: _FakeClient(a) for a in existing},
            session_factory=lambda: _FakeDB(rows),
        )
        eng._load_pi_mappings = AsyncMock()
        eng._build_lab_directories = lambda: None
        return eng

    async def test_removal_does_not_evict_grantbot(self, monkeypatch):
        """The remove path pops by matching agent_id; "grantbot" is never one."""
        monkeypatch.setattr(
            "src.agent.slack_client.AgentSlackClient",
            lambda agent_id, bot_token: _FakeClient(agent_id),
        )
        eng = self._engine([_row("su")], ["su", "wiseman"])
        await eng._sync_roster_from_db()
        assert "wisemanbot" not in eng._bot_name_to_id  # sanity: removal happened
        assert eng._bot_name_to_id["grantbot"] == "grantbot"
        # set_bot_name_map re-copies at the end of a churn tick — the log must
        # still carry the seed afterwards, since the poller reads *its* map.
        assert eng.message_log._bot_name_to_id["grantbot"] == "grantbot"

    async def test_addition_keeps_grantbot(self, monkeypatch):
        monkeypatch.setattr(
            "src.agent.slack_client.AgentSlackClient",
            lambda agent_id, bot_token: _FakeClient(agent_id),
        )
        eng = self._engine([_row("su"), _row("wiseman")], ["su"])
        await eng._sync_roster_from_db()
        assert eng._bot_name_to_id["wisemanbot"] == "wiseman"
        assert eng.message_log._bot_name_to_id["grantbot"] == "grantbot"

    async def test_uid_map_survives_roster_churn(self, monkeypatch):
        """_service_bot_uids is process state; nothing in the sync touches it."""
        monkeypatch.setattr(
            "src.agent.slack_client.AgentSlackClient",
            lambda agent_id, bot_token: _FakeClient(agent_id),
        )
        eng = self._engine([_row("su")], ["su", "wiseman"])
        eng._service_bot_uids = {GRANTBOT_UID: "grantbot"}
        await eng._sync_roster_from_db()
        # Sanity, as in the two tests above: a sync that returned early (bad
        # fake DB, changed query shape) would leave the uid map untouched too,
        # so the surviving entry below would prove nothing on its own.
        assert "wisemanbot" not in eng._bot_name_to_id
        assert eng._bot_uid_map()[GRANTBOT_UID] == "grantbot"


# ---------------------------------------------------------------------------
# start() ordering: the probe has to precede the reconcile that consumes it
# ---------------------------------------------------------------------------


def _order_recorder(order: list[str], name: str, is_async: bool):
    """A stub that records that it ran, with the same async-ness as the real method."""
    if is_async:
        async def _stub(*_a, **_k):
            order.append(name)
        return _stub

    def _stub(*_a, **_k):
        order.append(name)
    return _stub


class TestStartOrdering:
    """``_resolve_service_bot_uids`` must run BETWEEN the two rebuilds.

    ``_rebuild_state_from_slack`` attributes bot senders through
    ``_bot_uid_map()``, so if the probe ran after it, the reconcile would ingest
    grantbot's posts with a NULL agent_id and — because ``MessageLog.append`` is
    ts-idempotent — nothing would ever revisit them. Swapping two adjacent
    ``await`` lines in start() is a one-character-looking edit that no other test
    in this module notices, hence this one.

    Every ``self.<x>(...)`` call in start() is replaced with a recorder, so the
    engine does no DB, Slack or LLM work. The call list is discovered from
    start()'s own source rather than hand-kept, so a call added tomorrow gets
    stubbed too instead of reaching a real backend from a unit test.
    """

    WATCHED = (
        "_rebuild_state_from_db",
        "_resolve_service_bot_uids",
        "_rebuild_state_from_slack",
    )

    def _recording_engine(self, monkeypatch):
        eng = _engine()
        order: list[str] = []

        # Neither of these is a self-call, so the stubbing below misses them:
        # get_settings() would read the ambient environment, and the LLM
        # call-log hook is process-global state that must not leak into other
        # tests in the same session.
        _patch_settings(monkeypatch, "")
        monkeypatch.setattr(
            "src.agent.simulation.set_call_log_callback", lambda _cb: None
        )

        names = list(
            dict.fromkeys(
                re.findall(r"self\.(\w+)\(", inspect.getsource(SimulationEngine.start))
            )
        )
        for name in names:
            attr = getattr(SimulationEngine, name, None)
            assert callable(attr), f"start() calls self.{name}(), which is not a method"
            # setattr on the throwaway instance, not monkeypatch: undoing an
            # instance shadow of a class method would leave a bound method
            # pinned on the object, and this engine is discarded either way.
            setattr(
                eng,
                name,
                _order_recorder(order, name, inspect.iscoroutinefunction(attr)),
            )
        assert set(self.WATCHED) <= set(names), (
            f"start() no longer calls all of {self.WATCHED} — it was renamed or "
            "the step moved out of start(), so this test is no longer pinning it"
        )
        return eng, order

    async def test_probe_runs_after_the_db_rebuild_and_before_the_slack_one(
        self, monkeypatch
    ):
        eng, order = self._recording_engine(monkeypatch)
        await eng.start()
        # start() ran to completion, so nothing below is passing by absence.
        assert order[-1] == "_run_main_loop"
        for name in self.WATCHED:
            assert order.count(name) == 1, f"{name} ran {order.count(name)}x"
        positions = [order.index(name) for name in self.WATCHED]
        assert positions == sorted(positions), (
            "start() ran the startup steps out of order: "
            f"{[n for n in order if n in self.WATCHED]}"
        )

    async def test_the_slack_reconcile_sees_a_populated_uid_map(self, monkeypatch):
        """The same ordering stated as the behaviour it exists for.

        The index assertions above pin the line order; this one pins the reason,
        so a refactor that moves the probe into a helper still has to keep the
        uid map filled by the time the reconcile reads it.
        """
        eng, _order = self._recording_engine(monkeypatch)
        seen: list[dict] = []

        async def _probe(*_a, **_k):
            eng._service_bot_uids[GRANTBOT_UID] = "grantbot"

        async def _reconcile(*_a, **_k):
            seen.append(dict(eng._bot_uid_map()))

        eng._resolve_service_bot_uids = _probe
        eng._rebuild_state_from_slack = _reconcile
        await eng.start()
        assert seen == [{GRANTBOT_UID: "grantbot"}]
