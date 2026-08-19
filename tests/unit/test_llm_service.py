"""Pins src/services/llm.py orchestration that was previously uncovered:
max-tokens retry, empty-content handling, and the tool-use loop. FakeAnthropic
scripts the model responses; get_anthropic_client is the monkeypatch seam.
"""

import pytest

from src.services import llm
from tests.fakes import (
    FakeAnthropic,
    empty_response,
    text_response,
    thinking_then_text_response,
    tool_use_response,
)


@pytest.fixture(autouse=True)
def _clear_llm_callback():
    # llm keeps a module-global call-log callback; don't let it leak between tests.
    llm.set_call_log_callback(None)
    yield
    llm.set_call_log_callback(None)


async def test_generate_agent_response_retries_once_on_max_tokens(monkeypatch):
    fake = FakeAnthropic(
        [
            text_response("truncated...", stop_reason="max_tokens"),
            text_response("full answer"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000
    )
    assert out == "full answer"
    assert len(fake.calls) == 2
    assert fake.calls[1]["max_tokens"] == 2000  # retried at 2x the original cap


async def test_generate_agent_response_calls_on_retry_when_it_actually_retries(monkeypatch):
    """A retried turn is two real, billed API calls. A caller booking one call
    per turn against the sliding-window rate limiter (agent.record_api_call)
    needs a hook to book the second one too, or the limiter undercounts
    exactly the agent it exists to pace (Finding A1)."""
    fake = FakeAnthropic(
        [
            text_response("truncated...", stop_reason="max_tokens"),
            text_response("full answer"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    retry_calls = []
    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000,
        on_retry=lambda: retry_calls.append(1),
    )
    assert out == "full answer"
    assert retry_calls == [1]


async def test_generate_agent_response_does_not_call_on_retry_without_truncation(monkeypatch):
    fake = FakeAnthropic([text_response("full answer")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    retry_calls = []
    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000,
        on_retry=lambda: retry_calls.append(1),
    )
    assert out == "full answer"
    assert retry_calls == []


async def test_generate_agent_response_logs_loudly_when_retry_still_truncates(monkeypatch, caplog):
    """Before this fix, a still-truncated retry's stop_reason was never
    re-checked, so a phase-5 assessment whose 15-line <assessment_json>
    sidecar is emitted last could silently lose its verdict with no trace in
    the logs. The (still truncated) text is still returned — never swallowed —
    but the truncation must be loud and identifiable (Finding A1)."""
    fake = FakeAnthropic(
        [
            text_response("first truncated...", stop_reason="max_tokens"),
            text_response("still truncated...", stop_reason="max_tokens"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000,
        log_meta={"agent_id": "blackbird", "phase": "new_post"},
    )

    assert out == "still truncated..."  # best-available text, not swallowed
    assert len(fake.calls) == 2  # one retry only — no second retry added
    assert "still truncated after 2x max_tokens retry" in caplog.text
    assert "agent=blackbird" in caplog.text
    assert "phase=new_post" in caplog.text


async def test_generate_agent_response_empty_content_returns_blank(monkeypatch):
    fake = FakeAnthropic([empty_response()])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_agent_response(
        "sys",
        [{"role": "user", "content": "hi"}],
        log_meta={"agent_id": "su", "phase": "respond"},
    )
    assert out == ""
    assert len(fake.calls) == 1  # empty content is NOT retried


async def test_generate_with_tools_runs_tool_then_returns_final(monkeypatch):
    fake = FakeAnthropic(
        [
            tool_use_response("search_pubmed", {"query": "crispr"}, block_id="toolu_1"),
            text_response("Based on the results, here is my reply."),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    tool_calls = []

    async def executor(name, tool_input):
        tool_calls.append((name, tool_input))
        return "tool output"

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "find papers"}],
        tools=[{"name": "search_pubmed", "input_schema": {}}],
        tool_executor=executor,
    )
    assert out == "Based on the results, here is my reply."
    assert tool_calls == [("search_pubmed", {"query": "crispr"})]
    assert len(fake.calls) == 2  # one tool round + one final-text round


async def _noop_executor(name, tool_input):
    return "unused"


async def test_generate_with_tools_retries_once_on_max_tokens_in_final_branch(monkeypatch):
    """The "no more tool calls" branch's own retry: doubles max_tokens once."""
    fake = FakeAnthropic(
        [
            text_response("truncated...", stop_reason="max_tokens"),
            text_response("full answer"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_executor=_noop_executor,
        max_tokens=1000,
    )
    assert out == "full answer"
    assert len(fake.calls) == 2
    assert fake.calls[1]["max_tokens"] == 2000


async def test_generate_with_tools_calls_on_retry_when_final_branch_retries(monkeypatch):
    """Same call-accounting contract as generate_agent_response's on_retry
    (Finding B0): a retried turn is two real, billed API calls, so a caller
    booking one call per turn against the sliding-window rate limiter needs a
    hook to book the second one too."""
    fake = FakeAnthropic(
        [
            text_response("truncated...", stop_reason="max_tokens"),
            text_response("full answer"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    retry_calls = []
    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_executor=_noop_executor,
        max_tokens=1000,
        on_retry=lambda: retry_calls.append(1),
    )
    assert out == "full answer"
    assert retry_calls == [1]


async def test_generate_with_tools_does_not_call_on_retry_without_truncation(monkeypatch):
    fake = FakeAnthropic([text_response("full answer")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    retry_calls = []
    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_executor=_noop_executor,
        max_tokens=1000,
        on_retry=lambda: retry_calls.append(1),
    )
    assert out == "full answer"
    assert retry_calls == []


async def test_generate_with_tools_logs_loudly_when_final_branch_retry_still_truncates(
    monkeypatch, caplog
):
    """Before this fix this branch already re-checked stop_reason but only
    logged a bare token count via logger.warning — no model/agent/phase
    context, and not loud enough for the one function phase-4 replies (the
    interview itself) actually use (Finding B0)."""
    fake = FakeAnthropic(
        [
            text_response("first truncated...", stop_reason="max_tokens"),
            text_response("still truncated...", stop_reason="max_tokens"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_executor=_noop_executor,
        max_tokens=1000,
        log_meta={"agent_id": "blackbird", "phase": "thread_reply"},
    )

    assert out == "still truncated..."  # best-available text, not swallowed
    assert len(fake.calls) == 2  # one retry only — no second retry added
    assert "still truncated after 2x max_tokens retry" in caplog.text
    assert "agent=blackbird" in caplog.text
    assert "phase=thread_reply" in caplog.text


async def test_generate_with_tools_retries_once_after_exhausting_max_rounds(monkeypatch):
    """The second, previously-inconsistent retry site: the max-tool-rounds
    fallback that forces a final call once the loop never stops requesting
    tools."""
    fake = FakeAnthropic(
        [
            tool_use_response("search_pubmed", {}, block_id="1"),
            tool_use_response("search_pubmed", {}, block_id="2"),
            text_response("truncated...", stop_reason="max_tokens"),
            text_response("full answer"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "search_pubmed", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=1000,
        max_tool_rounds=1,
    )
    assert out == "full answer"
    assert len(fake.calls) == 4  # 2 tool rounds + forced final + its retry
    assert fake.calls[3]["max_tokens"] == 2000


async def test_generate_with_tools_calls_on_retry_after_exhausting_max_rounds(monkeypatch):
    fake = FakeAnthropic(
        [
            tool_use_response("search_pubmed", {}, block_id="1"),
            tool_use_response("search_pubmed", {}, block_id="2"),
            text_response("truncated...", stop_reason="max_tokens"),
            text_response("full answer"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    retry_calls = []
    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "search_pubmed", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=1000,
        max_tool_rounds=1,
        on_retry=lambda: retry_calls.append(1),
    )
    assert out == "full answer"
    assert retry_calls == [1]


async def test_generate_with_tools_logs_loudly_when_exhausted_rounds_retry_still_truncates(
    monkeypatch, caplog
):
    """Before this fix, this retry site never re-checked stop_reason at all —
    a still-truncated response here passed completely silently (Finding B0),
    unlike the other retry site which at least logged a bare warning."""
    fake = FakeAnthropic(
        [
            tool_use_response("search_pubmed", {}, block_id="1"),
            tool_use_response("search_pubmed", {}, block_id="2"),
            text_response("first truncated...", stop_reason="max_tokens"),
            text_response("still truncated...", stop_reason="max_tokens"),
        ]
    )
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "search_pubmed", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=1000,
        max_tool_rounds=1,
        log_meta={"agent_id": "blackbird", "phase": "thread_reply"},
    )

    assert out == "still truncated..."
    assert len(fake.calls) == 4
    assert "still truncated after 2x max_tokens retry" in caplog.text
    assert "agent=blackbird" in caplog.text
    assert "phase=thread_reply" in caplog.text


async def test_make_decision_parses_json_from_response(monkeypatch):
    fake = FakeAnthropic(['{"action": "skip", "reasoning": "no fit"}'])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    decision = await llm.make_decision("sys", [{"role": "user", "content": "decide"}])
    assert decision == {"action": "skip", "reasoning": "no fit"}


# ---------------------------------------------------------------------------
# Opus 5 / Sonnet 5 migration (2026-08-19).
#
# Both models run ADAPTIVE thinking when `thinking` is omitted; the 4.6 pair this
# module was written against ran thinking-OFF on omission. Two consequences, both
# of which ship green without these tests:
#   - `max_tokens` caps thinking + text together, so an implicit switch to
#     thinking-on makes truncation routine (11 retries in one 19-minute run at
#     the old budgets, with thinking already off).
#   - a thinking-enabled reply leads with a ThinkingBlock, which has no `.text`,
#     so `content[0].text` raises AttributeError instead of returning the answer.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_is_disabled_by_default_on_every_call(monkeypatch):
    """A call site that passes no `thinking` must NOT inherit adaptive thinking.

    Defaulted centrally in `_acreate` so a NEW call site cannot acquire
    thinking-on by forgetting the parameter.
    """
    fake = FakeAnthropic(responses=["ok"])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    await llm.generate_agent_response(system_prompt="s", messages=[{"role": "user", "content": "u"}])

    assert fake.calls[0]["thinking"] == {"type": "disabled"}, (
        "omitting `thinking` on Opus 5 / Sonnet 5 runs adaptive thinking, which "
        "shares the max_tokens budget with the answer"
    )


@pytest.mark.asyncio
async def test_the_tools_path_enables_adaptive_thinking(monkeypatch):
    """`generate_with_tools` is the one path that must run thinking ON.

    On Opus 5 a thinking-DISABLED turn can write a tool call into its visible
    text instead of emitting a tool_use block — the turn succeeds, the tool never
    runs, nothing errors. For the hub that silently skips consult_specialist.
    """
    fake = FakeAnthropic(responses=[text_response("done")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    async def _executor(name, params):  # pragma: no cover - not reached
        return "unused"

    await llm.generate_with_tools(
        system_prompt="s",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=_executor,
    )

    assert fake.calls[0]["thinking"] == {"type": "adaptive"}, (
        "the tools path must keep thinking on, or Opus 5 may emit tool calls as text"
    )


@pytest.mark.asyncio
async def test_text_is_extracted_past_a_leading_thinking_block(monkeypatch):
    """The answer must survive a thinking-enabled reply.

    `content[0]` is a ThinkingBlock here. Indexing it for `.text` raises
    AttributeError; filtering by block type returns the answer.
    """
    fake = FakeAnthropic(responses=[thinking_then_text_response("the answer")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    out = await llm.generate_agent_response(
        system_prompt="s", messages=[{"role": "user", "content": "u"}]
    )

    assert out == "the answer"


def test_a_reply_with_only_thinking_yields_empty_string():
    """No text block is 'no answer', not a crash — matching how callers already
    treat an empty response (and what has_usable_content then rejects)."""
    from tests.fakes import _Message, _ThinkingBlock

    msg = _Message(content=[_ThinkingBlock(thinking="thought hard, said nothing")])
    assert llm._first_text(msg) == ""
