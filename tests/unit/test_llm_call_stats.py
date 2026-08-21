"""`llm_call_logs.call_stats`: one entry per REAL API call, in call order.

One logged row is one TURN, and a turn is 1..7 API calls — up to
``max_tool_rounds`` tool rounds, a terminating or forced-final call, and at most
one ``max_tokens`` retry. The row's ``input_tokens``/``output_tokens``/
``latency_ms`` are sums over all of them, so on their own they cannot answer the
question the table gets asked during an incident: *which* call truncated, and how
many tokens did the model actually want? Measured: 78.6% of production
``thread_reply`` rows were multi-call, ``stop_reason`` was read in four places in
src/services/llm.py and logged in none, and the requested ``max_tokens`` ceiling
was recorded nowhere at all — so sizing the thread_reply ceiling had to be done by
inference and got the truncation count wrong by 2 of 9 events.

Two of the cases below had NO trace whatsoever before this change:
``test_a_truncating_tool_round_is_recorded_instead_of_vanishing`` (``stop_reason``
was only inspected inside the ``not tool_use_blocks`` branch, so a truncating tool
round produced no log line and no row content), and
``test_a_retried_turn_bills_the_sum_of_both_calls`` (``message = retry_msg``
made the row carry ONLY the retry's tokens — a billing undercount of one whole
call on exactly the turns most likely to retry).
"""

import pytest

from src.services import llm
from tests.fakes import (
    FakeAnthropic,
    _OutputTokensDetails,
    _Usage,
    text_response,
    tool_use_response,
)

pytestmark = pytest.mark.asyncio

# The callback only fires when log_meta is non-empty (llm.py: `if
# _call_log_callback and log_meta`), so every test here has to pass one.
LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}


@pytest.fixture
def logged(monkeypatch):
    """Captures the per-turn dicts src/services/llm.py hands the callback."""
    rows: list[dict] = []
    llm.set_call_log_callback(rows.append)
    yield rows
    llm.set_call_log_callback(None)


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


async def _noop_executor(name, tool_input):
    return "tool output"


def _kinds(row: dict) -> list[str]:
    return [c["kind"] for c in row["call_stats"]]


# ---------------------------------------------------------------------------
# generate_agent_response
# ---------------------------------------------------------------------------


async def test_a_single_call_turn_records_one_entry_with_its_stop_reason_and_ceiling(
    monkeypatch, logged
):
    _install(monkeypatch, FakeAnthropic([text_response("answer")]))

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1234, log_meta=LOG_META
    )

    (row,) = logged
    (entry,) = row["call_stats"]
    assert entry["seq"] == 1
    assert entry["kind"] == "final"
    assert entry["stop_reason"] == "end_turn"
    # The ceiling actually REQUESTED. Without it, stop_reason='max_tokens' says
    # the model wanted more but not how much it was allowed, which is exactly
    # half of a sizing decision.
    assert entry["max_tokens"] == 1234
    assert entry["input_tokens"] == 10
    assert entry["output_tokens"] == 20
    assert entry["latency_ms"] >= 0


async def test_a_retried_turn_records_the_capped_call_and_the_retry_in_order(
    monkeypatch, logged
):
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response("truncated...", stop_reason="max_tokens"),
                text_response("full answer"),
            ]
        ),
    )

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000, log_meta=LOG_META
    )

    (row,) = logged
    assert _kinds(row) == ["final", "retry"]
    assert [c["seq"] for c in row["call_stats"]] == [1, 2]
    first, retry = row["call_stats"]
    assert first["stop_reason"] == "max_tokens"
    assert first["max_tokens"] == 1000
    # The retry's own ceiling, not the original: "we doubled it and it still
    # truncated" is a different finding from "it truncated at 1000".
    assert retry["max_tokens"] == 2000
    assert retry["stop_reason"] == "end_turn"


