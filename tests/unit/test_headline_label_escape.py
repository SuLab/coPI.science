"""The headline label is escaped once, on the wire (R1.11 of the 2026-10-15
publication-handling design; E-D3): the rendered headline is source text."""
from src.agent.slack_client import wire_text
from src.services.assessment_headline import render_assessment_headline


def _render(pi_label: str) -> str:
    return render_assessment_headline(
        pi_label=pi_label, project="P", recommendation="conditional", scores={}, permalink=None,
    )


def test_the_headline_text_is_unescaped_source():
    assert _render("Lab <!channel> & Co").startswith(":mag: Lab <!channel> & Co — P → *conditional*")


def test_the_wire_escapes_the_label_once():
    assert wire_text(_render("Lab <!channel> & Co")).startswith(
        ":mag: Lab &lt;!channel&gt; &amp; Co — P → *conditional*")
    wired = wire_text(_render("R&amp;D"))
    assert "R&amp;D" in wired and "R&amp;amp;D" not in wired
