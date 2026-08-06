"""Blackbird's weighted screening rubric (Part C.3 of
data/Blackbird_initial_priorities-criteria_v1.pdf, transcribed in
profiles/private/blackbird.md).

The score is computed here rather than taken from the model's own
``weighted_score`` field: nine weights times nine 1-5 scores is precisely the
arithmetic an LLM gets plausibly wrong, and the band it lands in decides whether
a proposal advances.

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

import math

# Percentage weights, exactly as tabulated in Part C.3. Sums to 100.
RUBRIC_WEIGHTS: dict[str, int] = {
    "differentiation": 20,
    "market_unmet_need": 15,
    "team": 15,
    "external_signals": 15,
    "ip_fto": 10,
    "platform": 8,
    "dev_regulatory_feasibility": 7,
    "workplan_capital_efficiency": 5,
    "exit_thesis": 5,
}

_MIN_SCORE = 1
_MAX_SCORE = 5
_TOTAL_WEIGHT = sum(RUBRIC_WEIGHTS.values())

# band()'s decision lines, mirrored here so the display rounding in
# weighted_score() can be checked against them. Keep in sync with band().
_BAND_THRESHOLDS = (3.0, 4.0)


def weighted_score(scores: dict[str, object] | None) -> float:
    """Weighted mean of the nine dimensions, on the same 1-5 scale.

    A dimension that is missing, not a number, or not finite (NaN, +inf,
    -inf) counts as 0 — an unscored dimension must drag the total down, never
    be quietly excluded from the denominator, or a verdict that skipped its
    weakest dimensions would outscore one that answered honestly.
    """
    if not scores:
        return 0.0
    total = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        raw = scores.get(key)
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
    """
    rounded = round(raw, 2)
    for threshold in _BAND_THRESHOLDS:
        if raw < threshold <= rounded:
            return round(math.floor(raw * 100) / 100, 2)
        if rounded < threshold <= raw:
            return round(math.ceil(raw * 100) / 100, 2)
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
