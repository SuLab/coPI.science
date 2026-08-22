"""What a RAISING call inside `generate_with_tools` must not destroy.

Two things vanished together whenever anything after the first API call threw:

  * **the answer** — the truncation retries assign `response_text` from the
    first pass and then overwrite it from the retry, so an exception in the
    retry unwound past a truncated-but-usable reply that was already in hand and
    already paid for;
  * **the record of every billed call in the turn** — `_emit_call_log` is only
    reached on the normal return paths, so a turn that made six real API calls
    and then threw wrote `rows written: 0`. `SimulationEngine` rebuilds
    `api_call_count` and the rate limiter's `call_times` ledger from that table,
    so those six calls stopped existing at the next restart.

The guard is one wrapper around the function's whole body rather than four
patches at the retry sites, because the sites that lose the same record are not
only the retries: the tool-round call itself, `_execute_tool_blocks`, and
`b.model_dump()` are each capable of throwing after a round has been billed.

What the guard does NOT do is turn every failure into a silent "": a turn where
nothing has succeeded yet still raises, so a mis-sized `max_tokens` (the one
error this module raises by name) stays as loud as it was.
"""

import pytest

from src.services import llm
from tests.fakes import (
    FakeAnthropic,
    _Usage,
    text_response,
    tool_use_response,
)

pytestmark = pytest.mark.asyncio

LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}

TOOLS = [{"name": "consult_specialist", "input_schema": {}}]


@pytest.fixture
def logged():
    rows: list[dict] = []
    llm.set_call_log_callback(rows.append)
    yield rows
    llm.set_call_log_callback(None)


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


async def _noop_executor(name, tool_input):
    return "tool output"


def _boom(_kwargs):
    """A response script entry that fails the way a real request does."""
    raise RuntimeError("connection reset by peer")


def _kinds(row: dict) -> list[str]:
    return [c["kind"] for c in row["call_stats"]]


# ---------------------------------------------------------------------------
# the predicate later tasks import
# ---------------------------------------------------------------------------


async def test_is_truncated_stop_covers_refusal_and_max_tokens():
    """One definition, because the two callers that need it need the SAME one.

    `refusal` is the classifier cutting the generation and `max_tokens` is the
    ceiling doing it; both mean the text in hand is incomplete. A `refusal`-only
    test misses every turn that truncated, and misses a fallthrough from the
    retry path — which reports `max_tokens` even when the first pass was
    refused, because the message that ended the turn is the truncated one.
    """
    assert llm.is_truncated_stop("refusal") is True
    assert llm.is_truncated_stop("max_tokens") is True
    for complete in ("end_turn", "stop_sequence", "tool_use", "", None):
        assert llm.is_truncated_stop(complete) is False


# ---------------------------------------------------------------------------
# the answer survives
# ---------------------------------------------------------------------------


async def test_a_raising_retry_returns_the_first_pass_text(monkeypatch, logged):
    """The truncated first pass is the best answer available, and it is billed.

    Losing it costs a whole concluding turn: at the 16000-token thread_reply
    ceiling the retry is the call most likely to fail (it asks for 21_333 and
    can take ~351 s against a 300 s read timeout), and the reply it discards is
    the one carrying the verdict.
    """
    stops: list[str] = []
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response("half a verdict", stop_reason="max_tokens"),
                _boom,
            ]
        ),
    )

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        tool_executor=_noop_executor,
        max_tokens=1000,
        log_meta=LOG_META,
        on_stop_reason=stops.append,
    )

    assert out == "half a verdict"
    # `max_tokens`, not `refusal` and not `end_turn`: the message that ended
    # this turn IS the truncated one, because the retry never produced another.
    # This is why the caller-side predicate has to be `is_truncated_stop`.
    assert stops == ["max_tokens"]
    assert llm.is_truncated_stop(stops[0]) is True
    (row,) = logged
    assert row["response_text"] == "half a verdict"
    assert _kinds(row) == ["final"], "the retry never returned, so it never happened"


async def test_a_raising_forced_final_does_not_double_count_the_last_round(
    monkeypatch, logged
):
    """The trap in the obvious fix.

    At the forced-final call `response_text` is not bound yet and `message`
    still points at the LAST TOOL ROUND — so "fall through with what we have"
    would add that round's tokens a second time and label the entry
    `forced_final`, inventing an API call that never happened. The row must
    carry the two rounds that really ran, once each, and the turn returns "".
    """
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response(
                    "consult_specialist", {}, block_id="t1",
                    usage=_Usage(input_tokens=11, output_tokens=22),
                ),
                tool_use_response(
                    "consult_specialist", {}, block_id="t2",
                    usage=_Usage(input_tokens=33, output_tokens=44),
                ),
                _boom,
            ]
        ),
    )

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=TOOLS,
        tool_executor=_noop_executor,
        max_tokens=1000,
        max_tool_rounds=1,
        log_meta=LOG_META,
    )

    assert out == ""
    (row,) = logged
    assert _kinds(row) == ["round", "round"], "no phantom forced_final entry"
    assert row["input_tokens"] == 44
    assert row["output_tokens"] == 66
    assert row["response_text"] == ""


# ---------------------------------------------------------------------------
# the record survives
# ---------------------------------------------------------------------------


