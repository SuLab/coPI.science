"""A channel failing every poll tick must be visible — without flooding the log.

`_poll_slack_for_bot_messages`'s per-channel `except Exception:
logger.debug(...)` wraps the WHOLE ingest — the API call, `float(ts)`,
`ais_bot_user`, the append — and production runs at INFO. So a channel that
failed on every tick for hours produced no line anybody would ever see.

Raising it to WARNING alone trades invisibility for a flood: the poller runs
every CHANNEL_POLL_INTERVAL seconds and a broken channel fails every time. The
warning is therefore rate-limited per channel.
"""
import logging
import types

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

CH_NAME = "general"
CH_ID = "C_general"


class _BrokenClient(FakeSlackClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.attempts = 0

    async def apoll_channel_messages(self, *a, **kw):
        self.attempts += 1
        raise RuntimeError("channel_not_found")


def _engine():
    client = _BrokenClient(agent_id="wang")
    eng = SimulationEngine(
        agents=[Agent("wang", "WangBot", "Wang")],
        slack_clients={"wang": client},
    )
    eng._channel_id_map = {CH_NAME: CH_ID}
    return eng, client


def _poll_lines(caplog):
    """Every line the poller emitted about a failure, at ANY level.

    Keyed on the exception text rather than on the log message's wording, so
    the test cannot be satisfied (or defeated) by a rephrasing.
    """
    return [r for r in caplog.records if "channel_not_found" in r.getMessage()]


def _poll_records(caplog):
    """The operator-visible subset: production runs at INFO."""
    return [r for r in _poll_lines(caplog) if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_failing_channel_poll_is_visible_at_warning(caplog):
    eng, _client = _engine()
    # The suite's default level lets DEBUG through, which is exactly what
    # production does not do — assert on the RECORD LEVEL, not on presence.
    with caplog.at_level(logging.DEBUG, logger="src.agent.simulation"):
        eng._last_channel_poll = 0.0
        await eng._poll_slack_for_bot_messages()

    lines = _poll_lines(caplog)
    assert lines, "the poll failure was not logged at all"
    assert lines[0].levelno >= logging.WARNING, (
        "the poll failure is logged below WARNING; production runs at INFO, so "
        "a channel failing every tick for hours is invisible "
        f"(level={lines[0].levelname})"
    )
    assert CH_NAME in lines[0].getMessage()


@pytest.mark.asyncio
async def test_a_permanently_broken_channel_does_not_flood_the_log(
    caplog, monkeypatch,
):
    """The other half: one line per channel per interval, not one per tick."""
    eng, client = _engine()
    monkeypatch.setattr("src.agent.simulation.CHANNEL_POLL_INTERVAL", 0.0)

    with caplog.at_level(logging.DEBUG, logger="src.agent.simulation"):
        for _ in range(20):
            eng._last_channel_poll = 0.0
            await eng._poll_slack_for_bot_messages()

    assert client.attempts == 20, "setup: the poller should have tried 20 times"
    records = _poll_records(caplog)
    assert len(records) == 1, (
        f"20 consecutive failures produced {len(records)} log lines — a broken "
        "channel would bury everything else in the log"
    )


@pytest.mark.asyncio
async def test_a_second_broken_channel_gets_its_own_line(caplog, monkeypatch):
    """Rate-limited PER CHANNEL: one silencing the other would hide half of it."""
    eng, _client = _engine()
    eng._channel_id_map = {
        "general": "C_general", "chemical-biology": "C_chembio",
    }
    monkeypatch.setattr("src.agent.simulation.CHANNEL_POLL_INTERVAL", 0.0)

    with caplog.at_level(logging.DEBUG, logger="src.agent.simulation"):
        for _ in range(5):
            eng._last_channel_poll = 0.0
            await eng._poll_slack_for_bot_messages()

    named = {
        ch for r in _poll_records(caplog) for ch in ("general", "chemical-biology")
        if ch in r.getMessage()
    }
    assert named == {"general", "chemical-biology"}, (
        f"only these channels reported a failure: {named}"
    )
    assert len(_poll_records(caplog)) == 2


@pytest.mark.asyncio
async def test_a_channel_that_recovers_can_warn_again(caplog, monkeypatch):
    """A silenced channel must not stay silenced for the life of the run."""
    eng, client = _engine()
    monkeypatch.setattr("src.agent.simulation.CHANNEL_POLL_INTERVAL", 0.0)

    fake_now = [1000.0]
    monkeypatch.setattr(
        "src.agent.simulation.time",
        types.SimpleNamespace(time=lambda: fake_now[0], monotonic=lambda: fake_now[0]),
    )

    with caplog.at_level(logging.DEBUG, logger="src.agent.simulation"):
        eng._last_channel_poll = 0.0
        await eng._poll_slack_for_bot_messages()
        assert len(_poll_records(caplog)) == 1
        eng._last_channel_poll = 0.0
        await eng._poll_slack_for_bot_messages()
        assert len(_poll_records(caplog)) == 1, "second failure should be silenced"

        # Well past the silence window.
        fake_now[0] += 3600.0
        eng._last_channel_poll = 0.0
        await eng._poll_slack_for_bot_messages()

    assert len(_poll_records(caplog)) == 2, (
        "a channel silenced once stayed silenced forever, so a failure that "
        "outlives the window is never re-reported"
    )
    assert client.attempts == 3
