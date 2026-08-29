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

**Why `score`/`band` can be passed in verbatim (2026-08-29, fix round 1).** The
engine's own call site (`_post_assessment_summary`) computes band/score live
from a verdict's `scores` dict, against whichever rubric document THIS PROCESS
has loaded — correct for a verdict that just concluded, because "live" and
"the rubric that scored it" are the same document there. A REPAIRED headline
has no such guarantee: `scripts/backfill_assessment_headlines.py` can run weeks
or months after the rubric has moved on, and recomputing from `scores` would
then publish a band/score the STORED row does not actually carry — measured
directly against production run `61ccad6d`, whose rows are all stamped rubric
3.2.0 while the live document is 3.4.0. `opportunity_assessments` already
carries `weighted_score`/`band`, computed once at write time under the rubric
that WAS live then, so the repair script passes those straight through instead
of recomputing. Supplying both `score` and `band` skips the rubric functions
entirely; omitting either one (or both) reproduces today's compute-from-
`scores` behaviour exactly, which is what keeps the engine's own call site —
and its D12 sentinel test, `tests/unit/test_assessments_summary_post.py` —
unchanged.
"""

from __future__ import annotations

from src.services.blackbird_rubric import band as _rubric_band
from src.services.blackbird_rubric import weighted_score as _rubric_weighted_score

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
    score: float | None = None,
    band: str | None = None,
) -> str:
    """Render the complete Slack text for one headline.

    ``score``/``band`` are an explicit override: when BOTH are supplied, they
    are used verbatim and `scores` is never consulted at all — no call to
    `weighted_score`/`band` happens. Supplying only one of the two (or
    neither) falls back to computing from `scores`, exactly as this function
    behaved before this parameter pair existed; a partial override would
    leave a caller-supplied band describing a different score than the one
    this function would go on to compute, which is worse than not
    overriding at all. See the module docstring for why a caller — the
    repair script — would want this.

    The band/score segment is omitted entirely — rather than printing `None`
    or a `weighted_score({})` 0.00 that bands as a decline nobody made — in
    exactly ONE case: the compute path reached an empty (or non-dict) `scores`
    map. There is no separate "absent override" case, and the distinction is
    worth stating because the obvious reading is wrong: `score=None` does not
    suppress the segment, it declines the override, and a non-empty `scores`
    then falls through and COMPUTES band/score against whichever rubric
    document this process has loaded — the live recomputation the override
    exists to prevent.

    A partial override is therefore unreachable rather than defended, and
    deliberately so. `_persist_assessment` writes `weighted_score`, `band` and
    `scores` from one computation, so a stored row carries either all three or
    none of them (`scores or None` alongside a NULL score/band); the repair
    script passes `row.weighted_score`/`row.band` straight through, so it
    supplies both or neither. Rejecting a partial override in code would add a
    branch no caller can reach — untestable except by constructing the state
    that cannot occur — so this is documented instead. A future caller that
    can produce one (a hand-built row, a partial backfill) must pass both
    values or accept a live recomputation.
    """
    if score is not None and band is not None:
        score_part = f" (band: {band}, score: {score:.1f})"
    else:
        score_map = scores if isinstance(scores, dict) else {}
        if score_map:
            computed_score = _rubric_weighted_score(score_map)
            score_part = (
                f" (band: {_rubric_band(computed_score)}, score: {computed_score:.1f})"
            )
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