async def test_a_retried_turn_bills_the_sum_of_both_calls(monkeypatch, logged):
    """Pins the item-4 fix: `message = retry_msg  # use retry stats for logging`.

    That line made the logged row carry ONLY the retry's tokens. The first call
    was real and billed even though its truncated text was discarded, so every
    retried turn under-reported input AND output tokens by a whole call — while
    `latency_ms` was summed, so one row disagreed with itself about how many
    calls it represented.
    """
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response(
                    "truncated...",
                    stop_reason="max_tokens",
                    usage=_Usage(input_tokens=100, output_tokens=900),
                ),
                text_response(
                    "full answer", usage=_Usage(input_tokens=100, output_tokens=1500)
                ),
            ]
        ),
    )

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000, log_meta=LOG_META
    )

    (row,) = logged
    assert row["input_tokens"] == 200, "both calls sent the prompt; both were billed"
    assert row["output_tokens"] == 2400
    # And the split is still recoverable, which is the whole point of not having
    # picked one call's numbers in the first place.
    assert [c["output_tokens"] for c in row["call_stats"]] == [900, 1500]


# ---------------------------------------------------------------------------
# generate_with_tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_rounds", [1, 2, 3])
async def test_n_tool_rounds_plus_a_final_call_produce_n_plus_one_entries(
    monkeypatch, logged, n_rounds
):
    responses = [
        tool_use_response("consult_specialist", {}, block_id=f"t{i}")
        for i in range(n_rounds)
    ]
    responses.append(text_response("the reply"))
    _install(monkeypatch, FakeAnthropic(responses))

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=16000,
        max_tool_rounds=5,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert _kinds(row) == ["round"] * n_rounds + ["final"]
    assert [c["seq"] for c in row["call_stats"]] == list(range(1, n_rounds + 2))
    assert all(c["max_tokens"] == 16000 for c in row["call_stats"])
    # The cumulative columns stay cumulative — unchanged semantics, item 5.
    assert row["output_tokens"] == 20 * (n_rounds + 1)


async def test_a_truncating_tool_round_is_recorded_instead_of_vanishing(
    monkeypatch, logged
):
    """The case that left NO trace at all before this change.

    `stop_reason` was inspected only inside the `not tool_use_blocks` branch, so
    a round that emitted tool_use blocks AND hit the ceiling produced no warning,
    no retry and nothing in the row. On the hub that is the expensive kind of
    truncation: the round is thinking-enabled and thinking shares max_tokens with
    the text, so the round most likely to truncate is the one that consulted the
    panel.
    """
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response(
                    "consult_specialist",
                    {},
                    stop_reason="max_tokens",
                    usage=_Usage(input_tokens=12000, output_tokens=16000),
                ),
                text_response("the reply"),
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=16000,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert _kinds(row) == ["round", "final"]
    truncated = row["call_stats"][0]
    assert truncated["stop_reason"] == "max_tokens"
    assert truncated["output_tokens"] == 16000
    assert truncated["max_tokens"] == 16000, (
        "the round hit exactly the ceiling it was given — the pair of numbers "
        "that makes a ceiling decision measurable rather than inferred"
    )


