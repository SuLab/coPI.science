"""The review copy's reviewer note must quote the split the live weights produce
(it hard-coded "35% / 65%" until rubric 3.5.0 changed the weights)."""
from scripts.render_rubric_review_doc import render_review_markdown
from src.services.blackbird_rubric import RUBRIC_WEIGHTS, load_rubric


def test_the_reviewer_note_quotes_the_split_the_weights_produce():
    science = RUBRIC_WEIGHTS["scientific_credibility"] + RUBRIC_WEIGHTS["translational_path"]
    markdown = render_review_markdown(load_rubric(), "2026-09-25", "")
    assert f"the {science}% / {100 - science}% split quoted below" in markdown
    assert "35% / 65%" not in markdown
