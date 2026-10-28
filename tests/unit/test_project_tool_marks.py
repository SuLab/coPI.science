"""``display_tool_marks``: a tool is in the lab's profile only when it contains, as whole
words, a method or model-system item (or a project anchor from those sections) of at least
two words."""

from src.services.projects_view import display_tool_marks

PROFILE = {
    "Key Methods and Technologies": ["DNase I protection assays of NET DNA"],
    "Model Systems": ["primary human neutrophils"],
    "Key Molecular Targets": ["STING"],
    "Keywords": ["lupus nephritis"],
}


def _mark(tool, profile=PROFILE, anchors=()):
    (mark,) = display_tool_marks([tool], profile, list(anchors))
    return mark


def test_method_contained_in_tool_is_in_profile():
    mark = _mark("DNase I protection assays of NET DNA (published, gel-based)")
    assert mark["in_profile"] is True
    assert mark["matched"] == "DNase I protection assays of NET DNA"


def test_target_only_tool_is_stated():
    mark = _mark("STING agonist diABZI in monocytes")
    assert mark["in_profile"] is False
    assert mark["matched"] is None


def test_keyword_only_tool_is_stated():
    assert _mark("biopsies from lupus nephritis cohorts")["in_profile"] is False


def test_single_word_items_never_match():
    profile = {"Key Methods and Technologies": ["ELISA"]}
    assert _mark("ELISA kits", profile)["in_profile"] is False


def test_anchor_counts():
    anchors = [
        {"section": "Model Systems", "item": "in vitro reconstituted NET-ALR protein systems"}
    ]
    mark = _mark("Our in vitro reconstituted NET-ALR protein systems", {}, anchors)
    assert mark["in_profile"] is True
    assert mark["matched"] == "in vitro reconstituted NET-ALR protein systems"


def test_anchor_from_a_target_section_does_not_count():
    anchors = [{"section": "Key Molecular Targets", "item": "STING pathway signalling"}]
    assert _mark("STING pathway signalling reporter", {}, anchors)["in_profile"] is False


def test_longest_match_wins():
    profile = {"Model Systems": ["human neutrophils", "primary human neutrophils"]}
    mark = _mark("primary human neutrophils from donors", profile)
    assert mark["matched"] == "primary human neutrophils"


def test_whole_words_only():
    profile = {"Model Systems": ["cell lines"]}
    assert _mark("cell linesman", profile)["in_profile"] is False
    assert _mark("cell lines (HEK293)", profile)["in_profile"] is True


def test_non_string_tools_left_out():
    assert display_tool_marks(["a b", 3, None], {}, []) == [
        {"item": "a b", "in_profile": False, "matched": None}
    ]
