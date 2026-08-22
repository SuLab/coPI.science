"""Two blind spots run 8b64a0e0 was measured through, and neither is a behaviour.

**The reply that billed output tokens and carried no text** (RCA: "the two
``end_turn`` empties"). Both turns recovered on retry and cost 6 API calls,
167,894 input tokens and ~141 s — but the *mechanism* is still unresolved after
36 calibration rows and 8 negative controls ruled out summarized thinking and
every accounting explanation. The only surviving hypothesis is a content-block
type ``_all_text`` ignores, and it is inferred: zero such blocks appear in the
5,543 stored ``llm_call_logs`` rows, because nothing has ever recorded what
block types a reply contained. ``call_stats["block_types"]`` settles it the next
time it happens, for free.

**The refusal that left no row at all** (RCA: C5, CONFIRMED). ``generate_agent
_response`` returned "" from its empty-content branch ABOVE the call-log
callback, while ``tools.py``'s ``on_api_call`` had already fired — so 26 billed
refused consults booked a rate-limiter slot in-process and then vanished. It
reconciles three ways: 838 − 812 = 558 − 532 = 26. Both ``api_call_count`` and
the limiter's ``call_times`` ledger are rebuilt from this table on restart, so a
missing row is a throttle slot the next process cannot see.
"""

import logging

import pytest

from src.services import llm
from tests.fakes import (
    FakeAnthropic,
    empty_response,
    text_response,
    thinking_only_response,
    tool_use_response,
)

pytestmark = pytest.mark.asyncio

LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}


@pytest.fixture
def logged():
    """Captures the per-turn dicts src/services/llm.py hands the callback."""
    rows: list[dict] = []
    llm.set_call_log_callback(rows.append)
    yield rows
    llm.set_call_log_callback(None)


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


async def _noop_executor(name, tool_input):
    return "tool output"


# ---------------------------------------------------------------------------
# B1.3 — block_types
# ---------------------------------------------------------------------------


async def test_call_stats_records_the_reply_block_types(monkeypatch, logged):
    """The one line that would have named the mechanism on the day of the run.

    ``content`` is non-empty, so the empty-CONTENT branch never fires; every
    block is of a type ``_all_text`` skips, so the caller gets "" and treats the
    turn as "the model said nothing". Only the block-type list distinguishes
    that from a genuine refusal.
    """
    _install(monkeypatch, FakeAnthropic([thinking_only_response()]))

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    )

    assert out == ""
    (row,) = logged
    assert row["call_stats"][-1]["block_types"] == ["thinking", "redacted_thinking"]
    assert row["call_stats"][-1]["stop_reason"] == "end_turn", (
        "the whole puzzle is that the API called this a normal completion"
    )


async def test_block_types_are_recorded_on_an_ordinary_reply_too(monkeypatch, logged):
    """Not a failure-path field: it is only a baseline if every row has it."""
    _install(monkeypatch, FakeAnthropic([text_response("answer")]))

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    )

    assert logged[0]["call_stats"][0]["block_types"] == ["text"]


async def test_block_types_records_a_tool_round_as_the_mixed_reply_it_is(
    monkeypatch, logged
):
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response("consult_specialist", {}, text="thinking out loud"),
                text_response("the reply"),
            ]
        ),
    )

    await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        log_meta=LOG_META,
    )

    (row,) = logged
    assert [c["block_types"] for c in row["call_stats"]] == [
        ["text", "tool_use"],
        ["text"],
    ]


async def test_block_types_degrades_to_empty_rather_than_raising(monkeypatch, logged):
    """This is a logging path. A stub, or a future SDK that renames ``content``,
    must cost a null field and not a raised exception inside a real agent turn."""

    class _NoContent:
        stop_reason = "end_turn"

        class usage:
            input_tokens = 1
            output_tokens = 2

    assert llm._block_types(_NoContent()) == []
    assert llm._block_types(object()) == []


async def test_the_empty_reply_error_names_the_block_types(caplog):
    """The ERROR is the half an operator reads at 3am; the row is the half an
    analyst reads later. Both need to answer "what WAS in the reply, then?"."""
    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        llm._log_empty_reply(
            thinking_only_response(),
            model="claude-opus-5",
            log_meta=LOG_META,
            where="final",
        )

    (record,) = caplog.records
    assert "redacted_thinking" in record.getMessage()


# ---------------------------------------------------------------------------
# B1.4 — the row that was never written
# ---------------------------------------------------------------------------


async def test_an_empty_content_reply_still_writes_a_call_log_row(
    monkeypatch, logged
):
    """C5: the early ``return ""`` sat ABOVE the callback, so 26 billed calls
    this run left no row — and the restart path rebuilds both
    ``api_call_count`` and the limiter's ``call_times`` from row counts."""
    _install(monkeypatch, FakeAnthropic([empty_response(stop_reason="refusal")]))

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    )

    assert out == ""
    assert len(logged) == 1, "one billed call, one row — no more, no fewer"
    (row,) = logged
    assert row["response_text"] == ""
    assert row["call_stats"][0]["stop_reason"] == "refusal"
    assert row["call_stats"][0]["block_types"] == []
    # The billing columns were already read off `message.usage` before the
    # branch; the point of the row is that they now reach the table.
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20


async def test_an_empty_content_reply_logs_exactly_one_error(monkeypatch, caplog):
    """Trap (a) on B1.4, and a regression guard rather than a red-first test:
    the rich ``user_tail`` diagnostic and ``_log_empty_reply`` must not both
    fire for one reply. One billed call deserves one ERROR line."""
    _install(monkeypatch, FakeAnthropic([empty_response(stop_reason="refusal")]))

    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        await llm.generate_agent_response(
            "sys",
            [{"role": "user", "content": "the pitch, all 400 characters of it"}],
            log_meta=LOG_META,
        )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    message = errors[0].getMessage()
    # Trap (b): the empty-content branch is the ONLY place `user_tail` is
    # captured, and it is what tells an operator which prompt did this.
    assert "all 400 characters of it" in message
    assert "refusal" in message


async def test_an_empty_content_reply_with_no_log_meta_writes_nothing(
    monkeypatch, logged
):
    """``if _call_log_callback and log_meta`` is unchanged: an uninstrumented
    call site (every consult before this run) still writes no row, so this fix
    cannot invent rows for callers that never asked for them."""
    _install(monkeypatch, FakeAnthropic([empty_response(stop_reason="refusal")]))

    out = await llm.generate_agent_response("sys", [{"role": "user", "content": "hi"}])

    assert out == ""
    assert logged == []
