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
    Degrades to DEFAULT_TEMPLATE on any read failure (file missing, unreadable,
    or containing non-UTF-8 bytes).
    """
    path = roles.PROMPTS_DIR / TEMPLATE_FILENAME
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return DEFAULT_TEMPLATE


def render_run_start_announcement(
    values: dict[str, str], template_body: str | None = None
) -> str:
    """Render the announcement. Always sentinel-prefixed.

    ``template_body``, when given, is used INSTEAD of the template file —
    an admin-configured override (Task 6, ``app_settings`` key
    ``run_start_announcement_template``). ``None`` (the default) means
    "behave exactly as before": read ``prompts/run_start_announcement.md``
    if present, else ``DEFAULT_TEMPLATE``.

    A malformed template body — whether an operator's file or a DB override —
    degrades to the built-in default with one WARNING: a broken customization
    must not cost the run its announcement. The final
    ``DEFAULT_TEMPLATE.format_map(values)`` is DELIBERATELY unguarded, though:
    a missing engine-supplied key there is a code bug (a mismatch between
    ``ANNOUNCEMENT_VALUE_KEYS`` and what the caller actually populates), not an
    operator-customization failure, and it must fail loudly in tests rather
    than silently swallow the symptom. The engine's own caller
    (``SimulationEngine._announce_run_start``) has its own outer
    ``except Exception`` for exactly that case, so this function is not
    "never raises" end to end — only the template-body path is guarded.
    """
    body = template_body if template_body is not None else _template_body()
    try:
        rendered = body.format_map(values)
    except Exception as exc:
        source = (
            f"prompts/{TEMPLATE_FILENAME}"
            if template_body is None
            else "the DB-overridden run_start_announcement template"
        )
        logger.warning(
            "%s failed to render (%s: %s) — using the built-in "
            "default announcement body",
            source, type(exc).__name__, exc,
        )
        rendered = DEFAULT_TEMPLATE.format_map(values)
    return f"{RUN_START_MARKER_PREFIX}\n{rendered}"


def validate_template(body: str) -> str | None:
    """``None`` if ``body`` renders cleanly against a sample values dict
    covering every ``ANNOUNCEMENT_VALUE_KEYS`` placeholder; otherwise a
    human-readable error naming what went wrong.

    For the admin-panel route (Task 7) to reject a bad template inline at
    save time, rather than silently degrading the way
    ``render_run_start_announcement``'s runtime guard does — the two are
    deliberately separate checks: this one is advisory and used before a
    value is ever stored, that one is the last-resort safety net for
    whatever ends up stored (or in the file) regardless.
    """
    sample = {k: "sample" for k in ANNOUNCEMENT_VALUE_KEYS}
    try:
        body.format_map(sample)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def parse_announce_channels(raw: str) -> list[str]:
    """Comma-separated channel names -> deduplicated list, first occurrence
    order preserved. Empty string -> [] (feature off)."""
    return list(dict.fromkeys(c.strip() for c in raw.split(",") if c.strip()))