async def test_the_forced_final_call_after_max_rounds_is_labelled_forced_final(
    monkeypatch, logged
):
    """`max_tool_rounds=1` allows loop iterations 0 AND 1 (`range(n + 1)`), so it
    takes TWO tool-requesting replies to exhaust the budget and fall through to
    the forced final call — the same scripting as
    test_generate_with_tools_retries_once_after_exhausting_max_rounds."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response("consult_specialist", {}, block_id="t1"),
                tool_use_response("consult_specialist", {}, block_id="t2"),
                text_response("forced answer"),
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=1000,
        max_tool_rounds=1,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert _kinds(row) == ["round", "round", "forced_final"], (
        "a turn that spent its whole tool budget before writing an answer must be "
        "distinguishable from one that finished on its own"
    )


async def test_the_forced_final_retry_follows_the_call_it_retries(monkeypatch, logged):
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response("consult_specialist", {}, block_id="t1"),
                tool_use_response("consult_specialist", {}, block_id="t2"),
                text_response("truncated...", stop_reason="max_tokens"),
                text_response("full answer"),
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=1000,
        max_tool_rounds=1,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert _kinds(row) == ["round", "round", "forced_final", "retry"]
    assert [c["max_tokens"] for c in row["call_stats"]] == [1000, 1000, 1000, 2000]


async def test_the_final_branch_retry_follows_the_call_it_retries(monkeypatch, logged):
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response("truncated...", stop_reason="max_tokens"),
                text_response("full answer"),
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_executor=_noop_executor,
        max_tokens=1000,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert _kinds(row) == ["final", "retry"]


# ---------------------------------------------------------------------------
# thinking tokens
# ---------------------------------------------------------------------------


async def test_thinking_tokens_are_recorded_when_the_sdk_reports_them(
    monkeypatch, logged
):
    """`generate_with_tools` is the one call site running ADAPTIVE thinking, and
    thinking shares `max_tokens` with the text. So on the path that truncates,
    `output_tokens` alone cannot distinguish a long answer from a long
    deliberation that left no room for one — measurement put ~55-65% of output
    tokens in invisible thinking. The SDK reports the split and llm.py discarded
    it."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response(
                    "answer",
                    usage=_Usage(
                        input_tokens=1000,
                        output_tokens=4000,
                        output_tokens_details=_OutputTokensDetails(
                            thinking_tokens=2400
                        ),
                    ),
                )
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        tool_executor=_noop_executor,
        log_meta=LOG_META,
    )

    (entry,) = logged[0]["call_stats"]
    assert entry["thinking_tokens"] == 2400
    assert entry["output_tokens"] == 4000, "thinking is INSIDE output_tokens, not extra"


async def test_thinking_tokens_are_null_rather_than_fatal_when_unreported(
    monkeypatch, logged
):
    """An older SDK, a stub, or a reply the API did not decompose. A missing
    OBSERVABILITY field must degrade to null, never raise inside the logging path
    of a real agent turn."""
    _install(monkeypatch, FakeAnthropic([text_response("answer")]))

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    )

    (entry,) = logged[0]["call_stats"]
    assert entry["thinking_tokens"] is None


async def test_thinking_tokens_survive_a_usage_object_without_the_attribute_at_all(
    monkeypatch, logged
):
    """Belt and braces on the first of the two getattrs: a usage object that
    predates `output_tokens_details` entirely, which is what any pinned-older
    anthropic install presents."""

    class _OldUsage:
        input_tokens = 5
        output_tokens = 7

    msg = text_response("answer")
    msg.usage = _OldUsage()
    _install(monkeypatch, FakeAnthropic([msg]))

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    )

    (entry,) = logged[0]["call_stats"]
    assert entry["thinking_tokens"] is None
    assert (entry["input_tokens"], entry["output_tokens"]) == (5, 7)


# ---------------------------------------------------------------------------
# non-breaking
# ---------------------------------------------------------------------------


async def test_no_callback_and_no_log_meta_still_returns_the_answer(monkeypatch):
    """call_stats is built unconditionally; the callback is not. A turn with no
    log_meta must be exactly as it was — this is every consult and every
    non-instrumented call site."""
    _install(monkeypatch, FakeAnthropic([text_response("answer")]))

    out = await llm.generate_agent_response("sys", [{"role": "user", "content": "hi"}])
    assert out == "answer"


async def test_the_cumulative_columns_and_the_entries_agree_on_a_multi_call_turn(
    monkeypatch, logged
):
    """The invariant a reader can rely on: the row's totals are the sums of its
    own entries. If these ever diverge, one of the two is being maintained and
    the other is not."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response(
                    "consult_specialist",
                    {},
                    block_id="t1",
                    usage=_Usage(input_tokens=11, output_tokens=22),
                ),
                text_response(
                    "truncated", stop_reason="max_tokens",
                    usage=_Usage(input_tokens=33, output_tokens=44),
                ),
                text_response(
                    "done", usage=_Usage(input_tokens=55, output_tokens=66)
                ),
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=1000,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert _kinds(row) == ["round", "final", "retry"]
    assert row["input_tokens"] == sum(c["input_tokens"] for c in row["call_stats"])
    assert row["output_tokens"] == sum(c["output_tokens"] for c in row["call_stats"])
