"""E1: a reply with no usable text must not vanish silently.

The engine's Phase-4 path treats an empty string as "skip this turn", so any
path that can quietly produce "" is a turn — and after two in a row, an
interview — lost with no trace. See docs/specs/2026-08-21-hub-prompt-v3-design.md
§8 Window 0.
"""
from src.services import llm
from tests.fakes import multi_text_response


def test_all_text_joins_every_text_block():
    # A concluding hub reply emits the <assessment_json> sidecar LAST. Taking
    # only block 0 dropped it while leaving the visible half intact, which is
    # exactly how a verdict goes missing while Slack looks normal.
    message = multi_text_response("<slack_message>verdict</slack_message>",
                                  "<assessment_json>{}</assessment_json>")
    assert llm._all_text(message) == (
        "<slack_message>verdict</slack_message>\n"
        "<assessment_json>{}</assessment_json>"
    )


def test_all_text_returns_empty_string_when_there_is_no_text_block():
    message = multi_text_response()
    assert llm._all_text(message) == ""
