"""S4 scoring, the S5 order, and the epoch rubric's weighted score and band (spec §7.5, §7.6,
§7.11, L24, L56, L62)."""
import dataclasses
import math
import random
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from src.landscaping.criteria import load_criteria
from src.landscaping.scoring import (
    checklist_score,
    dimension_mean,
    epoch_band,
    epoch_weighted_score,
    excluded_reason,
    present_items,
    tie_key,
    triage_score,
)
from src.services import blackbird_rubric
from src.services.blackbird_rubric import load_rubric

GATES = ("life_sciences_domain", "credible_science", "translational_potential")


def _checklist(present: int, total: int = 11) -> list[dict]:
    return [
        {"item": n, "status": "present" if n <= present else "absent"}
        for n in range(1, total + 1)
    ]


def _result(**changes) -> dict:
    result = {
        "gating": {key: "met" for key in GATES},
        "in_therapeutic_scope": True,
        "llm_flags": {},
    }
    result.update(changes)
    return result


def test_triage_score_formula():
    rubric = load_rubric()
    scores = {
        "differentiation_unmet_need": 5, "scientific_credibility": 4, "translational_path": 3,
    }
    # The three dimensions carry equal weights in the rubric, so the mean is 4.
    assert dimension_mean(scores, rubric) == pytest.approx(4.0)
    assert checklist_score(_checklist(11), rubric=rubric) == pytest.approx(5.0)
    criteria = SimpleNamespace(dimension_share=0.8)
    assert triage_score(
        scores, _checklist(11), rubric=rubric, criteria=criteria
    ) == pytest.approx(0.8 * 4 + 0.2 * 5)
    assert triage_score(
        scores, _checklist(11), rubric=rubric, criteria=criteria
    ) == pytest.approx(4.2)


def test_gate_not_met_excludes():
    gating = {key: "met" for key in GATES}
    gating["credible_science"] = "not_met"
    assert excluded_reason(_result(gating=gating)) == "excluded:gate"
    assert excluded_reason(_result(in_therapeutic_scope=False)) == "excluded:scope"


def test_unconfirmed_passes():
    assert excluded_reason(_result(gating={key: "unconfirmed" for key in GATES})) is None


def test_flags_never_exclude():
    raised = {"raised": True, "reason": "An invented concern."}
    result = _result(
        llm_flags={
            "correlation_only": raised,
            "model_unrepresentative_no_human_data": raised,
            "key_molecule_owned_elsewhere": raised,
            "literature_contradiction": raised,
        },
        red_flags=["An invented red flag."],
        data_flags={"target_essential": {"raised": True}, "safety_liabilities": {"raised": True}},
    )
    assert excluded_reason(result) is None


def test_tie_key_stage_earlier_first():
    shared = {
        "triage_score": 3.8, "evidence_count": 6, "evidence_grade": "in_vivo",
        "jhu_group_count": 2, "document_count": 1, "newest_document_date": date(2026, 1, 5),
    }
    early = {**shared, "stage": "target_validation", "idea_id": uuid.UUID(int=2)}
    late = {**shared, "stage": "lead", "idea_id": uuid.UUID(int=1)}
    criteria = load_criteria()
    assert sorted([late, early], key=lambda r: tie_key(r, criteria=criteria)) == [early, late]
    # Stage is key 5: the JHU group count (key 4) still outranks it.
    more_groups = {**late, "jhu_group_count": 3}
    assert sorted(
        [early, more_groups], key=lambda r: tie_key(r, criteria=criteria)
    ) == [more_groups, early]


def test_tie_key_follows_the_ladders_of_the_criteria_it_is_given():
    shared = {
        "triage_score": 3.8, "evidence_count": 6, "jhu_group_count": 2, "document_count": 1,
        "newest_document_date": date(2026, 1, 5),
    }
    first = {**shared, "evidence_grade": "invented_grade_a", "stage": "invented_stage_x",
             "idea_id": uuid.UUID(int=1)}
    second = {**shared, "evidence_grade": "invented_grade_b", "stage": "invented_stage_x",
              "idea_id": uuid.UUID(int=2)}
    # Grades are ranked weakest first: the later grade in the ladder ranks higher.
    a_strong = SimpleNamespace(evidence_grades=("invented_grade_b", "invented_grade_a"),
                               stages=("invented_stage_x",))
    b_strong = SimpleNamespace(evidence_grades=("invented_grade_a", "invented_grade_b"),
                               stages=("invented_stage_x",))
    assert sorted([second, first], key=lambda r: tie_key(r, criteria=a_strong)) == [first, second]
    assert sorted([first, second], key=lambda r: tie_key(r, criteria=b_strong)) == [second, first]

    same_grade = {**second, "evidence_grade": "invented_grade_a", "stage": "invented_stage_y"}
    x_first = SimpleNamespace(evidence_grades=("invented_grade_a",),
                              stages=("invented_stage_x", "invented_stage_y"))
    y_first = SimpleNamespace(evidence_grades=("invented_grade_a",),
                              stages=("invented_stage_y", "invented_stage_x"))
    assert sorted([same_grade, first], key=lambda r: tie_key(r, criteria=x_first)) == [
        first, same_grade,
    ]
    assert sorted([first, same_grade], key=lambda r: tie_key(r, criteria=y_first)) == [
        same_grade, first,
    ]


