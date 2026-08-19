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


# ---------------------------------------------------------------------------
# Cooperative shutdown (2026-08-19).
#
# `request_stop()` only flips a flag; the final DB flush runs in main.py's
# finally, which needs the main loop to RETURN. But one thread_reply turn
# measured up to 134s — generate_with_tools runs up to max_tool_rounds=5, each
# round a real API call. So `docker stop` expired mid-turn and SIGKILLed before
# the flush, losing the in-flight turn's buffered log rows.
#
# The fix bounds the unit of work: once stop is requested, no NEW tool round
# starts. Nothing in flight is aborted, so no call is wasted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_request_prevents_further_tool_rounds(monkeypatch):
    """A stop mid-turn must end the tool loop instead of running all 5 rounds."""
    from tests.fakes import _Message, _ToolUseBlock

    keep_going = True

    def _tool_turn():
        return _Message(
            content=[_ToolUseBlock(id="t1", name="consult_specialist", input={})],
            stop_reason="tool_use",
        )

    # Model the real API: a call WITH tools may ask for another tool round; the
    # forced-final call passes NO tools, so it can only return text. Scripting a
    # flat list instead would let the forced-final call consume a tool turn,
    # which cannot happen in production.
    def _respond(kwargs):
        return _tool_turn() if kwargs.get("tools") else text_response("final answer")

    fake = FakeAnthropic(responses=[_respond] * 12)
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    executed = []

    async def _executor(name, params):
        nonlocal keep_going
        executed.append(name)
        keep_going = False  # stop requested while the first round's tools run
        return "opinion"

    out = await llm.generate_with_tools(
        system_prompt="s",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"name": "consult_specialist", "description": "d",
                "input_schema": {"type": "object"}}],
        tool_executor=_executor,
        should_continue=lambda: keep_going,
    )

    assert len(executed) == 1, "the in-flight round's tools still run — nothing is aborted"
    assert len(fake.calls) == 2, (
        f"expected round 1 + the forced final call, got {len(fake.calls)} API calls — "
        "a stopped turn must not keep opening new tool rounds"
    )
    assert out == "final answer", "the turn must still produce a usable reply"


@pytest.mark.asyncio
async def test_without_a_stop_request_all_rounds_still_run(monkeypatch):
    """The guard must be inert when nothing has asked the engine to stop."""
    from tests.fakes import _Message, _ToolUseBlock

    def _tool_turn():
        return _Message(
            content=[_ToolUseBlock(id="t1", name="consult_specialist", input={})],
            stop_reason="tool_use",
        )

    fake = FakeAnthropic(responses=[_tool_turn(), _tool_turn(), text_response("done")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    async def _executor(name, params):
        return "opinion"

    out = await llm.generate_with_tools(
        system_prompt="s",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"name": "consult_specialist", "description": "d",
                "input_schema": {"type": "object"}}],
        tool_executor=_executor,
        should_continue=lambda: True,
    )
    assert out == "done"
    assert len(fake.calls) == 3, "two tool rounds then the final text turn"


@pytest.mark.asyncio
async def test_should_continue_defaults_to_never_interrupting(monkeypatch):
    """Omitting the predicate must behave exactly as before it existed."""
    from tests.fakes import _Message, _ToolUseBlock

    fake = FakeAnthropic(responses=[
        _Message(content=[_ToolUseBlock(id="t1", name="x", input={})], stop_reason="tool_use"),
        text_response("done"),
    ])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    async def _executor(name, params):
        return "r"

    out = await llm.generate_with_tools(
        system_prompt="s",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"name": "x", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=_executor,
    )
    assert out == "done"


@pytest.mark.asyncio
async def test_round_zero_runs_even_if_stop_was_already_requested(monkeypatch):
    """Round 0 is unconditional — the `round_num > 0` half of the guard.

    If the predicate is already False when the turn starts (a stop requested
    between the engine's own eligibility check and this call), firing the guard
    at round 0 would skip tool use ENTIRELY: the hub would compose its reply
    having consulted nobody, and for a CONCLUDE reply that means a verdict whose
    panel was never convened. Better to run the round that was already committed
    to and stop after it.

    Caught by mutation: dropping `round_num > 0` passed every other test here.
    """
    from tests.fakes import _Message, _ToolUseBlock

    def _respond(kwargs):
        return (
            _Message(content=[_ToolUseBlock(id="t1", name="x", input={})],
                     stop_reason="tool_use")
            if kwargs.get("tools") else text_response("answer")
        )

    fake = FakeAnthropic(responses=[_respond] * 8)
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    executed = []

    async def _executor(name, params):
        executed.append(name)
        return "r"

    out = await llm.generate_with_tools(
        system_prompt="s",
        messages=[{"role": "user", "content": "u"}],
        tools=[{"name": "x", "description": "d", "input_schema": {"type": "object"}}],
        tool_executor=_executor,
        should_continue=lambda: False,  # already stopping before round 0
    )

    assert executed == ["x"], (
        "round 0 must still run its tools — skipping them would let a CONCLUDE "
        "reply be composed with no panel consulted at all"
    )
    assert len(fake.calls) == 2, "round 0 with tools, then the forced final call"
    assert out == "answer"
