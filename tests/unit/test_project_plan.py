"""The pure rules of BlackbirdBot's projects (``src/services/project_plan.py``) and the
profile reader they take their items from (``persona_sections.section_items``).

The outcome tests pin the owner's rule of 2026-10-29 (D68–D73): a close is Ready on the plan
alone — every blocking field filled and no live scientific disagreement — and never on either
bot's ✅/⚠️ marker. Run d54d7386 is what the disagreement and placeholder cases are drawn from.
"""

from __future__ import annotations

import pytest

from src.agent.persona_sections import section_items
from src.agent.slack_client import split_for_wire
from src.models.hub_project import STATUS_NOT_AGREED, STATUS_READY
from src.services.project_plan import (
    BLOCKING_FIELDS,
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


#: Run d54d7386's coyne close: both markers ✅, every field filled, and one disagreement entry
#: that says there is none and then records what refinement settled. It graded not_agreed.
COYNE_DISAGREEMENT = (
    "None outstanding on the science. Resolved during refinement, recorded for context: SIM "
    "versus confocal as the Phase 1 section readout (lab corrected Blackbird - confocal, SIM "
    "unvalidated on thick sarcomeric tissue); isolated nuclei versus fixed sections as primary "
    "(moved to sections after the prep-induced artefact)."
)


def _close(plan=None, **extra):
    """A close whose plan is complete, with no gaps and no disagreement, unless overridden."""
    return {"position": "agree", "plan": FULL_PLAN if plan is None else plan,
            "gaps": [], "disagreements": [], **extra}


def test_project_outcome_rules():
    assert project_outcome(close=_close()) == (STATUS_READY, [], [])

    status, gaps, _live = project_outcome(close=_close({**FULL_PLAN, "design": ""}))
    assert status == STATUS_NOT_AGREED
    assert "Design is not filled" in gaps

    status, _gaps, live = project_outcome(close=_close(disagreements=["the model"]))
    assert status == STATUS_NOT_AGREED
    assert live == ["the model"]

    broken = {"position": "agree", "plan": {"design": 3}, "gaps": "a string",
              "disagreements": []}
    status, gaps, _live = project_outcome(close=broken)
    assert status == STATUS_NOT_AGREED
    assert "Design is not filled" in gaps
    assert "a string" not in gaps


def test_a_disagreement_that_says_only_none_is_not_a_disagreement():
    """F3: a close whose only disagreement entry says there is none must not lose Ready."""
    status, gaps, live = project_outcome(close=_close(disagreements=["None."]))
    assert status == STATUS_READY
    assert live == []


def test_other_no_disagreement_phrasings_drop():
    for entry in ("No disagreements remain.", "Nothing outstanding on the science.",
                  "None known.", "None remaining.", "(none)", "None", "Nil", "Zero",
                  "No scientific disagreements remain between us.",
                  "No live disagreement on the science."):
        status, _gaps, live = project_outcome(close=_close(disagreements=[entry]))
        assert status == STATUS_READY, entry
        assert live == [], entry


def test_a_dispute_whose_first_word_is_a_negation_survives():
    """The first SENTENCE decides, and only when it says nothing but "there is none". A
    colon, a hyphen or a dash must not cut an entry down to a bare negation: splitting on
    them dropped "Disagreement: …" and "No-go …" and graded a disputed project ready."""
    for entry in (
        "None of the proposed controls is feasible in our hands",
        "Disagreement: the lab will not run the NHP arm.",
        "Open: whether the knockout is the right model",
        "Remains open — the lab disputes the negative control.",
        "Outstanding - no agreement on the readout",
        "No-go on the mouse arm",
        "No, the lab disputes the dose.",
    ):
        status, _gaps, live = project_outcome(close=_close(disagreements=[entry]))
        assert status == STATUS_NOT_AGREED, entry
        assert live == [entry], entry


def test_a_non_string_disagreement_entry_is_kept():
    """A list whose entries are objects is still the hub naming a dispute: it must not read
    as agreement, and the text must not vanish from the record."""
    status, _gaps, live = project_outcome(
        close=_close(disagreements=[{"issue": "the lab cannot run the NHP arm"}])
    )
    assert status == STATUS_NOT_AGREED
    assert live and "NHP arm" in live[0]


def test_a_negated_resolution_is_a_disagreement():
    """The inversion to guard against: "not" is not a no-disagreement negation, and a verb
    of resolution is not a filler word. With either, "Not agreed" would read as "there is
    no disagreement" and grade a disputed project ready."""
    for entry in ("Not agreed", "Nothing is settled", "Not resolved", "No change agreed",
                  "Not all issues raised with the lab are resolved",
                  "Not every change is agreed"):
        status, _gaps, live = project_outcome(close=_close(disagreements=[entry]))
        assert status == STATUS_NOT_AGREED, entry
        assert live == [entry], entry


def test_dropped_entries_are_recorded_once():
    status, gaps, _live = project_outcome(close=_close(disagreements=["None.", "None."]))
    assert status == STATUS_READY
    assert len([g for g in gaps if g.startswith("The close recorded")]) == 1


def test_an_empty_non_string_entry_is_not_a_disagreement():
    status, _gaps, live = project_outcome(close=_close(disagreements=[0, {}, None, []]))
    assert (status, live) == (STATUS_READY, [])


def test_a_note_that_opens_none_and_then_names_a_dispute_is_a_disagreement():
    """The hiding case the whole-entry rule exists for: an entry that says there is none
    and then names one must not grade Ready."""
    entry = "No disagreement on the science. The lab will not run the NHP arm at any dose."
    status, _gaps, live = project_outcome(close=_close(disagreements=[entry]))
    assert (status, live) == (STATUS_NOT_AGREED, [entry])


def test_a_dropped_entry_is_still_recorded_as_a_gap():
    """A bare "none" entry does not block, and is still written down: the sidecar is
    stripped before posting, so `final` is the only place it survives."""
    status, gaps, live = project_outcome(close=_close(disagreements=["None."]))
    assert (status, live) == (STATUS_READY, [])
    assert any(gap.endswith("None.") for gap in gaps)


def test_a_long_unreadable_disagreements_value_is_kept_whole():
    """The sidecar is stripped before posting, so this gap is the only copy of what the
    close wrote; it is collapsed to one line but never truncated."""
    text = "The lab refuses the NHP arm. " * 60
    status, gaps, _live = project_outcome(close=_close(disagreements=text))
    assert status == STATUS_NOT_AGREED
    [recorded] = [gap for gap in gaps if gap.startswith("The close's disagreements field")]
    assert recorded.endswith(text.strip()) and "\n" not in recorded


def test_a_close_that_did_not_answer_is_not_agreed():
    """D73: silence is not "there are none". It is a verdict on the plan, not an unreadable
    close — the block parsed — so the project stays counted and a repeatable formatting slip
    cannot walk the session into its failure stop. Whatever was written survives in the gaps."""
    text = "the lab cannot run the NHP arm"
    status, gaps, live = project_outcome(close=_close(disagreements=text))
    assert status == STATUS_NOT_AGREED
    assert "The close did not say whether a scientific disagreement remains" in gaps
    assert any(text in gap for gap in gaps)
    assert live == []

    silent = {"position": "agree", "plan": FULL_PLAN, "gaps": []}
    status, gaps, live = project_outcome(close=silent)
    assert status == STATUS_NOT_AGREED
    assert "The close did not say whether a scientific disagreement remains" in gaps
    assert live == []


def test_the_markers_no_longer_decide():
    for position in ("agree", "disagree", None, "⚠️ we cannot run it"):
        status, gaps, _live = project_outcome(close=_close(position=position))
        assert status == STATUS_READY, position
        assert gaps == []


def test_time_and_budget_does_not_block():
    status, gaps, _live = project_outcome(
        close=_close({**FULL_PLAN, "time_and_budget": "to settle with the lab"})
    )
    assert status == STATUS_READY
    assert "Time and budget is not filled" not in gaps


def test_a_blocking_field_placeholder_is_not_agreed():
    status, gaps, _live = project_outcome(
        close=_close({**FULL_PLAN, "positive_control": "to settle with the lab"})
    )
    assert status == STATUS_NOT_AGREED
    assert "Positive control is not filled" in gaps


def test_placeholder_rule():
    for value in ("none exists, because no drug is approved", "None-the-less we will", "tbdx",
                  "unknown aetiology, so the model is …"):
        assert field_filled(value), value
    for value in ("To settle with the lab (likely $150K)", "TBD — pending the quote", "none",
                  "to settle with the lab"):
        assert not field_filled(value), value


def test_missing_fields_takes_a_field_set():
    assert missing_fields(FULL_PLAN) == []
    blocking = missing_fields({}, BLOCKING_FIELDS)
    assert len(blocking) == 9
    assert "Time and budget" not in blocking


def test_a_close_whose_gaps_are_the_institutions_does_not_lose_ready():
    """D71: a gap only staff or the institution can close never blocks."""
    close = _close(gaps=["The indirect rate is the institution's to state"])
    status, gaps, live = project_outcome(close=close)
    assert (status, gaps, live) == (
        STATUS_READY, ["The indirect rate is the institution's to state"], []
    )


def test_run_d54d7386_coyne_keeps_its_note_as_a_disagreement():
    """coyne's entry opens "None outstanding on the science." and then writes two more
    sentences. A note that says there is none and a note that hides one ("No disagreement
    on the science. The lab will not run the NHP arm.") are the same shape, and no reading
    of the text separates them — so anything beyond a bare "none" is kept, and the verdict
    is Not agreed with the hub's own words in front of the reader. The close prompt now
    asks for `[]`, so a close of this shape is a close that did not follow it."""
    status, _gaps, live = project_outcome(close=_close(disagreements=[COYNE_DISAGREEMENT]))
    assert status == STATUS_NOT_AGREED
    assert live == [COYNE_DISAGREEMENT]
