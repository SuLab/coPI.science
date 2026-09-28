"""Key-point groups: the current six (scout_hub >= 1.9.0), the retired 1.8.0
group (`key_questions`, display-only), the legacy five (1.3.0–1.7.1,
display-only) and the one read helper every surface uses."""
from src.services.assessment_detail import (
    KEY_POINT_ACCEPTED_KEYS,
    KEY_POINT_GROUPS,
    LEGACY_KEY_POINT_GROUPS,
    RETIRED_KEY_POINT_GROUPS,
    key_point_sections,
    key_point_shape,
    normalize_key_points,
)

CURRENT = [k for k, _ in KEY_POINT_GROUPS]
RETIRED = [k for k, _ in RETIRED_KEY_POINT_GROUPS]
LEGACY = [k for k, _ in LEGACY_KEY_POINT_GROUPS]

#: The six keys a 1.8.0 sidecar carried.
ONE_EIGHT = [
    "indication_audience", "lab_background", "proposal",
    "clinical_actionability", "key_questions", "commercial_opportunity",
]


def test_the_current_retired_and_legacy_sets_are_pinned():
    assert KEY_POINT_GROUPS == (
        ("indication_audience", "Indication / Audience"),
        ("lab_background", "Lab Background"),
        ("proposal", "Proposal"),
        ("clinical_actionability", "Clinical Actionability"),
        ("path_to_clinic", "Path to Clinic / Commercialization"),
        ("commercial_opportunity", "Commercial Opportunity"),
    )
    # Retired by 1.9.0, still rendered for the 5 rows written under 1.8.0 —
    # under 1.8.0's label AND in 1.8.0's slot.
    assert RETIRED_KEY_POINT_GROUPS == (("key_questions", "Key Questions/Experiment"),)
    assert LEGACY_KEY_POINT_GROUPS == (
        ("significance", "Significance"),
        ("innovation", "Innovation"),
        ("clinical_actionability", "Clinical actionability"),
        ("key_questions", "Key questions / experiments"),
        ("commercial_potential", "Commercial potential"),
    )
    assert KEY_POINT_ACCEPTED_KEYS == (
        frozenset(CURRENT) | frozenset(RETIRED) | frozenset(LEGACY)
    )


def test_shape_classification():
    assert key_point_shape(["a"]) == "flat"
    assert key_point_shape({"significance": ["s"]}) == "legacy"
    assert key_point_shape({"lab_background": ["l"]}) == "current"
    # `key_questions` is retired: evidence of NEITHER shape (1.8.0 and the
    # legacy sets both wrote it), so a dict holding only it falls through to
    # "current" — and renders under the 1.8.0 label, not the legacy one. The
    # same holds for `clinical_actionability`, which current and legacy share.
    assert key_point_shape({"key_questions": ["q"]}) == "current"
    assert key_point_shape({"clinical_actionability": ["c"]}) == "current"
    # A full 1.8.0 row is "current", never "mixed": the retired key must not
    # send it down the stale-prompt branch.
    assert key_point_shape({k: ["x"] for k in ONE_EIGHT}) == "current"
    assert key_point_shape({"path_to_clinic": ["t"]}) == "current"
    assert key_point_shape({"significance": ["s"], "proposal": ["p"]}) == "mixed"
    assert key_point_shape({}) is None
    assert key_point_shape(None) is None
    assert key_point_shape("text") is None


def test_a_six_group_row_renders_in_the_new_order_under_the_new_labels():
    value = {k: [f"{k} point"] for k in reversed(CURRENT)}   # seeded out of order
    sections = key_point_sections(value)
    assert [label for label, _ in sections] == [label for _, label in KEY_POINT_GROUPS]
    assert sections[0] == ("Indication / Audience", ["indication_audience point"])


def test_a_1_8_0_row_keeps_key_questions_label_and_slot():
    """D3: a stored row renders under the labels AND in the order it was
    written with. All five 1.8.0 production rows carry this group."""
    sections = key_point_sections({
        "indication_audience": ["i"], "lab_background": ["l"], "proposal": ["p"],
        "clinical_actionability": ["c"], "key_questions": ["q"],
        "commercial_opportunity": ["o"],
    })
    assert [label for label, _ in sections] == [
        "Indication / Audience", "Lab Background", "Proposal",
        "Clinical Actionability", "Key Questions/Experiment", "Commercial Opportunity",
    ]


def test_a_1_9_0_row_renders_the_six_new_groups_in_order():
    sections = key_point_sections({
        "indication_audience": ["i"], "lab_background": ["l"], "proposal": ["p"],
        "clinical_actionability": ["c"], "path_to_clinic": ["t"],
        "commercial_opportunity": ["o"],
    })
    assert [label for label, _ in sections] == [
        "Indication / Audience", "Lab Background", "Proposal",
        "Clinical Actionability", "Path to Clinic / Commercialization",
        "Commercial Opportunity",
    ]


def test_a_legacy_row_still_uses_the_legacy_labels():
    sections = key_point_sections({"significance": ["s"], "key_questions": ["q"]})
    assert [label for label, _ in sections] == [
        "Significance", "Key questions / experiments",
    ]


def test_a_row_carrying_both_key_questions_and_path_to_clinic_renders_each_once():
    """A 1.8.0 row read after 1.9.0 lands, or a model emitting both. Neither
    may be hidden and neither may render twice."""
    sections = key_point_sections({
        "clinical_actionability": ["c"], "key_questions": ["q"], "path_to_clinic": ["t"],
    })
    assert [label for label, _ in sections] == [
        "Clinical Actionability", "Key Questions/Experiment",
        "Path to Clinic / Commercialization",
    ]
    assert len(sections) == len({label for label, _ in sections})


def test_no_key_ever_renders_twice_even_with_every_accepted_key_present():
    """The mixed branch appends legacy-only groups after the current-plus-
    retired order; `clinical_actionability` and `key_questions` are in both
    sets, so a naive append would render them twice."""
    value = {k: [f"{k} point"] for k in KEY_POINT_ACCEPTED_KEYS}
    sections = key_point_sections(value)
    bullets = [b for _, bs in sections for b in bs]
    assert len(bullets) == len(set(bullets)) == len(KEY_POINT_ACCEPTED_KEYS)
    assert [label for label, _ in sections] == [
        "Indication / Audience", "Lab Background", "Proposal",
        "Clinical Actionability", "Key Questions/Experiment",
        "Path to Clinic / Commercialization", "Commercial Opportunity",
        "Significance", "Innovation", "Commercial potential",
    ]


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


def test_normalize_accepts_current_retired_legacy_and_mixed_and_strips_blanks():
    six = {k: ["x"] for k in CURRENT}
    one_eight = {k: ["x"] for k in ONE_EIGHT}
    five = {k: ["x"] for k in LEGACY}
    assert normalize_key_points(six) == six
    assert normalize_key_points(one_eight) == one_eight
    assert normalize_key_points({**six, "key_questions": ["q"]}) == {**six, "key_questions": ["q"]}
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
