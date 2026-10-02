"""B-07: the detail page reads only the tool_use/tool_result blocks of each hub turn;
the chips built from that slice equal the chips built from the whole conversation."""

import pytest
from sqlalchemy import select

from src.models import LlmCallLog
from src.services.assessment_detail import TOOL_BLOCKS_SLICE, tool_chips_from_conversation
from tests import factories

pytestmark = pytest.mark.integration

_CONVERSATION = [
    "the opening user string",
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "x" * 5000, "signature": "sig"},
        {"type": "text", "text": "Let me ask two specialists."},
        {"type": "tool_use", "id": "t1", "name": "consult_specialist", "input": {"domain": "clinical", "question": "Q1"}},
        {"type": "tool_use", "id": "t2", "name": "lookup", "input": {"query": "Q2"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "content": "lookup result"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "{\"signal\": \"adequate\"}"},
    ]},
    {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
]


@pytest.mark.parametrize("messages_json", [_CONVERSATION, {"messages": []}, [], ["only a string"]])
async def test_chips_from_the_slice_equal_chips_from_the_whole(db_session, messages_json):
    log = await factories.make_llm_call_log(db_session, messages_json=messages_json)
    sliced = (
        await db_session.execute(select(TOOL_BLOCKS_SLICE).where(LlmCallLog.id == log.id))
    ).scalar_one()
    assert tool_chips_from_conversation(sliced) == tool_chips_from_conversation(messages_json)
    if messages_json is _CONVERSATION:
        assert "thinking" not in str(sliced) and "Let me ask" not in str(sliced)
