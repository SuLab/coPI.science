"""The one renderer for a `#assessments-summary` headline.

Extracted from `SimulationEngine._post_assessment_summary` on 2026-08-29 so the
engine and `scripts/backfill_assessment_headlines.py` cannot render differently
— a repaired headline that reads unlike a live one is worse than no repair,
because a reader cannot tell which rows were repaired.

**Content policy, not formatting (design D12, widened once).** Exactly six
fields are ever rendered: PI/lab name, project, recommendation, band/score,
permalink, and — since 2026-09-09 — the sidecar's `elevator_pitch` on a second
line. The verdict's `rationale`, `red_flags`, `gating` and `raw_verdict` are
never read here at all, which is what keeps this post from saying more than
the manager read-only detail view already shows staff. The pitch widening
rests on the operator's assertion (2026-09-09, design §0.2/A6) that PIs cannot
join the Slack workspace — no code enforces that, and it is a SIDECAR field
that may carry a PI's unpublished disclosures, so publishing it is a real risk
accepted deliberately, not a free extension of the existing policy. Widening
this further — interpolating a verdict wholesale, adding a "why" line — is a
policy change requiring sign-off, not a tidy-up.

The pitch segment is rendered through `_clip_at_sentence`, not `_clip`: all 8
pitches measured in production (2026-09-14) run 1173–1406 characters against
`PITCH_DISPLAY_CHARS = 600`, so `elevator_pitch` is routinely longer than the
cap, and a plain `_clip` cut every one of them mid-word. The fix is to the
CLIP, not the cap — raising `PITCH_DISPLAY_CHARS` would publish more sidecar
prose to a public channel, which is the widening this section's policy exists
to bound. `score_rationale`, landing in the same change set, is a new sidecar
field and is deliberately **not** rendered here — it is not a seventh field,
and adding it would be exactly the kind of policy widening this section warns
against.

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

import re

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
# This post's own display bound for the pitch, the same reasoning as
# PROJECT_DISPLAY_CHARS above: `elevator_pitch` is an unbounded Text column and
# a headline is not the place for a wall of model prose. The prose contract
# asks for four to six sentences (scout_hub 1.7.0) and requires sentences
# 1-4 to END within ~550 characters, so the citation sentence completes inside this
# window; see _clip_at_sentence below, which publishes only COMPLETE sentences.
PITCH_DISPLAY_CHARS = 600


#: The last whitespace-started run in a window, so the word-boundary fallback
#: backs off to ANY whitespace rather than to a literal space only.
_TRAILING_WHITESPACE_RUN = re.compile(r"\s\S*$")


def _clip(value: object, max_len: int) -> str | None:
    """A non-empty string clipped to ``max_len``, else ``None``.

    Drops a non-string outright: a model that answers `company_or_project` with
    an object would otherwise have a Python `repr` posted to a public channel.
    """
    if not isinstance(value, str) or not value:
        return None
    return value[:max_len]


def _clip_at_sentence(value: object, max_len: int) -> str | None:
    """A non-empty string ending at a sentence boundary within ``max_len``,
    else ``None`` for a non-string.

    Behaviour, in order:

    * ``value`` no longer than ``max_len`` → returned **unchanged**, byte-
      identical to what `_clip` returns for the same input. Every short pitch,
      and every pitch of exactly `PITCH_DISPLAY_CHARS`, renders exactly as it
      does today.
    * Over the cap → cut after the LAST sentence terminator — a `.`, `!` or
      `?` followed by ANY whitespace character, which includes the newline of a
      markdown paragraph break, or a terminator at the very end of the
      ``max_len`` window whose NEXT character is whitespace — that leaves at
      least half the budget, and
      append `" …"`. The marker is unconditional on a real truncation: the
      chosen boundary is the HIGHEST qualifying one, and the pitch contract
      requires a citation sentence (sentence four under scout_hub >= 1.7.0),
      so that boundary can be an abbreviation ("et al. ", "e.g. ", "vs. ")
      rather than a sentence end. An
      abbreviation blocklist would be a guess; "there is more" is a fact. The
      marker is omitted only when the cut lands at the true end of the value.
    * Over the cap, no such boundary, but whitespace exists in the window →
      clip to ``max_len`` first, then back off to the last space and append
      `" …"`, so a truncation reads as one rather than as a sentence that
      stopped mid-word. The suffix is appended AFTER the `max_len` clip, so
      the return may be `max_len + 1` characters.
    * Over the cap, no boundary, and no whitespace at all in the window →
      `value[:max_len]`, with NO suffix. Deliberate: reserving room for a
      suffix inside `max_len` here would cut the run of non-whitespace
      characters short of the cap, which is exactly what
      `test_an_overlong_pitch_is_clipped` exists to catch.
    * A non-string is dropped outright, exactly as `_clip` does.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= max_len:
        return value

    window = value[:max_len]
    half_budget = max_len / 2
    # Any WHITESPACE after the terminator, not just a space. The three-string
    # `rfind` this replaced matched only ". ", "! " and "? ", so a sentence
    # ending at a markdown paragraph break (".\n\n") was invisible to it — and
    # `elevator_pitch` is contractually markdown with "short paragraphs
    # separated by a blank line". That collided head-on with scout_hub 1.7.0,
    # which moved the citation from sentence two to sentence four and bounds
    # sentences 1-4 to end within ~550 so the citation lands inside this
    # window: measured, a citation sentence ending at offset 545 followed by a
    # blank line was dropped and the excerpt cut at 391. The budget is the
    # whole mechanism protecting the public excerpt's provenance, so a
    # terminator the scan cannot see silently defeats it.
    # Cut right after the punctuation mark itself, not the trailing
    # whitespace, so the result reads as a complete sentence.
    boundary_ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", window)]
    # A terminator sitting at the very END of the window counts only when the
    # NEXT character is whitespace (or there is no next character). Without
    # that guard this candidate is always `max_len` — by construction the
    # largest, so it beats every real boundary — and a pitch whose 600th
    # character happens to be the "." of "2.5-fold" or the "." of "et al."
    # publishes "… at 2." or "… (Smith et al." to a channel the post cannot
    # be retracted from.
    if window and window[-1] in ".!?" and (
        len(value) == max_len or value[max_len].isspace()
    ):
        boundary_ends.append(len(window))

    candidates = [end for end in boundary_ends if end >= half_budget]
    if candidates:
        end = max(candidates)
        # The ellipsis is unconditional on a real truncation, which REVERSES
        # this function's first draft ("a complete sentence needs no
        # ellipsis"). The draft assumed the chosen boundary is always a true
        # sentence end; it is not. `max(candidates)` takes the HIGHEST qualifying
        # index,
        # and the pitch contract now requires a citation sentence (sentence
        # four under scout_hub >= 1.7.0, "DOI or PubMed link"), which is
        # precisely where "et al. ", "e.g. ", "i.e. " and "vs. " live — so
        # the last candidate can be an
        # abbreviation, and a reader would have no way to tell the published
        # fragment from the whole pitch. An abbreviation blocklist would be a
        # guess; saying "there is more" is a fact. Omitted only when the cut
        # lands at the true end of the value, where there is nothing more.
        return value[:end] + ("" if end >= len(value) else " …")

    # Any whitespace, not just a space: a markdown pitch (`prose_format ==
    # 'markdown'`) separates paragraphs with newlines, and a window whose only
    # whitespace is "\n" would otherwise fall through to the mid-word cut this
    # function exists to remove.
    match = _TRAILING_WHITESPACE_RUN.search(window)
    if match is None:
        return value[:max_len]
    return window[: match.start()] + " …"


