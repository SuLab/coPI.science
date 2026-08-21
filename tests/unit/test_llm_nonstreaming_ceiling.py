"""The SDK's non-streaming `max_tokens` ceiling, and the retry that used to cross it.

anthropic's ``BaseClient._calculate_nonstreaming_timeout`` refuses a
non-streaming request whose ``max_tokens`` implies more than 10 minutes of
generation: ``expected_time = 3600 * max_tokens / 128_000``, raise once that
passes 600 seconds. The last accepted value is 21_333. Nothing is sent — the
ValueError is local.

Every truncation retry in src/services/llm.py asked for ``max_tokens * 2``, which
was harmless while the thread_reply ceiling was 4000 (retry: 8000) and fatal once
d23ccec raised it to 16000 (retry: 32000). The failure lands in the worst possible
place: the first reply has already truncated and been discarded, so the ValueError
propagates out through ``_reply_to_thread``'s broad handler with nothing posted, no
verdict written, no drop row, the specialist consults orphaned — and the turn is
retried and paid for again.

CI could not see any of this, which is the durable half of the fix. The suite
drives ``FakeAnthropic`` and never reaches the real client, so the fake now
enforces the same limit (tests/fakes.py, ``_MAX_NONSTREAMING_MAX_TOKENS``,
re-derived from the SDK's arithmetic rather than imported from src).
"""

import ast
from pathlib import Path

import pytest

from src.services import llm
from tests.fakes import (
    _MAX_NONSTREAMING_MAX_TOKENS,
    FakeAnthropic,
    text_response,
    tool_use_response,
)

pytestmark = pytest.mark.asyncio

LOG_META = {"agent_id": "blackbird", "phase": "thread_reply"}

# What src/agent/simulation.py's phase-4 call actually passes. Hard-coded rather
# than imported: this test exists to prove THAT configuration survives a
# truncation, so it has to fail if the call site drifts away from it.
THREAD_REPLY_MAX_TOKENS = 16000


def _install(monkeypatch, fake):
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)


async def _noop_executor(name, tool_input):
    return "tool output"


def _retry_call(fake):
    """The second request the fake received (the retry), or None."""
    return fake.calls[1] if len(fake.calls) > 1 else None


# ---------------------------------------------------------------------------
# the constant
# ---------------------------------------------------------------------------


async def test_the_constant_is_the_sdk_formula_not_a_round_number():
    assert llm.NONSTREAMING_MAX_TOKENS == int(600 * 128_000 / 3600) == 21_333
    assert llm.NONSTREAMING_MAX_TOKENS == _MAX_NONSTREAMING_MAX_TOKENS, (
        "src and the fake must agree, or the fake stops being a guard: the whole "
        "point is that CI refuses what the real client refuses"
    )


async def test_the_installed_sdk_draws_the_line_exactly_where_we_do():
    """Checked against the real SDK, offline — no HTTP, no API key that works.

    The suite runs against anthropic 0.120.2 (.venv-test) while the deployed
    agent image resolved 1.0.0, and both carry this guard with the same
    arithmetic; probed on both, highest accepted 21_333. This asserts it on
    whichever one is installed rather than trusting the comment.
    """
    import anthropic

    client = anthropic.Anthropic(api_key="sk-ant-not-a-real-key")
    calc = getattr(client, "_calculate_nonstreaming_timeout", None)
    if calc is None:
        pytest.skip(
            "anthropic dropped/renamed _calculate_nonstreaming_timeout — "
            "re-derive llm.NONSTREAMING_MAX_TOKENS against the new SDK before "
            "deleting this test"
        )

    calc(llm.NONSTREAMING_MAX_TOKENS, None)  # accepted: must not raise
    with pytest.raises(ValueError, match="Streaming is required"):
        calc(llm.NONSTREAMING_MAX_TOKENS + 1, None)


async def test_no_call_site_in_src_asks_for_more_than_the_ceiling():
    """A literal `max_tokens=N` above the limit is a call that can never succeed.

    Cheap static sweep, deliberately not a mock: the guard in ``_acreate`` only
    fires when the call is actually made, and the call sites that matter most
    (phase-4 thread_reply, phase-5, a consult) are the ones a unit test is least
    likely to reach on the day someone raises one.

    Over the AST rather than a regex over lines, because this module's own
    comments quote the numbers involved — a text scan flags the explanation of
    the bug as an instance of it.
    """
    src = Path(__file__).resolve().parents[2] / "src"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "max_tokens" or not isinstance(kw.value, ast.Constant):
                    continue
                value = kw.value.value
                if isinstance(value, int) and value > llm.NONSTREAMING_MAX_TOKENS:
                    offenders.append(
                        f"{path.name}:{kw.value.lineno}: max_tokens={value}"
                    )
    assert not offenders, (
        "these call sites exceed the SDK's non-streaming limit of "
        f"{llm.NONSTREAMING_MAX_TOKENS} and cannot succeed: {offenders}"
    )


# ---------------------------------------------------------------------------
# the fake is the seam, so the fake has to hold the line
# ---------------------------------------------------------------------------


async def test_the_fake_refuses_what_the_real_client_refuses():
    fake = FakeAnthropic()
    fake.messages.create(
        model="m", max_tokens=llm.NONSTREAMING_MAX_TOKENS, system="s", messages=[]
    )
    with pytest.raises(ValueError, match="Streaming is required"):
        fake.messages.create(model="m", max_tokens=32000, system="s", messages=[])
    assert len(fake.calls) == 1, "the refused request must not be recorded as sent"


