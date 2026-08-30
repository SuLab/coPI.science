"""DB-overridable run-start announce channels/template (Task 6).

`_announce_run_start` reads two `app_settings` keys —
`run_start_announce_channels` and `run_start_announcement_template` — via a
small `_announce_overrides()` helper. Either key's value wins over the
`Settings` default when non-None; with neither row (or with no
`session_factory` at all) the whole run-start announcement stays
byte-identical to the pre-Task-6 behaviour pinned by
`test_run_start_announcement.py`. A KV read failure must degrade to the
Settings/template-file default with one WARNING — announcing a run start
must never die on a KV hiccup.
"""
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent import roles
from src.agent.agent import Agent
from src.agent.run_marker import (
    ANNOUNCEMENT_VALUE_KEYS,
    RUN_START_MARKER_PREFIX,
    render_run_start_announcement,
    validate_template,
)
from src.agent.simulation import SimulationEngine
from src.models import AppSetting
from tests.fakes import FakeSlackClient

VALUES = {k: f"<{k}>" for k in ANNOUNCEMENT_VALUE_KEYS}


# --- (a) render_run_start_announcement(values, template_body=...) ---------


def test_template_body_override_renders_instead_of_the_file(monkeypatch, tmp_path):
    # An empty prompts dir: if the override were ignored, this would fall
    # through to DEFAULT_TEMPLATE instead, which contains none of "custom".
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)

    text = render_run_start_announcement(VALUES, template_body="custom {run_id}")

    assert text == f"{RUN_START_MARKER_PREFIX}\ncustom <run_id>"


def test_broken_override_falls_back_to_default_with_a_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    caplog.set_level(logging.WARNING)

    text = render_run_start_announcement(
        VALUES, template_body="broken {no_such_placeholder}",
    )

    assert text.startswith(RUN_START_MARKER_PREFIX)
    assert "<run_id>" in text  # the built-in default rendered instead
    assert "broken" not in text
    assert any(
        r.levelno == logging.WARNING and "run_start_announcement" in r.getMessage()
        for r in caplog.records
    )


def test_none_template_body_behaves_exactly_as_before(monkeypatch, tmp_path):
    """template_body=None (the default) must still read the file/default,
    unchanged from pre-Task-6 behaviour."""
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)

    text = render_run_start_announcement(VALUES)

    assert text.startswith(RUN_START_MARKER_PREFIX)
    for key in ANNOUNCEMENT_VALUE_KEYS:
        assert f"<{key}>" in text


# --- (b) validate_template --------------------------------------------------


def test_validate_template_accepts_a_body_using_only_known_placeholders():
    assert validate_template("ok {run_id}") is None


def test_validate_template_names_the_unknown_placeholder():
    error = validate_template("{nope}")

    assert error is not None
    assert "nope" in error


# --- (c)/(d) engine: _announce_run_start reads app_settings overrides ------


def _engine(monkeypatch, tmp_path, *, channels="general,assessments-summary"):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    monkeypatch.setattr(
        "src.agent.simulation.get_settings",
        lambda: SimpleNamespace(
            run_start_announce_channels=channels,
            reply_lane_max_in_flight=1,
        ),
    )
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    clients = {
        "blackbird": FakeSlackClient(agent_id="blackbird"),
        "wang": FakeSlackClient(agent_id="wang"),
    }
    eng = SimulationEngine(agents=[hub, lab], slack_clients=clients, fresh_start=True)
    eng._channel_id_map.update({
        "general": "C-GENERAL", "assessments-summary": "C-SUMMARY",
    })
    return eng, clients


async def _seed_app_setting(factory, key: str, value: str) -> None:
    async with factory() as db:
        db.add(AppSetting(key=key, value=value))
        await db.commit()


async def _cleanup_app_settings(factory, *keys: str) -> None:
    async with factory() as db:
        for key in keys:
            row = await db.get(AppSetting, key)
            if row is not None:
                await db.delete(row)
        await db.commit()


@pytest.mark.asyncio
async def test_db_channel_override_wins_over_the_settings_default(
    monkeypatch, tmp_path, engine,
):
    eng, clients = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    eng.session_factory = factory
    await _seed_app_setting(factory, "run_start_announce_channels", "general")

    try:
        await eng._announce_run_start()

        hub = clients["blackbird"]
        assert len(hub.posted_messages.get("C-GENERAL", [])) == 1
        assert "C-SUMMARY" not in hub.posted_messages
    finally:
        await _cleanup_app_settings(factory, "run_start_announce_channels")


@pytest.mark.asyncio
async def test_db_template_override_appears_in_the_posted_text(
    monkeypatch, tmp_path, engine,
):
    eng, clients = _engine(monkeypatch, tmp_path, channels="general")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    eng.session_factory = factory
    await _seed_app_setting(
        factory, "run_start_announcement_template", "override body {run_id}",
    )

    try:
        await eng._announce_run_start()

        hub = clients["blackbird"]
        texts = hub.posted_messages.get("C-GENERAL", [])
        assert len(texts) == 1
        assert "override body" in texts[0]
        assert texts[0].startswith(RUN_START_MARKER_PREFIX)
    finally:
        await _cleanup_app_settings(factory, "run_start_announcement_template")


@pytest.mark.asyncio
async def test_with_neither_row_behavior_is_byte_identical_to_today(
    monkeypatch, tmp_path, engine,
):
    """Pinned against test_run_start_announcement.py's
    test_posts_the_marker_to_every_configured_channel: a session_factory
    being present (but with no override rows) must change nothing."""
    eng, clients = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    eng.session_factory = factory

    await eng._announce_run_start()

    hub = clients["blackbird"]
    for ch_id in ("C-GENERAL", "C-SUMMARY"):
        texts = hub.posted_messages.get(ch_id, [])
        assert len(texts) == 1
        assert texts[0].startswith(RUN_START_MARKER_PREFIX)
    assert not clients["wang"].posted


@pytest.mark.asyncio
async def test_db_read_failure_falls_back_to_settings_with_a_warning(
    monkeypatch, tmp_path, engine, caplog,
):
    eng, clients = _engine(monkeypatch, tmp_path)

    def _boom():
        raise RuntimeError("db exploded")

    monkeypatch.setattr(eng, "session_factory", _boom)
    caplog.set_level(logging.WARNING, logger="src.agent.simulation")

    await eng._announce_run_start()  # must not raise

    hub = clients["blackbird"]
    for ch_id in ("C-GENERAL", "C-SUMMARY"):
        assert len(hub.posted_messages.get(ch_id, [])) == 1
    assert any(
        r.levelno == logging.WARNING and "app_settings" in r.getMessage()
        for r in caplog.records
    )
