"""Labelled key-point extras (scout_hub 1.10.0,
docs/specs/2026-10-02-hub-1-10-summary-risks-gates-design.md §6.1 Rendering, Review
Focus #3 of the plan): the classifier, `KeyPointSection.extras`, and how the chat
record renders a labelled group — rows without labelled extras byte-identical."""
import pytest

from src.services import assessment_chat_record as rec
from src.services.assessment_detail import (
    KEY_POINT_COMPANIES,
    KEY_POINT_RISK,
    KeyPointExtra,
    KeyPointSection,
    classify_key_point,
    key_point_sections,
)
from tests.assessment_chat_support import synthetic_detail
from tests.unit import _frozen_verdict_doc as frozen


@pytest.mark.parametrize("text, expected", [
    ("Risk: ownership is unresolved.", (KEY_POINT_RISK, "ownership is unresolved.")),
    ("risk: x", (KEY_POINT_RISK, "x")),
    ("RISK : x", (KEY_POINT_RISK, "x")),
    ("Risk :x", (KEY_POINT_RISK, "x")),
    ("**Risk:** x", (KEY_POINT_RISK, "x")),
    ("**Risk**: x", (KEY_POINT_RISK, "x")),
    ("*Risk:* x", (KEY_POINT_RISK, "x")),
    ("_Risk_: x", (KEY_POINT_RISK, "x")),
    ("**Risk: the whole bullet in bold**", (KEY_POINT_RISK, "the whole bullet in bold")),
    ("  Risk: x  ", (KEY_POINT_RISK, "x")),
    ("Companies: DELFI Diagnostics (founder, 2014).",
     (KEY_POINT_COMPANIES, "DELFI Diagnostics (founder, 2014).")),
    ("__Companies:__ x", (KEY_POINT_COMPANIES, "x")),
    ("companies : x", (KEY_POINT_COMPANIES, "x")),
], ids=[
    "plain", "lower", "upper-space-before-colon", "no-space-after-colon", "bold-label-and-colon",
    "bold-label", "italic", "underscore", "whole-bullet-bold", "padded", "companies",
    "companies-underscore-bold", "companies-lower-spaced",
])
def test_a_labelled_extra_is_classified_whatever_its_case_spacing_or_markup(text, expected):
    assert classify_key_point(text) == expected


@pytest.mark.parametrize("text", [
    "A complete claim about the proposal.",
    "Risky approach: a new modality.",
    "Risks: the plural is not the label.",
    "Company: the singular is not the label.",
    "Risk-adjusted: not a label either.",
    "The risk: a label must open the bullet.",
    "Risk without a colon",
    "Risk:",
    "**Risk:**",
])
def test_anything_else_is_a_main_bullet_returned_unchanged(text):
    assert classify_key_point(text) == (None, text)


def test_a_non_string_never_raises():
    assert classify_key_point(None) == (None, None)  # type: ignore[arg-type]


def test_a_group_whose_extras_all_carry_a_label_exposes_them():
    section = KeyPointSection("Lab Background", ["main", "**Companies:** c", "Risk: r"])
    assert section.extras == [
        KeyPointExtra(KEY_POINT_COMPANIES, "Companies:", "c"),
        KeyPointExtra(KEY_POINT_RISK, "Risk:", "r"),
    ]


@pytest.mark.parametrize("section", [
    KeyPointSection("Proposal", ["only"]),
    KeyPointSection("Proposal", ["main", "an unlabelled second bullet"]),
    KeyPointSection("Proposal", ["main", "Risk: r", "an unlabelled third"]),
    KeyPointSection(None, ["flat one", "Risk: a flat list is never label-rendered"]),
], ids=["single", "unlabelled-extra", "one-unlabelled-extra", "flat"])
def test_any_other_group_keeps_todays_rendering(section):
    assert section.extras is None


def test_sections_still_compare_and_unpack_as_plain_pairs():
    sections = key_point_sections({"proposal": ["p", "Risk: r"]})
    assert sections == [("Proposal", ["p", "Risk: r"])]
    [(label, points)] = sections
    assert (label, points) == ("Proposal", ["p", "Risk: r"])


def _find(record, prefix):
    return [
        block["text"]
        for block in record.documents[0]["source"]["content"]
        if block["text"].startswith("[" + prefix)
    ]


@pytest.mark.parametrize("tier", ["staff", "reviewer"])
def test_a_labelled_group_is_one_block_with_its_extras_as_plain_lines(tier):
    detail = synthetic_detail()
    detail["assessment"].key_points = {
        "lab_background": ["KP-LAB-MAIN", "**Companies:** KP-LAB-COMPANY"],
        "commercial_opportunity": ["KP-CO-MAIN", "risk : KP-CO-RISK"],
        "proposal": ["KP-PROP-ONE", "KP-PROP-TWO"],
    }
    record = rec.build_chat_record(detail, tier=tier)
    assert _find(record, "Key point — Lab Background") == [
        "[Key point — Lab Background]\n> KP-LAB-MAIN\n> Companies: KP-LAB-COMPANY"
    ]
    assert _find(record, "Key point — Commercial Opportunity") == [
        "[Key point — Commercial Opportunity]\n> KP-CO-MAIN\n> Risk: KP-CO-RISK"
    ]
    # An unlabelled extra keeps today's one block per bullet.
    assert _find(record, "Key point — Proposal") == [
        "[Key point — Proposal]\n> KP-PROP-ONE",
        "[Key point — Proposal]\n> KP-PROP-TWO",
    ]


@pytest.mark.parametrize("key_points", [
    None,
    ["flat one", "flat two"],
    {"proposal": ["one"]},
    {"proposal": ["one", "two"], "commercial_opportunity": ["main", "not a label"]},
    {"significance": ["s"], "key_questions": ["q1", "q2"]},
], ids=["none", "flat", "single", "unlabelled-extras", "legacy"])
@pytest.mark.parametrize("tier", ["staff", "reviewer"])
def test_rows_without_labelled_extras_render_exactly_as_the_frozen_record(key_points, tier):
    detail = synthetic_detail()
    detail["assessment"].key_points = key_points
    assert rec._verdict_doc(detail, tier).blocks == frozen._verdict_doc(detail, tier).blocks


def test_the_frozen_comparison_is_not_vacuous():
    detail = synthetic_detail()
    detail["assessment"].key_points = {"commercial_opportunity": ["main", "Risk: r"]}
    assert rec._verdict_doc(detail, "staff").blocks != frozen._verdict_doc(detail, "staff").blocks
