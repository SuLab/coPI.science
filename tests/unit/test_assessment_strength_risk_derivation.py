"""What `derive_strengths_and_risks` may and may not claim about a stored row.

The brief on the assessment detail page is a READ of four stored things —
dimension scores, `gating`, `red_flags` and the recorded specialist consults.
It invents nothing, stores nothing, and it is deliberately possible for an
input to land in NO bucket at all: a mid-scale dimension score is a real,
neutral answer, and a third bucket ("not established") exists so that "never
asked", "not scored" and "the specialist's reply was cut off" are never
reported as findings somebody made.

Pure unit tests: hand-built `OpportunityAssessment` instances (never flushed to
a database) plus literal `dimensions` / `consults` / `revision` structures of
exactly the shape `build_assessment_detail` passes.
"""

from __future__ import annotations

from src.models import OpportunityAssessment
from src.services.assessment_detail import (
    RISK_THRESHOLD_FRACTION,
    STRENGTH_THRESHOLD_FRACTION,
    derive_strengths_and_risks,
)
from src.services.blackbird_rubric import load_rubric
from src.services.rubric_revisions import (
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
    PROVENANCE_UNSTAMPED,
    RevisionDimension,
    RubricRevisionView,
    live_revision_view,
)


def _revision(scale_max: int = 5) -> RubricRevisionView:
    """A revision carrying only what the derivation reads: its scale."""
    return RubricRevisionView(
        version="test-1.0.0",
        content_hash="abcdef123456",
        scale_min=1,
        scale_max=scale_max,
        advance_min=None,
        conditional_min=None,
        pass_label=None,
        banding_note=None,
        dimensions=(
            RevisionDimension(key="significance", title="Significance", weight=25, weight_note="25%"),
        ),
    )


def _assessment(**kwargs) -> OpportunityAssessment:
    defaults = {
        "company_or_project": "Test idea",
        "recommendation": "conditional",
        "gating": None,
        "red_flags": None,
        "scores": None,
    }
    return OpportunityAssessment(**{**defaults, **kwargs})


def _dimension(key: str, score: object, title: str | None = None) -> dict:
    return {
        "key": key,
        "title": title or key.replace("_", " ").title(),
        "weight": 25,
        "weight_note": "25%",
        "score": score,
        "pct": None,
    }


def _consult(domain: str, signal: object, truncated: bool = False) -> dict:
    return {"domain": domain, "verdict_signal": signal, "reply_truncated": truncated}


#: `revision=None` is a meaningful input (an unknown revision), so the "not
#: passed" default cannot be None and cannot be a call in the signature.
_DEFAULT_REVISION = object()


def _derive(
    assessment=None,
    *,
    dimensions=None,
    consults=None,
    revision=_DEFAULT_REVISION,
    revision_provenance=None,
):
    return derive_strengths_and_risks(
        assessment if assessment is not None else _assessment(),
        dimensions=dimensions if dimensions is not None else [],
        consults=consults if consults is not None else [],
        revision=_revision() if revision is _DEFAULT_REVISION else revision,
        revision_provenance=revision_provenance,
    )


def _sole(entries: list[dict]) -> dict:
    """The single entry in a bucket, asserting there is exactly one."""
    assert len(entries) == 1, entries
    return entries[0]


def _details(entries, source: str) -> list[str]:
    return [e["detail"] for e in entries if e["source"] == source]


# ---------------------------------------------------------------------------
# Shape of the return value
# ---------------------------------------------------------------------------


def test_the_return_shape_is_the_pinned_contract():
    result = _derive()
    assert set(result) == {
        "strengths", "risks", "unestablished", "scale_known",
        "thresholds", "mid_scale_count", "scored_dimension_count",
    }
    assert result["scale_known"] is True
    assert result["strengths"] == result["risks"] == result["unestablished"] == []


def test_every_entry_carries_source_label_and_detail():
    result = _derive(
        _assessment(gating={"life_sciences_domain": "met"}, red_flags=["No IP position"]),
        dimensions=[_dimension("significance", 5)],
        consults=[_consult("clinical", "blocking")],
    )
    entries = result["strengths"] + result["risks"] + result["unestablished"]
    assert entries
    for entry in entries:
        assert set(entry) == {"source", "label", "detail", "body", "preview", "note"}
        assert entry["source"] in {"dimension", "gating", "red_flag", "consult"}
        assert isinstance(entry["label"], str) and entry["label"]
        assert isinstance(entry["detail"], str)


