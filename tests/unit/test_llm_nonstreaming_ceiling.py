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
# the client-level read timeout — and what setting it costs
# ---------------------------------------------------------------------------


async def test_the_shared_client_carries_a_300s_timeout():
    """300 s, not the SDK's 600 and not the 120 the first audit draft proposed.

    Run 8b64a0e0 stalled twice at 600.09 / 600.10 s — ``read=600`` exactly, on
    HTTP 200s, so no overload story survives. 120 would have cut legitimate
    calls: the run's largest honest reply took 119.18 s, 0.8 s under it, and
    ``max_tokens=16000`` at the measured 60.7 tok/s authorises up to 264 s. 300
    cut zero legitimate calls in that run.

    ``connect`` is left at the SDK's own 5 s rather than dragged up to 300 with
    the rest: a black-holed SYN is not a long generation, and a bare
    ``timeout=300.0`` float would set all four fields.
    """
    client = llm._client_for_key("sk-ant-not-a-real-key")
    assert llm.CLIENT_READ_TIMEOUT_SECONDS == 300.0
    assert client.timeout.read == 300.0
    assert client.timeout.connect == 5.0, (
        "a connection that never establishes must still fail fast — only the "
        "READ ceiling is what a long generation needs"
    )


async def test_setting_any_timeout_disables_the_sdks_own_ceiling_guard():
    """The trap that makes ``_acreate``'s guard load-bearing rather than belt.

    ``Messages.create`` applies ``_calculate_nonstreaming_timeout`` — the thing
    that raises on ``max_tokens > 21_333`` — only ``if not stream and not
    is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT``. Giving the
    client a timeout of our own permanently fails that condition, so the SDK no
    longer refuses an oversized non-streaming request for us. This test is the
    alarm on that reasoning: if it ever starts failing because the client is
    back on the default, the guard below is belt-and-braces again and the
    comment at ``NONSTREAMING_MAX_TOKENS`` needs revising.
    """
    try:
        from anthropic._constants import DEFAULT_TIMEOUT
    except ImportError:  # pragma: no cover - SDK internals moved
        pytest.skip("anthropic moved DEFAULT_TIMEOUT; re-derive this assertion")

    client = llm._client_for_key("sk-ant-not-a-real-key")
    assert client.timeout != DEFAULT_TIMEOUT


async def test_acreate_still_raises_above_the_nonstreaming_ceiling():
    """With the SDK's guard out of the picture, this is the only one left.

    Asserted against ``_acreate`` directly rather than through a public entry
    point, because ``_acreate`` is the single choke point every non-streaming
    request in this module passes through — the property under test is that the
    check lives THERE, not that one caller happens to be covered.
    """
    fake = FakeAnthropic([text_response("never reached")])

    with pytest.raises(ValueError, match="NONSTREAMING_MAX_TOKENS"):
        await llm._acreate(
            fake, model="m", max_tokens=llm.NONSTREAMING_MAX_TOKENS + 1,
            system="s", messages=[],
        )
    assert fake.calls == [], "a request that cannot succeed must not be issued"

    # ...and the last accepted value still goes through, so the guard is a
    # ceiling rather than an off-by-one that costs a whole token of budget.
    await llm._acreate(
        fake, model="m", max_tokens=llm.NONSTREAMING_MAX_TOKENS,
        system="s", messages=[],
    )
    assert len(fake.calls) == 1


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
