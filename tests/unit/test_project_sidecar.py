"""The `<project_json>` close block (spec §5.8): read the last closed block, strip every
block from the text that reaches Slack."""
from src.agent.engine.sidecar import _extract_project_json, _strip_project_sidecar


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