# ---------------------------------------------------------------------------
# Dimension scores
# ---------------------------------------------------------------------------


def test_a_high_dimension_score_is_a_strength():
    result = _derive(dimensions=[_dimension("significance", 5, title="Significance")])
    assert [e["label"] for e in result["strengths"]] == ["Significance"]
    assert _details(result["strengths"], "dimension") == ["scored 5 of 5"]
    assert result["risks"] == []


def test_a_dimension_score_exactly_on_the_strength_threshold_is_a_strength():
    result = _derive(dimensions=[_dimension("significance", 4)])
    assert len(result["strengths"]) == 1
    assert _details(result["strengths"], "dimension") == ["scored 4 of 5"]


def test_a_low_dimension_score_is_a_risk():
    result = _derive(dimensions=[_dimension("innovation", 1, title="Innovation")])
    assert [e["label"] for e in result["risks"]] == ["Innovation"]
    assert _details(result["risks"], "dimension") == ["scored 1 of 5"]
    assert result["strengths"] == []


def test_a_dimension_score_exactly_on_the_risk_threshold_is_a_risk():
    result = _derive(dimensions=[_dimension("innovation", 2)])
    assert _details(result["risks"], "dimension") == ["scored 2 of 5"]


def test_a_mid_scale_dimension_is_in_no_bucket():
    """A 3 of 5 is a real, neutral answer — not a strength, not a risk, and
    emphatically not an unknown."""
    result = _derive(dimensions=[_dimension("significance", 3)])
    assert result["strengths"] == []
    assert result["risks"] == []
    assert result["unestablished"] == []


def test_an_unscored_dimension_is_not_established_rather_than_a_zero():
    """Paired with a SCORED dimension deliberately: "counted as zero in the
    weighted score" is only true when a weighted score exists, and
    `_persist_assessment` leaves that column NULL for a verdict with no scores
    at all. The all-unscored shape is
    `test_a_verdict_with_no_dimension_scores_at_all_does_not_claim_a_zero`."""
    result = _derive(dimensions=[
        _dimension("significance", 5),
        _dimension("commercial_potential", None, title="Commercial"),
    ])
    assert result["risks"] == []
    assert _details(result["unestablished"], "dimension") == [
        "not scored — counted as zero in the weighted score"
    ]


def test_a_fractional_score_renders_without_trailing_zeros():
    result = _derive(dimensions=[_dimension("significance", 4.5)])
    assert _details(result["strengths"], "dimension") == ["scored 4.5 of 5"]


def test_an_unknown_revision_puts_no_dimension_in_any_bucket_and_says_so():
    """No scale, no threshold. Guessing one would relabel every row scored on
    another scale — the render-time re-derivation `panel_owed` exists to end."""
    result = _derive(
        dimensions=[_dimension("significance", 5), _dimension("innovation", None)],
        revision=None,
    )
    assert result["scale_known"] is False
    assert result["strengths"] == []
    assert result["risks"] == []
    assert result["unestablished"] == []


def test_thresholds_are_read_from_the_revision_scale():
    """A synthetic 1-10 revision: 8 is a strength and 4 a risk, which no
    literal 4-and-2 implementation can produce."""
    result = _derive(
        dimensions=[
            _dimension("significance", 8, title="Significance"),
            _dimension("innovation", 4, title="Innovation"),
            _dimension("commercial_potential", 6, title="Commercial"),
        ],
        revision=_revision(scale_max=10),
    )
    assert [e["label"] for e in result["strengths"]] == ["Significance"]
    assert _details(result["strengths"], "dimension") == ["scored 8 of 10"]
    assert [e["label"] for e in result["risks"]] == ["Innovation"]
    assert result["unestablished"] == []
    assert STRENGTH_THRESHOLD_FRACTION == 0.8
    assert RISK_THRESHOLD_FRACTION == 0.4


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_a_met_gate_is_a_strength():
    result = _derive(_assessment(gating={"life_sciences_domain": "met"}))
    assert result["strengths"] == [
        {"source": "gating", "label": "life sciences domain", "detail": "met", "body": [], "preview": None, "note": None}
    ]


