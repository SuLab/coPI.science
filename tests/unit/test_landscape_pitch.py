"""The compact card's pitch (D95 of docs/specs/2026-10-30-blackbird-landscaping-design.md):
the first two sentences of the elevator pitch, with Markdown links kept whole."""
from src.services.landscape_view import _pitch


def test_first_two_sentences():
    assert _pitch("One. Two?\n\nThree! Four.") == "One. Two?"


def test_nothing_to_cut():
    assert _pitch(None) is None
    assert _pitch("") is None
    assert _pitch("  Only one sentence.  ") == "Only one sentence."


def test_markdown_link_is_never_cut():
    text = (
        "It builds on [Smith et al. 2024. A gate title](<https://doi.org/10.1/x>). "
        "It also cites [a model link](https://example.org/a.b). Third."
    )
    assert _pitch(text) == (
        "It builds on [Smith et al. 2024. A gate title](<https://doi.org/10.1/x>). "
        "It also cites [a model link](https://example.org/a.b)."
    )
