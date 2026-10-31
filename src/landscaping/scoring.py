"""S4 scoring, computed in code from the triage result (spec §7.5), the S5 order (§7.6), and
the six-dimension weighted score and band under an epoch's own rubric (S6, S9; spec §7.11).

``weighted_score`` in ``src/services/blackbird_rubric.py`` is not used or changed: triage
scores three of the rubric's six dimensions and blends in the evidence checklist (L62). It reads
the process's loaded rubric, so S6 and S9 use ``epoch_weighted_score`` and ``epoch_band``, which
compute the same values from the rubric an epoch stored.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from src.services.bands import BANDS, band_for
from src.services.blackbird_rubric import _round_for_band

logger = logging.getLogger(__name__)

#: The rubric dimensions S4 scores (spec §7.5); the other three wait for S6.
TRIAGE_DIMENSIONS = (
    "differentiation_unmet_need", "scientific_credibility", "translational_path",
)


def _checklist_items(rubric) -> int:
    """The rubric's evidence checklist size: its Target Rubric items across its dimensions,
    counted from the rubric itself so the checklist score follows the epoch's document."""
    return sum(len(d.evidence) for d in rubric.dimensions)


def dimension_mean(scores: dict[str, int], rubric) -> float:
    """``Σ wᵢ·sᵢ / Σ wᵢ`` over ``TRIAGE_DIMENSIONS``, with ``rubric``'s weights (spec §7.5).

    ``rubric`` is a parsed rubric (``load_rubric()``). Raises ``ValueError`` when a scored
    dimension is missing from ``scores`` or from the rubric.
    """
    weights = {d.key: d.weight for d in rubric.dimensions}
    missing = [k for k in TRIAGE_DIMENSIONS if k not in scores or k not in weights]
    if missing:
        raise ValueError(f"triage scores or rubric weights missing for {missing}")
    total = sum(weights[k] for k in TRIAGE_DIMENSIONS)
    return sum(weights[k] * scores[k] for k in TRIAGE_DIMENSIONS) / total


def present_items(target_rubric: list[dict], *, rubric) -> int:
    """The number of distinct checklist items marked ``present`` (the stored
    ``evidence_count``, spec §7.5). Entries outside ``rubric``'s item numbers are ignored."""
    items = _checklist_items(rubric)
    return len({
        entry.get("item")
        for entry in target_rubric or ()
        if isinstance(entry, dict)
        and entry.get("status") == "present"
        and isinstance(entry.get("item"), int)
        and 1 <= entry["item"] <= items
    })


def checklist_score(target_rubric: list[dict], *, rubric) -> float:
    """``1 + 4 × present ÷ items`` over ``rubric``'s checklist: the checklist on the
    dimensions' 1-5 scale (spec §7.5)."""
    return 1 + 4 * present_items(target_rubric, rubric=rubric) / _checklist_items(rubric)


def triage_score(scores, target_rubric, *, rubric, criteria) -> float:
    """``α · dimension_mean + (1 − α) · checklist_score``, α = ``criteria.dimension_share``
    (``[triage] dimension_share`` in the criteria file, L62)."""
    alpha = criteria.dimension_share
    return alpha * dimension_mean(scores, rubric) + (1 - alpha) * checklist_score(
        target_rubric, rubric=rubric
    )


def excluded_reason(result: dict) -> str | None:
    """``"excluded:gate"`` when any gate is ``not_met``, else ``"excluded:scope"`` when
    ``in_therapeutic_scope`` is false, else None (spec §7.5). ``unconfirmed`` passes, and
    neither data flags nor ``llm_flags`` ever exclude (L24)."""
    gating = result.get("gating") or {}
    if any(value == "not_met" for value in gating.values()):
        return "excluded:gate"
    if result.get("in_therapeutic_scope") is False:
        return "excluded:scope"
    return None


@lru_cache(maxsize=8)
def _ladders(
    evidence_grades: tuple[str, ...], stages: tuple[str, ...]
) -> tuple[dict[str, int], dict[str, int]]:
    """Evidence-grade and stage ranks from a criteria file's ladders, cached on the ladders."""
    return (
        {grade: n for n, grade in enumerate(evidence_grades)},
        {stage: n for n, stage in enumerate(stages)},
    )


def _field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _date_rank(value: date | datetime | None) -> int:
    if value is None:
        return 0
    if isinstance(value, datetime):
        value = value.date()
    return -value.toordinal()


def tie_key(idea_row, *, criteria) -> tuple:
    """The S5 sort key, ascending, for spec §7.6 keys 1-8, ranking grades and stages on the
    ladders of ``criteria`` (the epoch's own, §7.11).

    ``idea_row`` is a mapping or object with ``triage_score``, ``evidence_count``,
    ``evidence_grade``, ``jhu_group_count``, ``stage``, ``document_count``,
    ``newest_document_date`` and ``idea_id`` (else ``id``). In order: triage score ↓,
    evidence count ↓, evidence grade ↓ (criteria ladder; unknown ranks lowest), JHU groups ↓
    (the S3 count only, never S6's outside count), stage earlier first (L56; unknown last),
    qualifying documents ↓, newest qualifying document date ↓ (none last), idea id ↑.
    """
    grades, stages = _ladders(criteria.evidence_grades, criteria.stages)
    idea_id = _field(idea_row, "idea_id") or _field(idea_row, "id")
    return (
        -(_field(idea_row, "triage_score") or 0.0),
        -(_field(idea_row, "evidence_count") or 0),
        -grades.get(_field(idea_row, "evidence_grade"), -1),
        -(_field(idea_row, "jhu_group_count") or 0),
        stages.get(_field(idea_row, "stage"), len(stages)),
        -(_field(idea_row, "document_count") or 0),
        _date_rank(_field(idea_row, "newest_document_date")),
        str(idea_id or ""),
    )


def epoch_weighted_score(rubric, scores: dict | None) -> float:
    """``blackbird_rubric.weighted_score`` computed with ``rubric``'s weights, scale and band
    thresholds instead of the process's loaded rubric.

    Keys match case- and whitespace-insensitively; a dimension that is missing, not a number
    (a bool included) or not finite counts as 0; a value off the scale is clamped onto it. The
    mean is rounded to 2dp without crossing a band threshold (``_round_for_band``).
    """
    if not scores:
        return 0.0
    normalized = {
        key.strip().lower(): value for key, value in scores.items() if isinstance(key, str)
    }
    weights = {d.key: d.weight for d in rubric.dimensions}
    low, high = rubric.scale_min, rubric.scale_max
    total = 0.0
    clamped: dict[str, float] = {}
    for key, weight in weights.items():
        raw = normalized.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if not math.isfinite(value):
            continue
        if not low <= value <= high:
            clamped[key] = value
        total += max(low, min(high, value)) * weight
    if clamped:
        logger.warning("epoch_weighted_score: score(s) outside the %s-%s scale were clamped "
                       "into range: %s", low, high, clamped)
    unmatched = sorted(set(normalized) - weights.keys())
    if unmatched:
        logger.warning("epoch_weighted_score: key(s) not in the rubric dimensions, scored as "
                       "unset: %s", unmatched)
    thresholds = tuple(sorted(
        getattr(rubric, b.threshold_key) for b in BANDS if b.threshold_key
    ))
    return _round_for_band(total / sum(weights.values()), thresholds)


def epoch_band(rubric, score: float) -> str:
    """``blackbird_rubric.band`` with ``rubric``'s thresholds."""
    return band_for(
        score, {"advance_min": rubric.advance_min, "conditional_min": rubric.conditional_min}
    )
