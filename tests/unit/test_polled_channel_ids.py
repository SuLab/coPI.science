import inspect

from src.agent.engine import channel_directory, constants, slack_io
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE


def test_selection(monkeypatch):
    cd = channel_directory.ChannelDirectory.__new__(channel_directory.ChannelDirectory)
    cd._channel_id_map = {"general": "C1", "random": "C2", "collab-x": "C3"}
    cd._channel_visibility = {"collab-x": VISIBILITY_COLLAB_PRIVATE}
    monkeypatch.setattr(constants, "SEEDED_CHANNELS", ("general",))
    assert cd._polled_channel_ids() == {"general": "C1", "collab-x": "C3"}


def test_both_callers_use_it():
    for fn in (slack_io.SlackIO._poll_slack_for_bot_messages, slack_io.SlackIO._seed_slack_cursors_without_ingest):
        src = inspect.getsource(fn)
        assert "_polled_channel_ids()" in src
        assert "_channel_id_map.items()" not in src