def test_an_unmet_gate_is_a_risk():
    result = _derive(_assessment(gating={"credible_science": "not_met"}))
    assert result["risks"] == [
        {"source": "gating", "label": "credible science", "detail": "not met", "body": [], "preview": None, "note": None}
    ]


def test_an_unconfirmed_gate_was_never_asked():
    result = _derive(_assessment(gating={"translational_potential": "unconfirmed"}))
    assert result["unestablished"] == [
        {"source": "gating", "label": "translational potential", "detail": "never asked", "body": [], "preview": None, "note": None}
    ]
    assert result["strengths"] == []
    assert result["risks"] == []


def test_an_unrecognised_gating_value_is_not_established():
    """`gating` is JSONB with no CHECK behind it, so a fourth value is
    possible — and it is neither a pass nor a failure."""
    result = _derive(_assessment(gating={"credible_science": True}))
    assert _details(result["unestablished"], "gating") == ["unrecognised gating value"]
    assert result["strengths"] == []
    assert result["risks"] == []


# ---------------------------------------------------------------------------
# Red flags
# ---------------------------------------------------------------------------


def test_every_red_flag_is_a_risk_carrying_its_full_text():
    flags = ["No freedom to operate on the core construct", "Single-source reagent"]
    result = _derive(_assessment(red_flags=flags))
    assert [e["source"] for e in result["risks"]] == ["red_flag", "red_flag"]
    assert [e["label"] for e in result["risks"]] == ["Red flag", "Red flag"]
    assert [e["detail"] for e in result["risks"]] == flags


# ---------------------------------------------------------------------------
# Consults
# ---------------------------------------------------------------------------


def test_an_adequate_consult_is_a_strength():
    result = _derive(consults=[_consult("clinical", "adequate")])
    assert result["strengths"] == [
        {"source": "consult", "label": "clinical", "detail": "adequate", "body": [], "preview": None, "note": None}
    ]


def test_the_historical_clear_signal_is_still_read_as_a_strength():
    result = _derive(consults=[_consult("regulatory", "clear")])
    assert _details(result["strengths"], "consult") == ["clear"]


def test_a_blocking_consult_is_a_risk():
    result = _derive(consults=[_consult("ip", "blocking")])
    assert result["risks"] == [
        {"source": "consult", "label": "ip", "detail": "blocking", "body": [], "preview": None, "note": None}
    ]


def test_a_gap_consult_is_a_risk():
    result = _derive(consults=[_consult("market", "gap")])
    assert _details(result["risks"], "consult") == ["gap"]


def test_the_historical_caution_signal_is_still_read_as_a_risk():
    result = _derive(consults=[_consult("market", "caution")])
    assert _details(result["risks"], "consult") == ["caution"]


def test_a_truncated_consult_is_never_a_strength_or_a_risk():
    """A truncated reply's `verdict_signal` is `specialists.py`'s parse default
    (`gap`), not anything a specialist said — so the signal is unusable however
    confident it looks."""
    result = _derive(
        consults=[
            _consult("ip", "gap", truncated=True),
            _consult("clinical", "adequate", truncated=True),
        ]
    )
    assert result["strengths"] == []
    assert result["risks"] == []
    assert _details(result["unestablished"], "consult") == [
        "reply cut off — no signal",
        "reply cut off — no signal",
    ]


