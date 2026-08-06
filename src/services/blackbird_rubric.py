"""Blackbird's weighted screening rubric (Part C.3 of
data/Blackbird_initial_priorities-criteria_v1.pdf, transcribed in
profiles/private/blackbird.md).

The score is computed here rather than taken from the model's own
``weighted_score`` field: nine weights times nine 1-5 scores is precisely the
arithmetic an LLM gets plausibly wrong, and the band it lands in decides whether
a proposal advances.
"""

from __future__ import annotations

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


def weighted_score(scores: dict[str, object] | None) -> float:
    """Weighted mean of the nine dimensions, on the same 1-5 scale.

    A dimension that is missing or not a number counts as 0 — an unscored
    dimension must drag the total down, never be quietly excluded from the
    denominator, or a verdict that skipped its weakest dimensions would outscore
    one that answered honestly.
    """
    if not scores:
        return 0.0
    total = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        raw = scores.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        total += max(_MIN_SCORE, min(_MAX_SCORE, float(raw))) * weight
    return round(total / sum(RUBRIC_WEIGHTS.values()), 2)


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
