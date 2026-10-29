"""A tool is in the lab's profile only when it contains, as whole words, a method or
model-system item (or a project anchor from those sections) of at least two words.

``project_plan.tool_marks`` is the one implementation of that rule and ``display_tool_marks``
delegates to it, so the stored marks and the page can no longer disagree (F9 of
``docs/plans/2026-10-29-projects-outcomes-and-refusals-plan.md``: the stored matcher needed
full-string equality and marked 8 of 210 tools where the page marked 23). Each case is pinned
through both entry points.
"""

from src.services.project_plan import tool_marks
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


def _plan_mark(tool, profile=PROFILE, anchors=()):
    (mark,) = tool_marks([tool], profile, list(anchors))
    return mark


def test_tool_marks_reads_containment():
    mark = _plan_mark("DNase I protection assays of NET DNA (published, gel-based)")
    assert mark["in_profile"] is True
    assert mark["matched"] == "DNase I protection assays of NET DNA"
    assert _plan_mark("STING agonist diABZI in monocytes") == {
        "item": "STING agonist diABZI in monocytes",
        "in_profile": False,
        "matched": None,
    }
    assert _plan_mark("biopsies from lupus nephritis cohorts")["in_profile"] is False


def test_tool_marks_takes_the_longest_candidate():
    profile = {"Model Systems": ["human neutrophils", "primary human neutrophils"]}
    assert _plan_mark("primary human neutrophils from donors", profile)["matched"] == (
        "primary human neutrophils"
    )


def test_tool_marks_matches_whole_words_only():
    profile = {"Model Systems": ["cell lines"]}
    assert _plan_mark("cell linesman", profile)["in_profile"] is False
    assert _plan_mark("cell lines (HEK293)", profile)["in_profile"] is True


def test_tool_marks_counts_method_and_model_anchors():
    anchors = [
        {"section": "Model Systems", "item": "in vitro reconstituted NET-ALR protein systems"}
    ]
    mark = _plan_mark("Our in vitro reconstituted NET-ALR protein systems", {}, anchors)
    assert mark["in_profile"] is True
    assert mark["matched"] == "in vitro reconstituted NET-ALR protein systems"
    target = [{"section": "Key Molecular Targets", "item": "STING pathway signalling"}]
    assert _plan_mark("STING pathway signalling reporter", {}, target)["in_profile"] is False


def test_tool_marks_ignores_single_word_candidates():
    assert _plan_mark("ELISA kits", {"Key Methods and Technologies": ["ELISA"]})[
        "in_profile"
    ] is False


def test_tool_marks_drops_a_non_string_tool():
    assert tool_marks(["a b", 3, None], {}, []) == [
        {"item": "a b", "in_profile": False, "matched": None}
    ]


def test_a_qualified_profile_item_marks_in_profile():
    """Run d54d7386's coyne close wrote its tool as prose around the profile's own wording;
    the close prompt never asks for a verbatim copy, and equality missed it (F9)."""
    item = ("pan-expansion microscopy (pan-ExM) with machine-learning-facilitated NPC "
            "segmentation")
    profile = {"Key Methods and Technologies": [item]}
    mark = _plan_mark(item + " (contingent, requires re-training)", profile)
    assert mark["in_profile"] is True
    assert mark["matched"] == item


def test_the_view_delegates():
    tools = ["DNase I protection assays of NET DNA (published, gel-based)", "STING agonist", 3]
    anchors = [{"section": "Model Systems", "item": "in vitro NET-ALR protein systems"}]
    assert display_tool_marks(tools, PROFILE, anchors) == tool_marks(tools, PROFILE, anchors)