def test_an_unrecognised_consult_signal_lands_in_the_third_bucket():
    """`specialist_consults.verdict_signal` is a plain stored column with no
    CHECK constraint, so falling off the end of the branch silently would be
    the S1 defect in miniature."""
    result = _derive(consults=[_consult("ip", None), _consult("market", "clear-ish")])
    assert result["strengths"] == []
    assert result["risks"] == []
    assert _details(result["unestablished"], "consult") == [
        "signal not recognised — nothing can be said about this consult",
        "signal not recognised — nothing can be said about this consult",
    ]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_it_never_raises_on_malformed_stored_values():
    """A brief card must never 500 a page. `bool` subclasses `int`, so a True
    in `scores` must not be reported as a real score of one."""
    result = _derive(
        _assessment(
            gating={"credible_science": "maybe", "life_sciences_domain": None},
            red_flags=["a real flag", 7, None, {"nested": "thing"}, "   "],
            scores={"significance": True},
        ),
        dimensions=[
            _dimension("significance", True),
            _dimension("innovation", "4"),
            {"key": "orphan"},
        ],
        consults=[{"domain": "ip"}, {}, "not a dict"],
    )
    assert _details(result["risks"], "red_flag") == ["a real flag"]
    assert result["strengths"] == []
    # The bool, the string score and the key-less dimension are all "no usable
    # score", which is the not-scored bucket — never a zero.
    # No dimension here has a usable score, so the detail is the
    # no-score-at-all wording rather than the counted-as-zero claim (there is
    # no weighted score for such a row to have counted anything into).
    assert _details(result["unestablished"], "dimension") == [
        "no dimension scores were recorded for this verdict"
    ] * 3
    assert len(_details(result["unestablished"], "gating")) == 2
    assert len(_details(result["unestablished"], "consult")) == 2


def test_null_gating_and_null_scores_contribute_nothing():
    result = _derive(_assessment(gating=None, red_flags=None, scores=None))
    assert result["strengths"] == []
    assert result["risks"] == []
    assert result["unestablished"] == []


# ---------------------------------------------------------------------------
# 2026-09-14 audit findings
# ---------------------------------------------------------------------------


def test_a_defaulted_consult_signal_is_never_a_strength_or_a_risk():
    """`read_state` has THREE values and two of them mean the stored
    `verdict_signal` is not something a specialist said. `truncated` was
    already handled via `reply_truncated`; `defaulted` — a reply that arrived
    COMPLETE but from which `parse_opinion` could read no signal, so
    `_DEFAULT_SIGNAL` (`gap`) was substituted — carries `truncated=False` and
    would otherwise render under a red glyph as a finding nobody made. That is
    exactly what point 1 of the function's contract forbids."""
    result = derive_strengths_and_risks(
        _assessment(),
        dimensions=[],
        consults=[{
            "domain": "legal",
            "verdict_signal": "gap",
            "reply_truncated": False,
            "read_state": "defaulted",
        }],
        revision=None,
    )
    assert result["risks"] == []
    assert result["strengths"] == []
    entry = _sole(result["unestablished"])
    assert entry["source"] == "consult"
    assert entry["label"] == "legal"
    assert "no signal" in entry["detail"]


def test_a_pre_0038_consult_with_no_read_state_stays_on_the_signal_path():
    """`read_state is None` is a row written before migration 0038: the
    question was never recorded, so it must NOT be read as "defaulted". The
    stored signal is the only answer available for it."""
    result = derive_strengths_and_risks(
        _assessment(),
        dimensions=[],
        consults=[{
            "domain": "legal",
            "verdict_signal": "blocking",
            "reply_truncated": False,
            "read_state": None,
        }],
        revision=None,
    )
    assert _sole(result["risks"])["detail"] == "blocking"


def test_a_verdict_with_no_dimension_scores_at_all_does_not_claim_a_zero():
    """`_persist_assessment` writes `scores or None` alongside a NULL
    `weighted_score`/`band`, so for such a row "counted as zero in the
    weighted score" would assert six times over about a number that was never
    computed."""
    revision = _revision(scale_max=5)
    dimensions = [
        {"key": f"d{i}", "title": f"D{i}", "weight": 10, "weight_note": "10%",
         "score": None, "pct": 0.0}
        for i in range(3)
    ]
    result = derive_strengths_and_risks(
        _assessment(), dimensions=dimensions, consults=[], revision=revision,
    )
    details = {e["detail"] for e in result["unestablished"]}
    assert details == {"no dimension scores were recorded for this verdict"}


def test_a_partly_scored_verdict_still_says_counted_as_zero():
    """The other side of the same branch: when SOME dimension is scored a
    weighted score does exist, so an unscored one really was counted as zero."""
    revision = _revision(scale_max=5)
    dimensions = [
        {"key": "a", "title": "A", "weight": 10, "weight_note": "10%",
         "score": 5.0, "pct": 100.0},
        {"key": "b", "title": "B", "weight": 10, "weight_note": "10%",
         "score": None, "pct": 0.0},
    ]
    result = derive_strengths_and_risks(
        _assessment(), dimensions=dimensions, consults=[], revision=revision,
    )
    assert _sole(result["unestablished"])["detail"] == (
        "not scored — counted as zero in the weighted score"
    )


