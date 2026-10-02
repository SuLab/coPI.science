"""The Stop dialog's cap must equal the engine's shutdown cap."""
from src.agent.engine.constants import HEADLINES_MAX_AT_SHUTDOWN
from src.routers.admin.simulation import STOP_ANNOUNCE_CAP


def test_the_stop_dialog_cap_matches_the_engine():
    assert STOP_ANNOUNCE_CAP == HEADLINES_MAX_AT_SHUTDOWN
