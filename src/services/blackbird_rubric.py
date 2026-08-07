"""Blackbird's weighted screening rubric (Part C.3 of
data/Blackbird_initial_priorities-criteria_v1.pdf, transcribed in
profiles/private/blackbird.md).

The score is computed here rather than taken from the model's own
``weighted_score`` field: thirteen weights times thirteen 1-5 scores is
precisely the arithmetic an LLM gets plausibly wrong, and the band it lands in
decides whether a proposal advances.

Two failure modes matter enough to call out explicitly:

* NaN and +/-inf must never reach the same clamp as an ordinary out-of-range
  number. Python's min()/max() are order-dependent on NaN (every NaN
  comparison is False), so a naive numeric clamp scores NaN as a perfect 5.0 —
  and a malformed LLM verdict can produce NaN: ``json.loads`` accepts bare
  ``NaN``/``Infinity`` tokens by default. Non-finite values are therefore
  treated as unscorable (count as 0), the same as any other non-numeric value.
* The 2dp rounding of the return value is a display concern only. Rounding
  must never be what decides which side of a ``band()`` threshold a score
  lands on — see ``_round_for_band`` below.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# Percentage weights. Sums to 100. Commercial dimensions (60) were tabulated
# in Part C.3; the four scientific dimensions (40) were added because the
# target-level scientific checklist carried zero weight even though it is
# where BBL's actual rejections land — see the audit referenced in the module
# docstring above.
RUBRIC_WEIGHTS: dict[str, int] = {
    # Commercial — 60. Was 100; compressed to make room for science.
    "differentiation": 15,
    "market_unmet_need": 12,
    "team": 10,
    "external_signals": 8,
    # 10 -> 6: FTO decides 1 of the 15 documented rejections and already
    # carries a gating criterion, a red flag and a dedicated tool. Its old
    # weight double-counted an over-instrumented concern.
    "ip_fto": 6,
    "platform": 4,
    "dev_regulatory_feasibility": 3,
    "workplan_capital_efficiency": 1,
    "exit_thesis": 1,
    # Scientific — 40. New. Before this, the target-level scientific checklist
    # carried ZERO weight, so the modal BBL rejection had nowhere to land.
    "mechanism_validation": 12,   # 5 of 15 rejections
    "toxicity_selectivity": 10,   # 4 of 15
    "experimental_rigor": 10,     # the whole "what we don't want" list
    "chemistry_dc_path": 8,       # 4 of 15
}

_MIN_SCORE = 1
_MAX_SCORE = 5
_TOTAL_WEIGHT = sum(RUBRIC_WEIGHTS.values())

# band()'s decision lines, mirrored here so the display rounding in
# weighted_score() can be checked against them. Keep in sync with band().
_BAND_THRESHOLDS = (3.0, 4.0)

# _round_for_band's up-only correction (see its docstring) relies on every
# threshold sitting exactly on the 0.01 display grid: round(raw, 2) moves a
# value by less than half a grid step, which can never carry it past a
# grid-aligned point in the direction away from ``raw`` — only toward it. If a
# future threshold ever lands off-grid that guarantee breaks silently instead
# of loudly, so assert it here rather than leave a dead "the other direction"
# branch in _round_for_band to cover a case that can only arise if this
# assertion has already started failing.
assert all(round(t, 2) == t for t in _BAND_THRESHOLDS), (
    "_BAND_THRESHOLDS must sit on the 0.01 grid — _round_for_band's "
    "correction only handles rounding crossing a threshold upward; see its "
    "docstring"
)


def weighted_score(scores: dict[str, object] | None) -> float:
    """Weighted mean of the thirteen dimensions, on the same 1-5 scale.

    A dimension that is missing, not a number, or not finite (NaN, +inf,
    -inf) counts as 0 — an unscored dimension must drag the total down, never
    be quietly excluded from the denominator, or a verdict that skipped its
    weakest dimensions would outscore one that answered honestly.

    Keys are matched case- and whitespace-insensitively: a verdict spelling a
    dimension ``"Differentiation"`` instead of ``"differentiation"`` must
    still hit its rubric weight rather than being silently treated as missing
    (and thus scored 0) purely because of casing (Finding A5).
    """
    if not scores:
        return 0.0
    # Normalize once. If two differently-cased keys collapse to the same
    # canonical name (e.g. both "team" and "Team" present), the later one in
    # iteration order wins — an unlikely input, but a deterministic pick beats
    # an arbitrary dict-merge accident.
    normalized = {
        key.strip().lower(): value for key, value in scores.items() if isinstance(key, str)
    }
    total = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        raw = normalized.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if not math.isfinite(value):
            # NaN and +/-inf are unscorable, not "off the scale" — without
            # this explicit check they would sail past the isinstance guard
            # above (isinstance(float("nan"), float) is True) and into the
            # min()/max() clamp below, where NaN comparisons are always False
            # and silently produce a perfect 5.0.
            continue
        total += max(_MIN_SCORE, min(_MAX_SCORE, value)) * weight

    unmatched = sorted(set(normalized) - RUBRIC_WEIGHTS.keys())
    if unmatched:
        # Diagnosable, not fatal: each unmatched key already counts as 0 via
        # the .get(key) miss above (or was never a rubric key to begin with).
        # This just makes a malformed/misspelled verdict findable instead of
        # a silently low score.
        logger.warning(
            "weighted_score: verdict has key(s) not in the thirteen rubric "
            "dimensions, scored as unset: %s", unmatched,
        )
    return _round_for_band(total / _TOTAL_WEIGHT)


def _round_for_band(raw: float) -> float:
    """Round ``raw`` to 2dp without letting the rounding cross a band()
    threshold.

    round(3.995, 2) == 4.0: a true mean of 3.995 is < 4.0 and must band as
    "conditional", but the naively-rounded display value of 4.0 would band as
    "advance". Rounding is for display; which band a score falls in must be
    decided from the true value. If naive rounding would move the value to
    the other side of a threshold from where the true value sits, round
    toward the true value's side instead — still to 2dp, just not to the
    nearest one.

    Only one direction is handled: naive rounding pushing ``raw`` UP across a
    threshold it was truly below (the floor correction). The opposite
    correction (``raw`` truly at/above a threshold, naive rounding pushing it
    below) can only happen if a threshold is off the 0.01 grid — asserted
    impossible at module load, above — so it is not implemented here; adding
    an unreachable branch "just in case" would be dead code with no test able
    to prove it correct.
    """
    rounded = round(raw, 2)
    for threshold in _BAND_THRESHOLDS:
        if raw < threshold <= rounded:
            return round(math.floor(raw * 100) / 100, 2)
    return rounded


def band(score: float) -> str:
    """Part C.3 banding: >=4.0 advance, 3.0-3.9 conditional, <3.0 pass.

    'pass' here means pass ON the deal (decline), matching the PDF's vocabulary —
    not 'passing' the screen.
    """
    if score >= 4.0:
        return "advance"
    if score >= 3.0:
        return "conditional"
    return "pass"
