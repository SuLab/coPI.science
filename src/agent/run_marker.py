"""Run-start announcement: sentinel, template, rendering, channel list.

The engine posts one marker per configured channel when a --fresh run starts
(SimulationEngine._announce_run_start). Everything here is deliberately
dependency-light (no models, no DB) so the ingest paths can import the
predicate and unit tests need nothing but a tmp dir.

THE PREFIX IS LOAD-BEARING. Both Slack-ingest paths
(_poll_slack_for_bot_messages and _rebuild_state_from_slack in
src/agent/simulation.py) drop any message matching is_run_start_marker so the
engine's own markers are never mirrored into agent_messages — without that, the
first restart of a fresh run would re-ingest the marker as a bot post
(_known_slack_ts is seeded from stored rows only). Changing the prefix orphans
every marker already posted: they would start being ingested on the next
resume. Do not change it casually; if it must change, keep the old prefix
recognized alongside the new one.

The prefix is PREPENDED BY CODE, never part of the template, so operator
customization of prompts/run_start_announcement.md cannot break the sentinel.
"""

from __future__ import annotations

import logging

from src.agent import roles

logger = logging.getLogger(__name__)

RUN_START_MARKER_PREFIX = ":checkered_flag: NEW EXPERIMENTAL RUN"

TEMPLATE_FILENAME = "run_start_announcement.md"

#: Every placeholder the template may use. The engine supplies exactly these.
ANNOUNCEMENT_VALUE_KEYS: tuple[str, ...] = (
    "run_id", "started_at", "run_duration",
    "git_commit", "git_branch", "git_dirty",
    "hub_prompts_version", "hub_prompts_hash",
    "pi_prompts_version", "pi_prompts_hash",
    "rubric_version", "rubric_hash",
)

#: Fallback body when the template file is missing or malformed. Kept in sync
#: with the SHIPPED prompts/run_start_announcement.md by hand — they may drift
#: once an operator customizes the file, which is the point of the file.
DEFAULT_TEMPLATE = """\
*A new simulation run is starting.*
- Run: {run_id}
- Started: {started_at}
- Planned duration: {run_duration}
- Code: commit {git_commit} on branch {git_branch} ({git_dirty})
- Hub prompts: v{hub_prompts_version} (hash {hub_prompts_hash})
- PI prompts: v{pi_prompts_version} (hash {pi_prompts_hash})
- Rubric: v{rubric_version} (hash {rubric_hash})
Messages above this line belong to earlier runs."""


def is_run_start_marker(text: str | None) -> bool:
    """True for engine-authored run-start markers (and nothing else the
    system writes: the prefix is reserved — no prompt offers it, and the
    realistic misfire is a foreign bot opening a message with the exact
    prefix, whose only cost is that one message not being mirrored)."""
    return bool(text) and text.lstrip().startswith(RUN_START_MARKER_PREFIX)


def _template_body() -> str:
    """The operator's template if present and readable, else the default.

    Read at call time (not import) from roles.PROMPTS_DIR so the bind-mounted
    prompts/ directory is honoured and tests can monkeypatch the dir.
    """
    path = roles.PROMPTS_DIR / TEMPLATE_FILENAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TEMPLATE


def render_run_start_announcement(values: dict[str, str]) -> str:
    """Render the announcement. Never raises; always sentinel-prefixed.

    A malformed operator template (unknown placeholder, stray braces) degrades
    to the built-in default with one WARNING — a broken customization must not
    cost the run its announcement.
    """
    body = _template_body()
    try:
        rendered = body.format_map(values)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "prompts/%s failed to render (%s: %s) — using the built-in "
            "default announcement body",
            TEMPLATE_FILENAME, type(exc).__name__, exc,
        )
        rendered = DEFAULT_TEMPLATE.format_map(values)
    return f"{RUN_START_MARKER_PREFIX}\n{rendered}"


def parse_announce_channels(raw: str) -> list[str]:
    """Comma-separated channel names -> list. Empty string -> [] (feature off)."""
    return [c.strip() for c in raw.split(",") if c.strip()]