def test_checklist_counts_the_items_of_the_rubric_it_is_given():
    rubric = load_rubric()
    items = sum(len(d.evidence) for d in rubric.dimensions)
    holder = next(d for d in rubric.dimensions if d.evidence)
    longer = dataclasses.replace(rubric, dimensions=tuple(
        dataclasses.replace(d, evidence=(*d.evidence, "An invented evidence item."))
        if d is holder else d
        for d in rubric.dimensions
    ))
    full = _checklist(items + 1, total=items + 1)

    # Item N+1 lies outside the N-item checklist, so it is ignored there.
    assert present_items(full, rubric=rubric) == items
    assert present_items(full, rubric=longer) == items + 1
    assert checklist_score(full, rubric=rubric) == pytest.approx(5.0)
    assert checklist_score(full, rubric=longer) == pytest.approx(5.0)
    half = _checklist(items // 2, total=items + 1)
    assert checklist_score(half, rubric=rubric) == pytest.approx(1 + 4 * (items // 2) / items)
    assert checklist_score(half, rubric=longer) == pytest.approx(
        1 + 4 * (items // 2) / (items + 1)
    )


def _random_value(rng: random.Random):
    """A score as a verdict might carry it: on or off the scale, fractional, non-finite, or
    not a number at all."""
    return rng.choice([
        lambda: rng.randint(1, 5),
        lambda: rng.randint(-2, 8),
        lambda: round(rng.uniform(0.5, 5.5), rng.choice([1, 2, 3])),
        lambda: rng.choice([math.nan, math.inf, -math.inf]),
        lambda: rng.choice([None, "4", True, False, [3]]),
    ])()


def _random_scores(rng: random.Random, keys: list[str]) -> dict | None:
    if rng.random() < 0.05:
        return rng.choice([None, {}])
    scores: dict = {}
    for key in keys:
        if rng.random() < 0.15:
            continue  # missing
        spelled = rng.choice([key, key.upper(), key.title(), f"  {key} "])
        scores[spelled] = _random_value(rng)
    if rng.random() < 0.2:
        scores["invented_extra_dimension"] = rng.randint(1, 5)
    return scores


def test_epoch_weighted_score_matches_the_loaded_rubric():
    rubric = load_rubric()
    keys = [d.key for d in rubric.dimensions]
    rng = random.Random(20261030)
    for _ in range(2000):
        scores = _random_scores(rng, keys)
        assert epoch_weighted_score(rubric, scores) == blackbird_rubric.weighted_score(scores), (
            scores
        )
    # Means just under a band threshold must not round across it.
    for threshold in (rubric.advance_min, rubric.conditional_min):
        for delta in (0.001, 0.004, 0.005, 0.006):
            scores = {key: threshold - delta for key in keys}
            assert epoch_weighted_score(rubric, scores) == blackbird_rubric.weighted_score(scores)

    # The score follows the rubric it is given, not the loaded one.
    first = rubric.dimensions[0]
    reweighted = dataclasses.replace(rubric, dimensions=(
        dataclasses.replace(first, weight=first.weight * 10), *rubric.dimensions[1:]
    ))
    lopsided = {key: 1 for key in keys} | {first.key: 5}
    assert epoch_weighted_score(reweighted, lopsided) > blackbird_rubric.weighted_score(lopsided)


def test_epoch_band_matches_band():
    rubric = load_rubric()
    for hundredths in range(0, 601):
        score = hundredths / 100
        assert epoch_band(rubric, score) == blackbird_rubric.band(score), score
    for score in (rubric.advance_min, rubric.conditional_min, math.nextafter(rubric.advance_min, 0)):
        assert epoch_band(rubric, score) == blackbird_rubric.band(score)

    raised = dataclasses.replace(rubric, advance_min=rubric.scale_max + 1)
    assert epoch_band(raised, rubric.advance_min) != blackbird_rubric.band(rubric.advance_min)
