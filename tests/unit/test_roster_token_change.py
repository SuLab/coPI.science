from types import SimpleNamespace

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

VALID_A = "xoxb-1111-2222-aaaa"
VALID_B = "xoxb-1111-2222-bbbb"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return _Result(self._rows)


def _row(aid, token):
    return SimpleNamespace(agent_id=aid, bot_name=f"{aid}Bot", pi_name=aid, slack_bot_token=token, role="pi_lab")


@pytest.mark.asyncio
async def test_a_changed_token_takes_effect_within_one_poll_and_env_clients_are_kept(monkeypatch):
    import src.agent.slack_client as sc

    built = []

    class _Client(FakeSlackClient):
        def __init__(self, agent_id, bot_token, **kw):
            super().__init__(agent_id=agent_id, bot_token=bot_token)
            built.append((agent_id, bot_token))

    monkeypatch.setattr(sc, "AgentSlackClient", _Client)
    monkeypatch.setattr("src.services.slack_tokens.env_token", lambda aid: VALID_B if aid == "envlab" else None)
    rows = [_row("dblab", VALID_B), _row("envlab", None)]
    sim = SimulationEngine(
        agents=[Agent("dblab", "dblabBot", "dblab"), Agent("envlab", "envlabBot", "envlab")],
        slack_clients={"dblab": _Client("dblab", VALID_A), "envlab": _Client("envlab", VALID_B)},
        session_factory=lambda: _Session(rows), simulation_run_id=None, slack_enabled=True,
    )
    built.clear()
    sim.roster._last_roster_poll = 0.0
    await sim.roster._sync_roster_from_db()
    assert built == [("dblab", VALID_B)]
    assert sim.ctx.slack_clients["dblab"].bot_token == VALID_B
    sim.roster._last_roster_poll = 0.0
    await sim.roster._sync_roster_from_db()
    assert built == [("dblab", VALID_B)], "an unchanged resolved token never rebuilds a client"