# ---------------------------------------------------------------------------
# `body`: quoted text carried alongside an entry
# ---------------------------------------------------------------------------


def test_a_consult_strength_with_established_evidence_carries_it_as_body():
    result = _derive(consults=[{
        "domain": "clinical",
        "verdict_signal": "adequate",
        "reply_truncated": False,
        "established": ["Strong prior data", "Validated in two models"],
    }])
    entry = _sole(result["strengths"])
    assert entry["body"] == ["Strong prior data", "Validated in two models"]


def test_a_consult_strength_with_empty_established_carries_no_body():
    result = _derive(consults=[{
        "domain": "clinical",
        "verdict_signal": "adequate",
        "reply_truncated": False,
        "established": [],
    }])
    assert _sole(result["strengths"])["body"] == []


def test_a_consult_strength_with_null_established_carries_no_body():
    result = _derive(consults=[{
        "domain": "clinical",
        "verdict_signal": "adequate",
        "reply_truncated": False,
        "established": None,
    }])
    assert _sole(result["strengths"])["body"] == []


def test_a_consult_risk_with_five_concerns_caps_at_three_plus_a_tally():
    result = _derive(consults=[{
        "domain": "ip",
        "verdict_signal": "blocking",
        "reply_truncated": False,
        "concerns": ["c1", "c2", "c3", "c4", "c5"],
    }])
    assert _sole(result["risks"])["body"] == ["c1", "c2", "c3", "and 2 more"]


def test_a_consult_risk_with_no_concerns_key_carries_no_body():
    result = _derive(consults=[_consult("market", "gap")])
    assert _sole(result["risks"])["body"] == []


def test_a_gate_carries_the_live_title_and_description_when_provenance_is_live():
    live = live_revision_view()
    rubric = load_rubric()
    key = next(iter(rubric.gating))
    result = _derive(
        _assessment(gating={key: "met"}),
        revision=live,
        revision_provenance=PROVENANCE_LIVE,
    )
    entry = _sole(result["strengths"])
    assert entry["label"] == rubric.gating[key]["title"]
    assert entry["body"] == [rubric.gating[key]["description"]]


def test_a_gate_stays_bare_when_provenance_is_unknown():
    live = live_revision_view()
    rubric = load_rubric()
    key = next(iter(rubric.gating))
    result = _derive(
        _assessment(gating={key: "met"}),
        revision=live,
        revision_provenance=PROVENANCE_UNKNOWN,
    )
    entry = _sole(result["strengths"])
    assert entry["label"] == key.replace("_", " ")
    assert entry["body"] == []


def test_a_gate_stays_bare_when_provenance_is_unstamped():
    live = live_revision_view()
    rubric = load_rubric()
    key = next(iter(rubric.gating))
    result = _derive(
        _assessment(gating={key: "met"}),
        revision=live,
        revision_provenance=PROVENANCE_UNSTAMPED,
    )
    entry = _sole(result["strengths"])
    assert entry["label"] == key.replace("_", " ")
    assert entry["body"] == []


def test_a_dimension_body_carries_the_weight_note_verbatim_including_dual_scale():
    result = _derive(dimensions=[{
        "key": "significance", "title": "Significance", "weight": 6,
        "weight_note": "6%/4% (investment/incubation)", "score": 5, "pct": 100.0,
    }])
    entry = _sole(result["strengths"])
    assert entry["body"] == ["weight: 6%/4% (investment/incubation)"]


def test_a_dimension_body_is_absent_when_weight_is_none():
    result = _derive(dimensions=[{
        "key": "extra", "title": "Extra", "weight": None,
        "weight_note": None, "score": 5, "pct": 100.0,
    }])
    entry = _sole(result["strengths"])
    assert entry["body"] == []