async def test_a_raising_call_still_writes_its_call_log_row(monkeypatch, logged):
    """A tool round is billed the moment it returns. If the NEXT call throws,
    the round still happened — and the row is the only durable trace of it, for
    the rate limiter as much as for the audit."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response(
                    "consult_specialist", {}, block_id="t1",
                    usage=_Usage(input_tokens=11, output_tokens=22),
                ),
                _boom,
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="connection reset"):
        await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS,
            tool_executor=_noop_executor,
            max_tokens=1000,
            log_meta=LOG_META,
        )

    (row,) = logged
    assert _kinds(row) == ["round"]
    assert (row["input_tokens"], row["output_tokens"]) == (11, 22)


async def test_a_raising_tool_executor_still_writes_the_round_it_billed(
    monkeypatch, logged
):
    """`_execute_tool_blocks` is one of the sites a per-call patch would miss,
    and it raises AFTER its round has been paid for. The exception still
    propagates — `execute_tool` catches everything in production, so a raise
    here is a bug the caller must see — but the round is recorded first."""
    _install(
        monkeypatch,
        FakeAnthropic([tool_use_response("consult_specialist", {}, block_id="t1")]),
    )

    async def _exploding_executor(name, tool_input):
        raise RuntimeError("specialist exploded")

    with pytest.raises(RuntimeError, match="specialist exploded"):
        await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS,
            tool_executor=_exploding_executor,
            log_meta=LOG_META,
        )

    (row,) = logged
    assert _kinds(row) == ["round"]


async def test_a_call_that_never_reached_the_api_writes_no_row(monkeypatch, logged):
    """The ONE exception to "a failed turn still writes its row", and it is
    narrow by construction: `_acreate`'s pre-flight check raises before any HTTP
    request is made, so nothing was sent and nothing was billed. A row here would
    book a rate-limiter slot after the next restart for a call that never
    existed.

    The suppression is keyed on the exception TYPE rather than on an empty
    `call_stats`, because "no call completed" and "no call was issued" are
    different facts and only the second one justifies silence — a request that
    went out and then timed out has no `usage` either.
    """
    fake = FakeAnthropic([text_response("never reached")])
    _install(monkeypatch, fake)

    assert issubclass(llm.NonStreamingMaxTokensError, ValueError), (
        "call sites catch ValueError; narrowing the type must not narrow what "
        "they catch"
    )
    with pytest.raises(llm.NonStreamingMaxTokensError, match="NONSTREAMING_MAX_TOKENS"):
        await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS,
            tool_executor=_noop_executor,
            max_tokens=llm.NONSTREAMING_MAX_TOKENS + 1,
            log_meta=LOG_META,
        )

    assert fake.calls == []
    assert logged == []


async def test_a_first_round_failure_after_the_request_went_out_still_writes_a_row(
    monkeypatch, logged
):
    """A request that WAS sent and then failed is a billed generation.

    The distinction that matters is "did anything go out", not "did anything come
    back": a 300 s `APITimeoutError` on the first round leaves no `usage` to
    record, but the call happened, was paid for, and — since `SimulationEngine`
    rebuilds `api_call_count` and the limiter's `call_times` from these rows —
    disappears from the ledger entirely if this writes nothing. An empty
    `call_stats` on the row is the honest report: the turn is recorded, no call
    completed.
    """
    fake = FakeAnthropic([_boom])
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="connection reset"):
        await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=TOOLS,
            tool_executor=_noop_executor,
            log_meta=LOG_META,
        )

    assert len(fake.calls) == 1, "the request went out — that is the premise"
    (row,) = logged
    assert row["call_stats"] == []
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)


async def test_generate_agent_response_writes_a_row_for_a_request_that_went_out(
    monkeypatch, logged
):
    """The same rule in the other function — the higher-traffic one. Every
    specialist consult, every memory write and every phase-1 decision goes
    through `generate_agent_response`."""
    fake = FakeAnthropic([_boom])
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="connection reset"):
        await llm.generate_agent_response(
            "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
        )

    assert len(fake.calls) == 1
    (row,) = logged
    assert row["call_stats"] == []


async def test_a_raising_retry_in_generate_agent_response_still_writes_its_row(
    monkeypatch, logged
):
    """The retry site a `generate_with_tools` guard cannot reach.

    Both calls are billed; the first one's tokens and its truncated text are the
    only evidence the turn happened. The exception STILL propagates, and the
    truncated text is deliberately NOT returned to the caller here — see the
    comment at that site: `src/agent/tools.py` still tests
    `stop_reasons[-1] == "refusal"`, so a fallthrough reporting `max_tokens`
    would be credited to the specialist panel as a complete opinion.
    """
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response(
                    "half an opinion",
                    stop_reason="max_tokens",
                    usage=_Usage(input_tokens=11, output_tokens=22),
                ),
                _boom,
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="connection reset"):
        await llm.generate_agent_response(
            "sys", [{"role": "user", "content": "hi"}], max_tokens=1000,
            log_meta=LOG_META,
        )

    assert len(logged) == 1, "one turn, one row — covering BOTH billed calls"
    (row,) = logged
    assert _kinds(row) == ["final"], "the retry never returned, so it never happened"
    assert (row["input_tokens"], row["output_tokens"]) == (11, 22)
    # The ROW carries the truncated text even though the CALLER does not get it:
    # `llm_call_logs.response_text` is what the dropped-verdict backfill regexes
    # for `<assessment_json>`, and this is a reply that was paid for.
    assert row["response_text"] == "half an opinion"