def render_assessment_headline(
    *,
    pi_label: str,
    project: object,
    recommendation: object,
    scores: object,
    permalink: str | None,
    score: float | None = None,
    band: str | None = None,
    elevator_pitch: object = None,
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
    # Sixth field (2026-09-09), a deliberate widening of design D12's five.
    # OMITTED ENTIRELY when absent, exactly as the band/score segment is for an
    # empty `scores` map — every row written before 0043 has NULL here and is
    # deliberately never backfilled, so a repaired headline for one of those
    # rows must be byte-identical to what this function produced before the
    # widening. Clipped at a sentence boundary, not mid-word: all 8 pitches
    # measured in production (2026-09-14) run well past `PITCH_DISPLAY_CHARS`,
    # so a plain `_clip` here cut every one of them mid-word. `_clip_at_sentence`
    # drops a non-string outright, exactly as `_clip` does, so a model that
    # answers with an object cannot have a Python repr posted to a channel
    # humans read.
    pitch_text = _clip_at_sentence(elevator_pitch, PITCH_DISPLAY_CHARS)
    pitch_part = f"\n{pitch_text}" if pitch_text else ""
    return (
        f":mag: {pi_label} — {project_text} → *{display}*{score_part}{link_part}"
        f"{pitch_part}"
    )
