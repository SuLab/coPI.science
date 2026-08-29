"""run_marker: the sentinel, the template, and the channel-list parser.

The sentinel prefix is load-bearing: both Slack-ingest paths drop matching
messages (see test_run_marker_ingest_skip.py), so the prefix must be
engine-prepended (customization can't remove it) and stable under the
markdown->mrkdwn conversion every outbound post goes through.
"""
import pytest

from src.agent import roles
from src.agent.channels import ASSESSMENTS_SUMMARY_CHANNEL, SEEDED_CHANNELS
from src.agent.run_marker import (
    ANNOUNCEMENT_VALUE_KEYS,
    RUN_START_MARKER_PREFIX,
    is_run_start_marker,
    parse_announce_channels,
    render_run_start_announcement,
)
from src.agent.slack_client import markdown_to_mrkdwn
from src.config import Settings

VALUES = {k: f"<{k}>" for k in ANNOUNCEMENT_VALUE_KEYS}


@pytest.fixture
def no_template(tmp_path, monkeypatch):
    """Point the template at an empty dir so the built-in default renders."""
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    return tmp_path


def test_rendered_text_starts_with_the_sentinel(no_template):
    text = render_run_start_announcement(VALUES)
    assert text.startswith(RUN_START_MARKER_PREFIX)
    assert is_run_start_marker(text)


def test_default_template_carries_every_value(no_template):
    text = render_run_start_announcement(VALUES)
    for key in ANNOUNCEMENT_VALUE_KEYS:
        assert f"<{key}>" in text, f"default template must render {{{key}}}"


def test_template_file_overrides_the_default(no_template):
    (no_template / "run_start_announcement.md").write_text(
        "custom body: {run_id}", encoding="utf-8"
    )
    text = render_run_start_announcement(VALUES)
    assert "custom body: <run_id>" in text
    assert text.startswith(RUN_START_MARKER_PREFIX)  # prefix is not the template's job


def test_template_cannot_remove_the_sentinel(no_template):
    (no_template / "run_start_announcement.md").write_text(
        "no placeholders at all", encoding="utf-8"
    )
    assert is_run_start_marker(render_run_start_announcement(VALUES))


def test_bad_placeholder_falls_back_to_default(no_template, caplog):
    (no_template / "run_start_announcement.md").write_text(
        "broken {no_such_placeholder}", encoding="utf-8"
    )
    text = render_run_start_announcement(VALUES)
    assert "broken" not in text
    assert "<run_id>" in text  # the default rendered instead
    assert any("run_start_announcement" in r.getMessage() for r in caplog.records)


def test_sentinel_survives_mrkdwn_conversion(no_template):
    assert is_run_start_marker(markdown_to_mrkdwn(render_run_start_announcement(VALUES)))


def test_predicate_rejects_ordinary_messages():
    assert not is_run_start_marker("a normal post about :checkered_flag: racing")
    assert not is_run_start_marker("")
    assert not is_run_start_marker(None)


def test_parse_announce_channels():
    assert parse_announce_channels(" general , social ,,") == ["general", "social"]
    assert parse_announce_channels("") == []


def test_setting_default_is_the_simulation_channels():
    """Drift alarm: the literal default in config.py must equal the seeded
    channels plus assessments-summary (config.py cannot import channels.py)."""
    default = Settings.model_fields["run_start_announce_channels"].default
    assert parse_announce_channels(default) == SEEDED_CHANNELS + [ASSESSMENTS_SUMMARY_CHANNEL]


def test_shipped_template_file_renders_cleanly():
    """Against the REAL prompts/ tree: the shipped template must format with
    exactly the documented keys (an operator edit that breaks it degrades to
    the default at runtime, but we ship it working)."""
    text = render_run_start_announcement(VALUES)
    assert is_run_start_marker(text)
    assert "<run_id>" in text
