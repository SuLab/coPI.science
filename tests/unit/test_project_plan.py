"""The pure rules of BlackbirdBot's projects (``src/services/project_plan.py``) and the
profile reader they take their items from (``persona_sections.section_items``)."""

from __future__ import annotations

import pytest

from src.agent.persona_sections import section_items
from src.agent.slack_client import split_for_wire
from src.models.hub_project import STATUS_NOT_AGREED, STATUS_READY
from src.services.project_plan import (
    POSTING_HEADER,
    POSTING_MAX_CHARS,
    POSTING_MAX_MESSAGES,
    REPLY_HEADER,
    TEMPLATE_FIELDS,
    NameEntry,
    assemble_posting,
    check_anchors,
    display_id,
    field_filled,
    fits_one_message,
    fits_posting,
    foreign_names,
    has_link,
    has_method_or_model_anchor,
    is_project_post_text,
    missing_fields,
    normalize_item,
    position_marker,
    posting_head,
    project_outcome,
    resolve_source,
    source_key,
)

PERSONA = """# Jeffrey Gray

## Research Summary
Protein structure prediction and design.

## Key Methods and Technologies
- protein-protein docking with RosettaDock 4.0
- deep learning for antibody structure

## Model Systems
- antibodies

## Disease Areas / Biological Processes
- immune recognition

## Key Molecular Targets
- GH62 α-L-arabinofuranosidase

## Keywords
Rosetta, open-source molecular modeling (OMSF, OpenFold), PyRosetta

## Recent Publications
- A paper. (PMID 31500698)
"""

FULL_PLAN = {
    "indication": "cholestatic itch",
    "target": "MRGPRX4",
    "model": "humanised mouse",
    "positive_control": "nalfurafine",
    "negative_control": "vehicle",
    "standard_of_care": "none exists, because no drug is approved",
    "readout": "scratching bouts per hour",
    "design": "blinded, randomised, 12 per arm",
    "time_and_budget": "six months, $40k",
    "lab_tools": ["protein-protein docking with RosettaDock 4.0"],
}


def test_section_items_reads_bullets_and_keywords():
    items = section_items(PERSONA)
    assert items["Key Methods and Technologies"] == [
        "protein-protein docking with RosettaDock 4.0",
        "deep learning for antibody structure",
    ]
    assert items["Model Systems"] == ["antibodies"]
    assert items["Keywords"] == [
        "Rosetta",
        "open-source molecular modeling (OMSF, OpenFold)",
        "PyRosetta",
    ]
    assert section_items("## Research Summary\nx\n")["Model Systems"] == []


def test_normalize_item_folds_case_dashes_unicode_and_whitespace():
    assert normalize_item("GH62 α‑L‑arabinofuranosidase.") == normalize_item(
        "gh62  α-l-arabinofuranosidase"
    )


def test_anchor_must_equal_a_whole_item():
    item = "protein-protein docking with RosettaDock 4.0 …"
    items = {"Key Methods and Technologies": [item]}
    kept, dropped = check_anchors(
        [
            {"section": "Key Methods and Technologies", "item": "docking"},
            {"section": "Model Systems", "item": "Protein-protein docking with RosettaDock 4.0 …"},
        ],
        items,
    )
    assert kept == [{"section": "Key Methods and Technologies", "item": item}]
    assert dropped == [
        {
            "section": "Key Methods and Technologies",
            "item": "docking",
            "why": "not an item of the lab's profile",
        }
    ]


def test_method_or_model_anchor_required():
    assert has_method_or_model_anchor([{"section": "Key Molecular Targets", "item": "x"}]) is False
    assert has_method_or_model_anchor([{"section": "Model Systems", "item": "x"}]) is True


def test_source_key_forms():
    assert source_key("31500698", "pubmed") == "pmid:31500698"
    assert source_key("nct01903460", "trial") == "nct:NCT01903460"
    assert source_key("US 11,234,567 B2", "patent") == "patent:US11234567B2"
    assert source_key("", "pubmed") is None


def test_resolve_source_uses_what_was_retrieved():
    assert resolve_source("17123456", {"patent:17123456": {}}) == "patent:17123456"
    assert resolve_source("PMID: 31500698", {"pmid:31500698": {}}) == "pmid:31500698"
    assert resolve_source("99999999", {"pmid:31500698": {}}) is None


def test_is_project_post_text():
    assert is_project_post_text(POSTING_HEADER.format(surname="Gray") + "\nBody")
    assert is_project_post_text(REPLY_HEADER.format(title="A use"))
    assert not is_project_post_text(":bulb: A pitch about docking antibodies")
    assert not is_project_post_text("")
    assert not is_project_post_text(None)


def test_display_id_forms():
    assert display_id("pmid:31500698") == "PMID 31500698"
    assert display_id("nct:NCT01903460") == "NCT01903460"
    assert display_id("patent:US11234567B2") == "US11234567B2"
    assert display_id("other") == "other"


def test_has_link_and_fits_one_message():
    assert has_link("see https://x.org")
    assert has_link("<http://x|y>")
    assert not has_link("no link here")
    assert not fits_one_message("&" * 3400)
    assert fits_one_message("a" * 3400)


def test_field_filled_and_missing_fields():
    for value in ("to settle with the lab", "TBD", "", "none"):
        assert not field_filled(value)
    assert field_filled("none exists, because no drug is approved")
    assert not field_filled([])
    assert missing_fields({**FULL_PLAN, "lab_tools": []}) == ["The lab's tools and models used"]
    assert missing_fields(FULL_PLAN) == []
    assert missing_fields({}) == [label for _key, label in TEMPLATE_FIELDS]


