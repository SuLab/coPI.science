"""``on_stop_reason``: the call site gets to see WHY the model stopped.

Run 8b64a0e0, finding H4 (CONFIRMED and stronger): ``stop_reason`` is compared
against ``"max_tokens"`` at nine sites in ``llm.py`` and branched on **nowhere
else in ``src/``**. Everything else — ``refusal`` above all — arrives at the
caller as an ordinary string, indistinguishable from a complete answer. The
measured cost of that in one 19-minute run:

- all 4 truncated hub replies reached Slack (joined on the last 60 characters of
  each row against ``agent_messages.content`` — 4 of 4 exact matches), so the
  truncation *is* the end of the posted message;
- klein's working-memory write persisted **twice** — a ``profile_revisions`` row
  1.6 ms later and ``profiles/memory/klein/public.md`` on disk — replacing a
  complete 1,977-char memory with a 1,437-char refusal-truncated one;
- 3 truncated consults were published to the interview thread as
  ``⚠️ caution`` and fed verbatim back into the hub's next prompt.

The decision about a partial answer belongs at the call site (post it? refuse to
persist it? don't credit it to the panel?), so this layer's job is only to
*report*, and to keep returning the partial text it always has. Consumed by
``src/agent/tools.py`` (C1.3: a refusal-truncated consult is recorded but not
credited).
"""

import logging

import pytest

from src.services import llm
from tests.fakes import (
    FakeAnthropic,
    multi_text_response,
    text_response,
    tool_use_response,
)

pytestmark = pytest.mark.asyncio

LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


async def _noop_executor(name, tool_input):
    return "tool output"


class _Recorder:
    """A callback that records every stop_reason handed to it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, stop_reason: str) -> None:
        self.seen.append(stop_reason)


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------


async def test_on_stop_reason_reports_a_refusal_that_kept_partial_text(monkeypatch):
    """The klein case: a refusal that still carried usable-looking text.

    Both halves matter. The callback must say ``refusal`` — and the partial text
    must still come back, because "persist it anyway" is a legitimate answer for
    a Slack reply and a wrong one for a working-memory overwrite. Only the
    caller knows which it is.
    """
    _install(monkeypatch, FakeAnthropic([text_response("half a memory", stop_reason="refusal")]))
    seen = _Recorder()

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}],
        log_meta=LOG_META, on_stop_reason=seen,
    )

    assert out == "half a memory"
    assert seen.seen == ["refusal"]


async def test_on_stop_reason_fires_exactly_once_on_a_multi_round_turn(monkeypatch):
    """Once per CALL of this function, not once per API call: the contract is
    "the final call's stop_reason", so a 2-round turn reports one value and it
    is the terminating call's, not a round's."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response("consult_specialist", {}, block_id="t1"),
                tool_use_response("consult_specialist", {}, block_id="t2"),
                text_response("the reply", stop_reason="refusal"),
            ]
        ),
    )
    seen = _Recorder()

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        log_meta=LOG_META,
        on_stop_reason=seen,
    )

    assert out == "the reply"
    assert seen.seen == ["refusal"]


async def test_on_stop_reason_reports_the_retrys_outcome_not_the_truncation(
    monkeypatch
):
    """A turn that truncated and then recovered is not a truncated turn. The
    retry is the final call, so ``end_turn`` is the honest report — otherwise
    C1.3 would refuse to credit consults that came back complete."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response("half", stop_reason="max_tokens"),
                text_response("the whole thing"),
            ]
        ),
    )
    seen = _Recorder()

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000,
        log_meta=LOG_META, on_stop_reason=seen,
    )

    assert out == "the whole thing"
    assert seen.seen == ["end_turn"]


async def test_on_stop_reason_reports_a_still_truncated_retry(monkeypatch):
    """The other half of the same coin: doubled and STILL truncated. This is the
    one the hub's concluding reply cares about — the sidecar is emitted last."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                text_response("half", stop_reason="max_tokens"),
                text_response("still half", stop_reason="max_tokens"),
            ]
        ),
    )
    seen = _Recorder()

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=1000,
        log_meta=LOG_META, on_stop_reason=seen,
    )

    assert seen.seen == ["max_tokens"]


async def test_on_stop_reason_fires_for_the_forced_final_call(monkeypatch):
    """``max_tool_rounds=1`` allows loop iterations 0 AND 1 (``range(n + 1)``),
    so two tool-requesting replies exhaust the budget and the no-tools forced
    final call is the one whose stop_reason is final."""
    _install(
        monkeypatch,
        FakeAnthropic(
            [
                tool_use_response("consult_specialist", {}, block_id="t1"),
                tool_use_response("consult_specialist", {}, block_id="t2"),
                text_response("forced answer", stop_reason="refusal"),
            ]
        ),
    )
    seen = _Recorder()

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tool_rounds=1,
        log_meta=LOG_META,
        on_stop_reason=seen,
    )

    assert out == "forced answer"
    assert seen.seen == ["refusal"]


async def test_on_stop_reason_fires_on_the_empty_content_early_return(monkeypatch):
    """The C5 path. A reply with no content at all is precisely the case a call
    site most needs told about, and it returns from a different place."""
    _install(monkeypatch, FakeAnthropic([multi_text_response(stop_reason="refusal")]))
    seen = _Recorder()

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}],
        log_meta=LOG_META, on_stop_reason=seen,
    )

    assert out == ""
    assert seen.seen == ["refusal"]


# ---------------------------------------------------------------------------
# it can never make things worse
# ---------------------------------------------------------------------------


async def test_a_raising_callback_does_not_cost_the_reply(monkeypatch, caplog):
    """Observability must never become a failure mode. A call site whose hook
    throws still gets its text — anything else would make this keyword strictly
    more dangerous than the silence it replaces."""
    _install(monkeypatch, FakeAnthropic([text_response("the reply")]))

    def _boom(stop_reason: str) -> None:
        raise RuntimeError("call site bug")

    with caplog.at_level(logging.ERROR, logger="src.services.llm"):
        out = await llm.generate_agent_response(
            "sys", [{"role": "user", "content": "hi"}],
            log_meta=LOG_META, on_stop_reason=_boom,
        )

    assert out == "the reply"
    assert any("on_stop_reason" in r.getMessage() for r in caplog.records), (
        "swallowed silently is not the same as swallowed safely"
    )


async def test_omitting_the_keyword_changes_nothing(monkeypatch):
    """Purely additive: every existing call site passes nothing here."""
    _install(monkeypatch, FakeAnthropic([text_response("the reply")]))

    assert await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], log_meta=LOG_META
    ) == "the reply"


async def test_a_missing_stop_reason_is_reported_as_the_empty_string(monkeypatch):
    """The interface is ``Callable[[str], None]`` — a stub or a future SDK that
    omits ``stop_reason`` must not hand the caller ``None`` to compare against
    ``"refusal"``."""

    class _NoStopReason:
        content: list = []

        class usage:
            input_tokens = 1
            output_tokens = 2

    _install(monkeypatch, FakeAnthropic([_NoStopReason()]))
    seen = _Recorder()

    await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}],
        log_meta=LOG_META, on_stop_reason=seen,
    )

    assert seen.seen == [""]
