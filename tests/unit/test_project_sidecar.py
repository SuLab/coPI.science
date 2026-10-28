"""The `<project_json>` close block (spec §5.8): read the last closed block, strip every
block from the text that reaches Slack."""
import json

from src.agent.engine.sidecar import (
    _extract_project_json,
    _missing_closers,
    _strip_project_sidecar,
)

PLAN = {
    "indication": "Atherosclerosis",
    "target": "LOX-1",
    "model": "ApoE-/- mice",
    "positive_control": "Atorvastatin arm",
    "negative_control": "Vehicle",
    "standard_of_care": "Statins",
    "readout": "Plasma lipidomics",
    "design": "Two arms, ten mice each",
    "time_and_budget": "9 months, $200K",
    "lab_tools": ["LC-MS lipidomics"],
}


def _block(body: str) -> str:
    return f"<project_json>{body}</project_json>"


def test_last_closed_block_wins():
    raw = ('Draft.\n<project_json>{"plan": {"model": "mouse"}}</project_json>\n'
           'Final.\n<project_json>{"plan": {"model": "organoid"}, "position": "agree"}'
           '</project_json>')
    assert _extract_project_json(raw) == {"plan": {"model": "organoid"}, "position": "agree"}


def test_fenced_json_is_read():
    raw = '<project_json>\n```json\n{"plan": {"readout": "qPCR"}}\n```\n</project_json>'
    assert _extract_project_json(raw) == {"plan": {"readout": "qPCR"}}


def test_a_block_without_a_plan_object_is_none():
    assert _extract_project_json('<project_json>{"position": "agree"}</project_json>') is None
    assert _extract_project_json('<project_json>{"plan": "text"}</project_json>') is None
    assert _extract_project_json('<project_json>[1, 2]</project_json>') is None
    assert _extract_project_json("<project_json>{not json}</project_json>") is None
    assert _extract_project_json(None) is None
    assert _extract_project_json("no block here") is None


def test_unclosed_tail_is_stripped():
    text = 'We agree.\n<project_json>{"plan": {"model": "orga'
    assert _strip_project_sidecar(text) == "We agree."


def test_closed_blocks_are_stripped():
    text = 'Before.\n<project_json>{"plan": {}}</project_json>\nAfter.'
    assert _strip_project_sidecar(text) == "Before.\n\nAfter."


def test_text_without_a_block_is_unchanged():
    assert _strip_project_sidecar("Plain reply.") == "Plain reply."


def test_a_block_one_brace_short_with_its_tail_inside_plan_is_repaired():
    # Run 1abf809c's shape: `plan` never closed, so `gaps` and `disagreements` sit inside it.
    body = ('{"position": "agree", "plan": {' + json.dumps(PLAN)[1:-1]
            + ', "gaps": ["No budget yet"], "disagreements": []}')
    assert _extract_project_json(_block(body)) == {
        "position": "agree", "plan": PLAN, "gaps": ["No budget yet"], "disagreements": [],
    }


def test_raw_line_breaks_inside_strings_are_read():
    body = '{"plan": {"readout": "qPCR\nand ELISA"}}'
    assert _extract_project_json(_block(body)) == {"plan": {"readout": "qPCR\nand ELISA"}}


def test_top_level_keys_left_in_a_balanced_plan_move_up():
    body = '{"position": "agree", "plan": {"readout": "qPCR", "gaps": [], "disagreements": ["x"]}}'
    assert _extract_project_json(_block(body)) == {
        "position": "agree", "plan": {"readout": "qPCR"}, "gaps": [], "disagreements": ["x"],
    }


def test_a_top_level_key_already_present_is_not_overwritten():
    close = {"plan": {"readout": "qPCR", "gaps": ["inner"]}, "gaps": ["outer"]}
    assert _extract_project_json(_block(json.dumps(close))) == close


def test_a_mid_text_error_is_not_repaired():
    assert _extract_project_json(_block('{"plan": {"readout": "the "best" assay"}}')) is None


def test_a_misnested_bracket_is_not_repaired():
    assert _extract_project_json(_block('{"plan": {"lab_tools": ["a"}}')) is None


def test_missing_closers():
    assert _missing_closers('{"a": [1, {"b": "}"') == "}]}"
    assert _missing_closers('{"a": "x') is None
    assert _missing_closers('{"a": 1}') == ""
    assert _missing_closers('{"a": [1}') is None
