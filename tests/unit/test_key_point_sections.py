"""Key-point groups: the current six (scout_hub >= 1.8.0), the legacy five
(1.3.0–1.7.1, display-only) and the one read helper every surface uses."""
from src.services.assessment_detail import (
    KEY_POINT_ACCEPTED_KEYS,
    KEY_POINT_GROUPS,
    LEGACY_KEY_POINT_GROUPS,
    key_point_sections,
    key_point_shape,
    normalize_key_points,
)

CURRENT = [k for k, _ in KEY_POINT_GROUPS]
LEGACY = [k for k, _ in LEGACY_KEY_POINT_GROUPS]


def test_the_current_and_legacy_sets_are_the_reviewers_and_the_old_ones():
    assert KEY_POINT_GROUPS == (
        ("indication_audience", "Indication / Audience"),
        ("lab_background", "Lab Background"),
        ("proposal", "Proposal"),
        ("clinical_actionability", "Clinical Actionability"),
        ("key_questions", "Key Questions/Experiment"),
        ("commercial_opportunity", "Commercial Opportunity"),
    )
    assert LEGACY_KEY_POINT_GROUPS == (
        ("significance", "Significance"),
        ("innovation", "Innovation"),
        ("clinical_actionability", "Clinical actionability"),
        ("key_questions", "Key questions / experiments"),
        ("commercial_potential", "Commercial potential"),
    )
    assert KEY_POINT_ACCEPTED_KEYS == frozenset(CURRENT) | frozenset(LEGACY)


def test_shape_classification():
    assert key_point_shape(["a"]) == "flat"
    assert key_point_shape({"significance": ["s"]}) == "legacy"
    assert key_point_shape({"lab_background": ["l"]}) == "current"
    assert key_point_shape({"key_questions": ["q"]}) == "current"   # shared key only
    assert key_point_shape({"significance": ["s"], "proposal": ["p"]}) == "mixed"
    assert key_point_shape({}) is None
    assert key_point_shape(None) is None
    assert key_point_shape("text") is None


def test_a_six_group_row_renders_in_the_new_order_under_the_new_labels():
    value = {k: [f"{k} point"] for k in reversed(CURRENT)}   # seeded out of order
    sections = key_point_sections(value)
    assert [label for label, _ in sections] == [label for _, label in KEY_POINT_GROUPS]
    assert sections[0] == ("Indication / Audience", ["indication_audience point"])


def test_a_five_group_legacy_row_keeps_its_own_labels_and_order():
    value = {k: [f"{k} point"] for k in reversed(LEGACY)}
    sections = key_point_sections(value)
    assert [label for label, _ in sections] == [label for _, label in LEGACY_KEY_POINT_GROUPS]


def test_a_three_group_1_3_0_row_keeps_its_own_labels_and_order():
    value = {"commercial_potential": ["c"], "significance": ["s"], "innovation": ["i"]}
    assert key_point_sections(value) == [
        ("Significance", ["s"]), ("Innovation", ["i"]), ("Commercial potential", ["c"]),
    ]


def test_a_mixed_row_hides_nothing():
    value = {"significance": ["s"], "proposal": ["p"], "key_questions": ["q"]}
    assert key_point_sections(value) == [
        ("Proposal", ["p"]), ("Key Questions/Experiment", ["q"]), ("Significance", ["s"]),
    ]


def test_empty_blank_unknown_and_non_list_groups_render_nothing():
    assert key_point_sections({"significance": [], "innovation": []}) == []
    assert key_point_sections({"proposal": ["  ", ""]}) == []
    assert key_point_sections({"not_a_group": ["x"]}) == []
    assert key_point_sections({"proposal": "not a list"}) == []
    assert key_point_sections({"proposal": [" p ", "", None, 3]}) == [("Proposal", ["p"])]
    assert key_point_sections(None) == []
    assert key_point_sections([]) == []


def test_a_flat_list_is_one_unlabelled_section():
    assert key_point_sections([" a ", "", "b"]) == [(None, ["a", "b"])]


def test_normalize_accepts_current_legacy_and_mixed_and_strips_blanks():
    six = {k: ["x"] for k in CURRENT}
    five = {k: ["x"] for k in LEGACY}
    assert normalize_key_points(six) == six
    assert normalize_key_points(five) == five
    assert normalize_key_points({"significance": ["s"], "proposal": ["p"]}) == {
        "significance": ["s"], "proposal": ["p"],
    }
    assert normalize_key_points({"proposal": [" p ", "  "]}) == {"proposal": ["p"]}
    assert list(normalize_key_points(dict(reversed(list(six.items()))))) == list(reversed(CURRENT))


def test_normalize_still_rejects_unknown_keys_and_wrong_types():
    assert normalize_key_points({"proposal": ["p"], "open_questions": ["y"]}) is None
    assert normalize_key_points({"proposal": "p"}) is None
    assert normalize_key_points({"proposal": [3]}) is None
    assert normalize_key_points({}) is None
    assert normalize_key_points("text") is None
    assert normalize_key_points(["a", " ", "b"]) == ["a", "b"]
