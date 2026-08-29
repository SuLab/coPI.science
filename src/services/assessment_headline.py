"""The one renderer for a `#assessments-summary` headline.

Extracted from `SimulationEngine._post_assessment_summary` on 2026-08-29 so the
engine and `scripts/backfill_assessment_headlines.py` cannot render differently
— a repaired headline that reads unlike a live one is worse than no repair,
because a reader cannot tell which rows were repaired.

**Content policy, not formatting (design D12).** Exactly five fields are ever
rendered: PI/lab name, project, recommendation, band/score, permalink. The
verdict's `rationale`, `red_flags`, `gating` and `raw_verdict` are never read
here at all, which is what keeps this post from saying more than the manager
read-only detail view already shows staff. Widening this — interpolating a
verdict wholesale, adding a "why" line — is a policy change requiring sign-off,
not a tidy-up.
"""

from __future__ import annotations

from src.services.blackbird_rubric import band, weighted_score

# This post's own display bound. `company_or_project` is a `Text` column with no
# width, so an unbounded value would turn a HEADLINE into a wall of model text
# that `split_for_slack` then cuts into several messages. The full title is
# always in the row and on the detail page the permalink's reader can reach.
PROJECT_DISPLAY_CHARS = 120
# `recommendation`'s own column width, so the post and the stored row can never
# disagree about it.
RECOMMENDATION_DISPLAY_CHARS = 30


def _clip(value: object, max_len: int) -> str | None:
    """A non-empty string clipped to ``max_len``, else ``None``.

    Drops a non-string outright: a model that answers `company_or_project` with
    an object would otherwise have a Python `repr` posted to a public channel.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:max_len]


def render_assessment_headline(
    *,
    pi_label: str,
    project: object,
    recommendation: object,
    scores: object,
    permalink: str | None,
) -> str:
    """Render the complete Slack text for one headline."""
    score_map = scores if isinstance(scores, dict) else {}
    if score_map:
        score = weighted_score(score_map)
        score_part = f" (band: {band(score)}, score: {score:.1f})"
    else:
        # An empty scores map is "we don't know", and `weighted_score({})` is a
        # 0.00 that bands as a decline nobody made — the same reason
        # `_persist_assessment` leaves those columns NULL.
        score_part = ""

    project_text = _clip(project, PROJECT_DISPLAY_CHARS) or "(untitled)"
    recommendation_text = (
        _clip(recommendation, RECOMMENDATION_DISPLAY_CHARS) or "unknown"
    )
    # Display form only — the stored verdict and every downstream engine
    # predicate keep writing "pass"; this headline is the one place a human
    # reads it, so it reads as "decline" (rubric banding.pass_label).
    display = "decline" if recommendation_text == "pass" else recommendation_text

    link_part = (
        f" — <{permalink}|View interview>" if permalink else " (link unavailable)"
    )
    return f":mag: {pi_label} — {project_text} → *{display}*{score_part}{link_part}"
