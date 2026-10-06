"""Headline labels are Slack-escaped (spec 2026-10-05 §6.4, D60)."""
from src.services.assessment_headline import render_assessment_headline, slack_escape


def test_slack_escape():
    assert slack_escape("A & B <c>") == "A &amp; B &lt;c&gt;"
    assert slack_escape("Jane O’Brien-Ó") == "Jane O’Brien-Ó"


def test_the_pi_label_is_escaped_in_the_headline():
    text = render_assessment_headline(
        pi_label="Lab <!channel> & Co", project="P", recommendation="conditional",
        scores={}, permalink=None,
    )
    assert text.startswith(":mag: Lab &lt;!channel&gt; &amp; Co — P → *conditional*")
