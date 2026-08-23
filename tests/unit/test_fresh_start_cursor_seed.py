"""A `--fresh` cursor seed must never leave a cursor at "0".

`_seed_slack_cursors_without_ingest` only advanced a cursor when it saw a
newest ts. But `AgentSlackClient.get_full_channel_history` SWALLOWS
`SlackApiError` and returns `[]` (src/agent/slack_client.py), so the seed's own
`try/except` never fires: the channel looks empty, the cursor stays `"0"`, and
the live poller — which uses a DIFFERENT endpoint that does not swallow —
re-imports that channel's whole back catalogue on the first tick. Harness: 30
messages ingested into a run that was supposed to start clean.

There are three ways to a `"0"` cursor, not one:

1. the swallowed fetch error above;
2. a genuinely-empty read that is indistinguishable from it;
3. `_client_for_channel(...) is None` — a private channel with no connected
   member bot, which the seed skipped with `continue`.

And the seed reached for `next(iter(self.slack_clients.values()), None)`, whose
first-in-dict client being disconnected skipped the ENTIRE seed while the live
poller (which uses `_next_poll_client`) carried on polling. The resume path
`_rebuild_state_from_slack` had the same line.
"""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.visibility import VISIBILITY_COLLAB_PRIVATE
from tests.fakes import FakeSlackClient

CH_NAME = "general"
CH_ID = "C_general"

BACK_CATALOGUE = [
    {"ts": "1700000001.000000", "user": "U_wang", "bot_id": "B1",
     "text": "a pitch from a previous run", "reply_count": 0},
    {"ts": "1700000002.000000", "user": "U_wang", "bot_id": "B1",
     "text": "and the hub's reply to it", "reply_count": 0},
]


class _Client(FakeSlackClient):
    """A FakeSlackClient with a settable connection flag and a failing history.

    `history_fails` reproduces the real client's behaviour exactly: it returns
    `[]` rather than raising, because `get_full_channel_history` catches
    `SlackApiError` itself. The live-poll endpoint is unaffected, which is what
    makes the back catalogue reappear.
    """

    def __init__(self, *a, connected: bool = True, history_fails: bool = False,
                 **kw):
        super().__init__(*a, **kw)
        self._connected = connected
        self.history_fails = history_fails

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_full_channel_history(self, channel_id, *a, **kw):
        if self.history_fails:
            return []
        return super().get_full_channel_history(channel_id, *a, **kw)


def _engine(clients: dict) -> SimulationEngine:
    agents = [Agent(aid, f"{aid.capitalize()}Bot", aid) for aid in clients]
    eng = SimulationEngine(agents=agents, slack_clients=clients, fresh_start=True)
    eng._channel_id_map = {CH_NAME: CH_ID}
    return eng


@pytest.mark.asyncio
async def test_a_failed_history_fetch_does_not_leave_the_cursor_at_zero():
    client = _Client(agent_id="wang", history_fails=True)
    client.channel_history[CH_ID] = list(BACK_CATALOGUE)
    eng = _engine({"wang": client})

    await eng._seed_slack_cursors_without_ingest()

    assert eng._poll_cursors.get(CH_ID, "0") != "0", (
        "the seed read the swallowed error as an empty channel and left the "
        "cursor at 0"
    )

    # The consequence, driven end to end: the live poller must find nothing.
    eng._last_channel_poll = 0.0
    await eng._poll_slack_for_bot_messages()
    assert len(eng.message_log) == 0, (
        "the first poll re-imported the back catalogue: "
        f"{[e.content for e in eng.message_log._entries]}"
    )


@pytest.mark.asyncio
async def test_a_private_channel_with_no_member_bot_does_not_leave_the_cursor_at_zero():
    """The third path to a "0" cursor, which the earlier write-up missed."""
    client = _Client(agent_id="wang")
    eng = _engine({"wang": client})
    eng._channel_id_map = {"priv-x": "G_priv_x"}
    eng._channel_visibility = {"priv-x": VISIBILITY_COLLAB_PRIVATE}
    # A member bot that is not among the connected clients: _client_for_channel
    # returns None and the seed used to `continue`.
    eng._private_channel_members["G_priv_x"] = {"absent"}

    await eng._seed_slack_cursors_without_ingest()

    assert eng._poll_cursors.get("G_priv_x", "0") != "0", (
        "a private channel with no connected member bot kept a 0 cursor, so "
        "the first tick that DOES have a member bot re-imports all of it"
    )


@pytest.mark.asyncio
async def test_the_seed_uses_a_connected_client():
    """One disconnected client, first in dict order, skipped the whole seed."""
    dead = _Client(agent_id="dead", connected=False)
    live = _Client(agent_id="wang", connected=True)
    live.channel_history[CH_ID] = list(BACK_CATALOGUE)
    # Insertion order is dict order: `next(iter(...))` picks `dead`.
    eng = _engine({"dead": dead, "wang": live})

    await eng._seed_slack_cursors_without_ingest()

    assert eng._poll_cursors.get(CH_ID) == "1700000002.000000", (
        "the seed gave up because the FIRST client in the dict was "
        "disconnected, while the live poller would have kept polling"
    )


@pytest.mark.asyncio
async def test_the_resume_path_also_uses_a_connected_client():
    """`_rebuild_state_from_slack` carried the identical line."""
    dead = _Client(agent_id="dead", connected=False)
    live = _Client(agent_id="wang", connected=True)
    live.channel_history[CH_ID] = list(BACK_CATALOGUE)
    eng = SimulationEngine(
        agents=[Agent("dead", "DeadBot", "Dead"), Agent("wang", "WangBot", "Wang")],
        slack_clients={"dead": dead, "wang": live},
        fresh_start=False,
    )
    eng._channel_id_map = {CH_NAME: CH_ID}

    await eng._rebuild_state_from_slack()

    assert len(eng.message_log) == 2, (
        "the resume reconcile gave up because the FIRST client in the dict was "
        "disconnected — a restart then cannot recover its own in-flight threads"
    )


@pytest.mark.asyncio
async def test_no_connected_client_at_all_is_still_a_clean_skip():
    """The other direction: Slack-off must stay a no-op, not a wall-clock cursor."""
    dead = _Client(agent_id="dead", connected=False)
    eng = _engine({"dead": dead})

    await eng._seed_slack_cursors_without_ingest()

    assert eng._poll_cursors == {}, (
        "with no connected client there is nothing to seed against — writing "
        "cursors here would hide real history from a later, connected run"
    )
