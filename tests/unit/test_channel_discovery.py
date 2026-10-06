import pytest

from src.agent.agent import Agent
from src.agent.simulation import SEEDED_CHANNELS, SimulationEngine
from tests.fakes import FakeSlackClient

MATCHING = (
    "# Wang Lab\n\n## Research Summary\n\nsmall molecule drug imaging\n\n"
    "## Key Methods and Technologies\n\n- single-cell RNA sequencing\n"
)
NOT_MATCHING = (
    "# Wang Lab\n\n## Research Summary\n\nsmall molecule drug\n\n"
    "## Key Methods and Technologies\n\n- live imaging\n"
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    (root / "public").mkdir(parents=True)
    (root / "public" / "wang.md").write_text(MATCHING, encoding="utf-8")
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", root)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Jane Wang")
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in (hub, lab)}
    eng = SimulationEngine(agents=[hub, lab], slack_clients=clients)
    eng._channel_id_map.update({name: f"C_{name}" for name in SEEDED_CHANNELS})
    return eng, hub, lab, clients, root


async def test_a_lab_joins_only_what_its_tag_sections_match(setup):
    eng, _hub, lab, clients, _root = setup
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == {"general", "single-cell-omics"}
    assert clients["wang"].joined_channels == {"general", "single-cell-omics"}


async def test_a_recompute_after_a_reload_drops_the_channel_but_keeps_slack_membership(setup):
    eng, _hub, lab, clients, root = setup
    await eng._phase1_channel_discovery(lab)
    (root / "public" / "wang.md").write_text(NOT_MATCHING, encoding="utf-8")
    lab.reload_profiles()
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == {"general"}
    assert clients["wang"].joined_channels == {"general", "single-cell-omics"}


async def test_the_hub_subscribes_to_every_seeded_channel(setup):
    eng, hub, _lab, clients, _root = setup
    await eng._phase1_channel_discovery(hub)
    assert hub.state.subscribed_channels == set(SEEDED_CHANNELS)
    assert clients["blackbird"].joined_channels == set(SEEDED_CHANNELS)


async def test_an_unchanged_persona_joins_nothing_twice(setup):
    eng, _hub, lab, clients, _root = setup
    await eng._phase1_channel_discovery(lab)
    clients["wang"].joined_channels.clear()
    await eng._phase1_channel_discovery(lab)
    assert clients["wang"].joined_channels == set()


async def test_tokenless_discovery_retries_when_the_client_is_adopted(setup):
    eng, _hub, lab, clients, _root = setup
    client = clients.pop('wang')
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == set()
    clients['wang'] = client
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == {'general', 'single-cell-omics'}
    assert client.joined_channels == {'general', 'single-cell-omics'}


async def test_a_missing_channel_id_is_retried_after_directory_refresh(setup):
    eng, _hub, lab, clients, _root = setup
    channel_id = eng._channel_id_map.pop('single-cell-omics')
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == {'general'}
    eng._channel_id_map['single-cell-omics'] = channel_id
    await eng._phase1_channel_discovery(lab)
    assert clients['wang'].joined_channels == {'general', 'single-cell-omics'}


async def test_a_refused_join_is_not_recorded_as_a_subscription(setup, monkeypatch):
    eng, _hub, lab, clients, _root = setup
    original = clients['wang'].ajoin_channel
    async def refused(_channel_id):
        return False
    monkeypatch.setattr(clients['wang'], 'ajoin_channel', refused)
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == set()
    monkeypatch.setattr(clients['wang'], 'ajoin_channel', original)
    await eng._phase1_channel_discovery(lab)
    assert lab.state.subscribed_channels == {'general', 'single-cell-omics'}


def test_the_lab_directory_is_gone_from_the_engine():
    import src.agent.simulation as sim

    for name in ("refresh_lab_directories", "_build_lab_directories"):
        assert name not in sim._FORWARD
        assert not hasattr(sim.SimulationEngine, name)
