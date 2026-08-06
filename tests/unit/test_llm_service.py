"""Pins src/services/llm.py orchestration that was previously uncovered:
max-tokens retry, empty-content handling, and the tool-use loop. FakeAnthropic
scripts the model responses; get_anthropic_client is the monkeypatch seam.
"""

import pytest

from src.services import llm
from tests.fakes import FakeAnthropic, empty_response, text_response, tool_use_response


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


async def test_make_decision_parses_json_from_response(monkeypatch):
    fake = FakeAnthropic(['{"action": "skip", "reasoning": "no fit"}'])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    decision = await llm.make_decision("sys", [{"role": "user", "content": "decide"}])
    assert decision == {"action": "skip", "reasoning": "no fit"}