def test_mid_scale_count_counts_only_scored_dimensions_strictly_between_thresholds():
    result = _derive(dimensions=[
        _dimension("significance", 5),  # strength
        _dimension("innovation", 1),  # risk
        _dimension("commercial_potential", 3),  # mid-scale
        _dimension("clinical_actionability", None),  # not scored, excluded
    ])
    assert result["mid_scale_count"] == 1


def test_thresholds_are_all_none_when_the_scale_is_unknown():
    result = _derive(dimensions=[_dimension("significance", 5)], revision=None)
    assert result["thresholds"] == {"strength": None, "risk": None, "scale_max": None}
    assert result["mid_scale_count"] == 0



# ---------------------------------------------------------------------------
# Compaction (2026-09-14): one entry per domain, previews, clipped red flags
# ---------------------------------------------------------------------------


def test_consults_collapse_to_one_entry_per_domain_from_the_latest():
    """37 consults across 8 domains must not be 37 bullets. The LATEST consult
    in a domain decides the bucket and supplies the quotes; the count is a
    `note`, never a bullet, so it cannot read as specialist text."""
    consults = [
        {**_consult("clinical", "adequate"), "established": ["EARLY"]},
        {**_consult("clinical", "gap"), "concerns": ["LATE ONE.", "LATE TWO."]},
        {**_consult("legal", "gap"), "concerns": ["ONLY"]},
    ]
    result = _derive(consults=consults)
    assert result["strengths"] == []
    labels = [(e["label"], e["detail"], e["note"]) for e in result["risks"]]
    assert labels == [
        ("clinical", "gap", "latest of 2 consults"),
        ("legal", "gap", None),
    ]
    assert result["risks"][0]["body"] == ["LATE ONE.", "LATE TWO."]
    assert "EARLY" not in str(result)


def test_a_truncated_latest_consult_moves_its_domain_to_not_established_with_the_count():
    consults = [_consult("ip", "adequate"), _consult("ip", "gap", truncated=True)]
    result = _derive(consults=consults)
    assert result["strengths"] == [] and result["risks"] == []
    entry = _sole(result["unestablished"])
    assert (entry["label"], entry["note"]) == ("ip", "latest of 2 consults")


def test_the_preview_is_the_first_quotes_first_sentence_clipped():
    long = "First sentence here. " + "Second sentence that goes on and on. " * 12
    result = _derive(consults=[{**_consult("clinical", "gap"), "concerns": [long]}])
    entry = _sole(result["risks"])
    assert entry["preview"] is not None
    assert entry["preview"].startswith("First sentence here.")
    assert len(entry["preview"]) <= 160 + 2  # `_clip_at_sentence` may append " …"
    assert entry["body"] == [long]


def test_an_entry_with_no_quotes_has_no_preview():
    result = _derive(consults=[_consult("clinical", "gap")])
    entry = _sole(result["risks"])
    assert entry["preview"] is None and entry["body"] == []


def test_a_long_red_flag_is_summarised_to_its_first_sentence_with_the_full_text_behind_it():
    flag = "Freedom to operate is blocked. " + "There is a dominating patent family " * 10
    result = _derive(_assessment(red_flags=[flag]))
    entry = _sole(result["risks"])
    assert entry["detail"].startswith("Freedom to operate is blocked.")
    assert len(entry["detail"]) < len(flag)
    assert entry["body"] == [flag.strip()]


def test_a_short_red_flag_stays_a_plain_bullet_with_no_body():
    result = _derive(_assessment(red_flags=["No IP position"]))
    entry = _sole(result["risks"])
    assert entry["detail"] == "No IP position"
    assert entry["body"] == []


def test_gate_and_dimension_bodies_carry_no_preview():
    from src.services.rubric_revisions import PROVENANCE_LIVE, live_revision_view

    live = live_revision_view()
    dim = live.dimensions[0]
    result = _derive(
        _assessment(gating={next(iter(load_rubric().gating)): "met"}),
        dimensions=[{"key": dim.key, "title": dim.title, "weight": dim.weight,
                     "weight_note": dim.weight_note, "score": live.scale_max}],
        revision=live,
        revision_provenance=PROVENANCE_LIVE,
    )
    assert all(e["preview"] is None for e in result["strengths"])
    assert all(e["body"] for e in result["strengths"])