# ---------------------------------------------------------------------------
# the regression: a 16000-token thread_reply that truncates
# ---------------------------------------------------------------------------


async def test_a_truncated_thread_reply_at_the_current_ceiling_still_completes(
    monkeypatch, caplog
):
    """The blocker, at the exact configuration production runs.

    Before the clamp this raised ValueError from inside the retry and the whole
    concluding turn — sidecar verdict included — was lost.
    """
    fake = FakeAnthropic(
        [
            text_response("half a verdict", stop_reason="max_tokens"),
            text_response("the whole verdict <assessment_json>{}</assessment_json>"),
        ]
    )
    _install(monkeypatch, fake)

    with caplog.at_level("WARNING"):
        out = await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "consult_specialist", "input_schema": {}}],
            tool_executor=_noop_executor,
            max_tokens=THREAD_REPLY_MAX_TOKENS,
            log_meta=LOG_META,
        )

    assert out == "the whole verdict <assessment_json>{}</assessment_json>"
    assert len(fake.calls) == 2, "the retry has to have actually been made"
    assert _retry_call(fake)["max_tokens"] == llm.NONSTREAMING_MAX_TOKENS
    assert _retry_call(fake)["max_tokens"] != THREAD_REPLY_MAX_TOKENS * 2
    assert "Truncation retry clamped" in caplog.text, (
        "an operator reading 'retrying with max_tokens=21333' after a 16000 "
        "truncation must be told why the number is not 32000"
    )


async def test_a_truncated_forced_final_at_the_current_ceiling_also_completes(
    monkeypatch
):
    """The OTHER retry site in generate_with_tools: after max_tool_rounds.

    `max_tool_rounds=1` allows loop iterations 0 and 1 (`range(n + 1)`), so two
    tool-requesting replies exhaust the budget and fall through to the no-tools
    forced final call — which is the one that truncates here.
    """
    fake = FakeAnthropic(
        [
            tool_use_response("consult_specialist", {}, block_id="t1"),
            tool_use_response("consult_specialist", {}, block_id="t2"),
            text_response("truncated forced answer", stop_reason="max_tokens"),
            text_response("complete forced answer"),
        ]
    )
    _install(monkeypatch, fake)

    out = await llm.generate_with_tools(
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "consult_specialist", "input_schema": {}}],
        tool_executor=_noop_executor,
        max_tokens=THREAD_REPLY_MAX_TOKENS,
        max_tool_rounds=1,
        log_meta=LOG_META,
    )

    assert out == "complete forced answer"
    assert fake.calls[-1]["max_tokens"] == llm.NONSTREAMING_MAX_TOKENS


async def test_generate_agent_response_clamps_its_retry_too(monkeypatch):
    """The third retry site. No call site is near 20000 today — but this is the
    function every non-tool phase goes through, so the clamp belongs at all three
    rather than only at the one that happens to be over the line."""
    fake = FakeAnthropic(
        [
            text_response("truncated", stop_reason="max_tokens"),
            text_response("complete"),
        ]
    )
    _install(monkeypatch, fake)

    out = await llm.generate_agent_response(
        "sys", [{"role": "user", "content": "hi"}], max_tokens=20000,
        log_meta=LOG_META,
    )

    assert out == "complete"
    assert _retry_call(fake)["max_tokens"] == llm.NONSTREAMING_MAX_TOKENS


async def test_a_retry_that_fits_under_the_ceiling_still_doubles(monkeypatch, caplog):
    """The clamp must not become the retry budget for everything else: at the
    consult/memory/new_post ceilings, doubling is still what happens, and the
    WARNING that explains a clamp must not cry wolf on turns that got one."""
    fake = FakeAnthropic(
        [
            text_response("truncated", stop_reason="max_tokens"),
            text_response("complete"),
        ]
    )
    _install(monkeypatch, fake)

    with caplog.at_level("WARNING"):
        await llm.generate_agent_response(
            "sys", [{"role": "user", "content": "hi"}], max_tokens=4000,
            log_meta=LOG_META,
        )

    assert _retry_call(fake)["max_tokens"] == 8000
    assert "Truncation retry clamped" not in caplog.text


# ---------------------------------------------------------------------------
# a mis-sized call site fails by name, before it costs anything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("max_tokens", [21_334, 22_000, 32_000])
async def test_a_call_site_above_the_ceiling_fails_fast_and_says_why(
    monkeypatch, max_tokens
):
    fake = FakeAnthropic([text_response("never reached")])
    _install(monkeypatch, fake)

    with pytest.raises(ValueError) as exc_info:
        await llm.generate_with_tools(
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "consult_specialist", "input_schema": {}}],
            tool_executor=_noop_executor,
            max_tokens=max_tokens,
            log_meta=LOG_META,
        )

    message = str(exc_info.value)
    assert "max_tokens" in message
    assert str(llm.NONSTREAMING_MAX_TOKENS) in message
    assert "streaming" in message.lower()
    assert fake.calls == [], (
        "the point of raising in _acreate is that nothing is issued — a request "
        "that cannot succeed must not be billed first"
    )


async def test_generate_agent_response_rejects_an_oversized_ceiling_too(monkeypatch):
    fake = FakeAnthropic([text_response("never reached")])
    _install(monkeypatch, fake)

    with pytest.raises(ValueError, match="NONSTREAMING_MAX_TOKENS"):
        await llm.generate_agent_response(
            "sys", [{"role": "user", "content": "hi"}], max_tokens=25_000,
            log_meta=LOG_META,
        )
    assert fake.calls == []
