"""E1: a reply with no usable text must not vanish silently.

The engine's Phase-4 path treats an empty string as "skip this turn", so any
path that can quietly produce "" is a turn — and after two in a row, an
interview — lost with no trace. See docs/specs/2026-08-21-hub-prompt-v3-design.md
§8 Window 0.
"""
import logging

import pytest

from src.services import llm
from tests.fakes import FakeAnthropic, multi_text_response


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


def test_empty_reply_logs_an_error_naming_the_stop_reason(caplog):
    # `refusal` is the case that motivated this: the turn is skipped, and
    # before this ERROR the only trace on the generate_with_tools path was a
    # WARNING in the engine that did not say WHY the text was empty.
    message = multi_text_response(stop_reason="refusal")
    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        llm._log_empty_reply(
            message,
            model="claude-opus-5",
            log_meta={"agent_id": "blackbird", "phase": "thread_reply"},
            where="final",
        )
    assert len(caplog.records) == 1
    text = caplog.records[0].getMessage()
    assert "refusal" in text
    assert "blackbird" in text
    assert "thread_reply" in text
    assert "final" in text


def test_empty_reply_logger_never_raises_on_a_bare_message():
    class Bare:
        content: list = []

    llm._log_empty_reply(Bare(), model="m", log_meta=None, where="final")


@pytest.mark.asyncio
async def test_an_empty_retry_after_truncation_still_logs(monkeypatch, caplog):
    # First call truncates with ZERO text (adaptive thinking ate the budget);
    # the retry returns nothing either. v1 of this fix gated only on the first
    # call's stop_reason, so this exact sequence returned "" in silence.
    fake = FakeAnthropic(responses=[
        multi_text_response(stop_reason="max_tokens"),
        multi_text_response(),
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    async def _executor(name, params):
        return "unused"

    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        out = await llm.generate_with_tools(
            system_prompt="s",
            messages=[{"role": "user", "content": "u"}],
            tools=[{"name": "t", "description": "d",
                    "input_schema": {"type": "object"}}],
            tool_executor=_executor,
        )

    assert out == ""
    assert any("final_retry" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_truncated_reply_survives_a_retry_that_returns_nothing(
    monkeypatch, caplog
):
    # First call truncates but already carries usable text; the retry (fired
    # because of that truncation) comes back with none. `response_text =
    # _all_text(retry_msg) or response_text` must keep the truncated text
    # rather than clobber it with the retry's "" — that's the data-recovery
    # half of the fix the sibling test above only covers the logging half of.
    fake = FakeAnthropic(responses=[
        multi_text_response("partial", stop_reason="max_tokens"),
        multi_text_response(),
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    async def _executor(name, params):
        return "unused"

    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        out = await llm.generate_with_tools(
            system_prompt="s",
            messages=[{"role": "user", "content": "u"}],
            tools=[{"name": "t", "description": "d",
                    "input_schema": {"type": "object"}}],
            tool_executor=_executor,
        )

    assert out == "partial"
    assert caplog.records == []