def test_foreign_names_rule():
    roster = [
        NameEntry("gray", "GrayBot", "Jeffrey Gray"),
        NameEntry("wilson", "WilsonBot", "Ann Wilson"),
        NameEntry("blackbird", "BlackbirdBot", "Blackbird"),
    ]

    def names(text, entries=roster):
        return foreign_names(text, roster=entries, own_agent_id="gray", hub_agent_ids={"blackbird"})

    for text in ("gray matter", "Wilson disease", "BlackbirdBot", "Gray lab"):
        assert names(text) == [], text
    for text in ("the Wilson lab", "Dr. Wilson", "WilsonBot", "Ann Wilson"):
        assert names(text), text

    shared = [*roster, NameEntry("pgray", "PGrayBot", "Paula Gray")]
    assert names("Gray lab", shared) == []
    assert names("Paula Gray", shared)


def _source(i, title=None):
    return {"key": f"pmid:{1000 + i}", "title": title or f"Paper {i}"}


def _posting(sources, summary="An assay for cholestatic itch."):
    return assemble_posting(
        surname="Gray", summary=summary, anchors=["a", "b"], plan=FULL_PLAN, sources=sources
    )


def test_assemble_posting_layout_and_limit():
    text = _posting([_source(1)])
    lines = text.split("\n")
    assert lines[0] == ":test_tube: *Blackbird project for the Gray lab*"
    assert "Builds on: a; b" in lines
    assert "• Indication: cholestatic itch" in lines
    assert "• PMID 1001 — Paper 1" in lines

    many = _posting([_source(i) for i in range(30)])
    assert sum(line.startswith("• PMID ") for line in many.split("\n")) <= 5

    long_titles = _posting([_source(i, "T" * 2500) for i in range(5)])
    assert fits_posting(long_titles)
    assert len(long_titles) <= POSTING_MAX_CHARS

    with pytest.raises(ValueError, match="posting too long"):
        _posting([_source(1)], summary="x" * 7500)


def test_a_posting_may_take_two_slack_messages():
    """Owner, 2026-10-27: a posting longer than one Slack message posts as two (the second
    in the first one's Slack thread) rather than failing the choice."""
    assert POSTING_MAX_MESSAGES == 2
    text = _posting([_source(i) for i in range(5)], summary="x " * 2000)
    assert not fits_one_message(text) and fits_posting(text)
    chunks = split_for_wire(text)
    assert len(chunks) == 2
    assert sum(line.startswith("• PMID ") for line in text.split("\n")) == 5
    assert posting_head(text) == chunks[0] and chunks[0].startswith(POSTING_HEADER.format(
        surname="Gray"))
    short = _posting([_source(1)])
    assert posting_head(short) == short
    assert not fits_posting("a " * 6000)


def test_source_titles_cannot_carry_slack_tokens_or_links():
    # Titles come from PubMed, ClinicalTrials.gov and USPTO, not the model, and are checked
    # nowhere else; escape_for_slack keeps <@U…> and <https://…|…> tokens live.
    text = _posting([
        _source(1, "Foo <https://x.example|bar> <@U12345> study"),
        _source(2, "See www.example.org for data"),
        {"key": "patent:US<@U9>1", "title": "A patent"},
    ])
    assert "<" not in text and ">" not in text and "@" not in text
    assert "https://" not in text and "www." not in text
    lines = text.split("\n")
    assert "• PMID 1002" in lines
    assert "• US U91 — A patent" not in lines and "• USU91 — A patent" in lines


def test_has_link_refuses_live_slack_tokens_and_titles_drop_bare_domains():
    for text in ("see <#C0ABC123|general>", "<mailto:x.example|ask the office>",
                 "mailto:x.example"):
        assert has_link(text), text
    assert not has_link("an assay of p53 and cell lines")
    text = _posting([_source(1, "Login at evil-site.com/login now"), _source(2, "Plain title")])
    lines = text.split("\n")
    assert "• PMID 1001" in lines and "evil-site" not in text
    assert "• PMID 1002 — Plain title" in lines


def test_position_marker_forms():
    assert position_marker("  ✅ agreed") == "agree"
    assert position_marker(":white_check_mark: yes") == "agree"
    for text in ("⚠ missing", ":warning: x", "⏸️ stop"):
        assert position_marker(text) == "disagree", text
    assert position_marker("I agree ✅") is None


def test_project_outcome_rules():
    agreed = {"position": "agree", "plan": FULL_PLAN, "gaps": [], "disagreements": []}
    assert project_outcome(lab_position="agree", hub_marker="agree", close=agreed) == (
        STATUS_READY,
        [],
    )

    no_design = {**agreed, "plan": {**FULL_PLAN, "design": ""}}
    status, gaps = project_outcome(lab_position="agree", hub_marker="agree", close=no_design)
    assert status == STATUS_NOT_AGREED
    assert "Design is not filled" in gaps

    status, gaps = project_outcome(
        lab_position=position_marker("⚠️ we cannot run it"), hub_marker="agree", close=agreed
    )
    assert status == STATUS_NOT_AGREED
    assert "The lab did not agree" in gaps

    split = {**agreed, "disagreements": ["the model"]}
    status, _gaps = project_outcome(lab_position="agree", hub_marker="agree", close=split)
    assert status == STATUS_NOT_AGREED

    broken = {"position": "agree", "plan": {"design": 3}, "gaps": "a string"}
    status, gaps = project_outcome(lab_position="agree", hub_marker="agree", close=broken)
    assert status == STATUS_NOT_AGREED
    assert "Design is not filled" in gaps
    assert "a string" not in gaps
