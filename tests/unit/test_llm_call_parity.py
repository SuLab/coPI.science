"""LC-05: generate_agent_response and generate_with_tools, rebuilt on the shared
helpers, send byte-identical kwargs to messages.create for every call of every
sequence (tool rounds, truncation retry, forced final), return the same text, and
fire on_retry / on_stop_reason / on_llm_call in the same order as today's code
(frozen verbatim in tests/unit/_frozen_llm_turns.py, copied at the Phase 3 base).

Each request's kwargs are deep-copied at call time: FakeAnthropic.calls stores
references and the tool loop mutates ``messages`` in place afterwards."""
import copy

import pytest

from src.services import llm
from tests.fakes import FakeAnthropic, text_response, tool_use_response
from tests.unit import _frozen_llm_turns as frozen


def _reply(item, requests):
    """A FakeAnthropic response callable that snapshots the request, then yields
    the scripted message or raises the scripted exception."""
    def _call(kwargs):
        requests.append(copy.deepcopy(kwargs))
        if isinstance(item, Exception):
            raise item
        return item
    return _call


_TRUNC = "max_tokens"

_SCENARIOS = {
    "plain": lambda: [text_response("hello")],
    "truncated_then_ok": lambda: [text_response("part", stop_reason=_TRUNC), text_response("whole")],
    "truncated_twice": lambda: [text_response("part", stop_reason=_TRUNC),
                                text_response("still part", stop_reason=_TRUNC)],
    "truncated_blank_retry": lambda: [text_response("part", stop_reason=_TRUNC), text_response("  \n")],
    "truncated_retry_raises": lambda: [text_response("part", stop_reason=_TRUNC),
                                       RuntimeError("529 overloaded")],
    "first_call_raises": lambda: [RuntimeError("boom")],
}

_TOOL_SCENARIOS = {
    "one_round": lambda: [tool_use_response("retrieve_profile", {"agent_id": "a"}), text_response("final")],
    "final_truncated": lambda: [tool_use_response("retrieve_profile", {"agent_id": "a"}),
                                text_response("cut", stop_reason=_TRUNC), text_response("complete")],
    "final_retry_raises": lambda: [tool_use_response("t", {}), text_response("cut", stop_reason=_TRUNC),
                                   RuntimeError("timeout")],
    "forced_final": lambda: [tool_use_response("t", {}) for _ in range(3)] + [text_response("forced")],
    "forced_final_truncated": lambda: [tool_use_response("t", {}) for _ in range(3)]
                                      + [text_response("cut", stop_reason=_TRUNC), text_response("whole")],
    "forced_final_retry_raises": lambda: [tool_use_response("t", {}) for _ in range(3)]
                                         + [text_response("cut", stop_reason=_TRUNC), RuntimeError("timeout")],
    "forced_final_raises": lambda: [tool_use_response("t", {}) for _ in range(3)] + [RuntimeError("boom")],
    "round_raises": lambda: [RuntimeError("boom")],
}


async def _run(fn, replies, monkeypatch, **kw):
    requests: list[dict] = []
    fake = FakeAnthropic([_reply(r, requests) for r in replies])
    monkeypatch.setattr(llm, "get_anthropic_client", lambda: fake)
    events: list[tuple] = []

    def _row(row):
        events.append(("on_llm_call", row["response_text"], row["input_tokens"], row["output_tokens"],
                       [(c["seq"], c["kind"], c["max_tokens"], c.get("stop_reason")) for c in row["call_stats"]]))

    llm.set_call_log_callback(_row)
    try:
        out = await fn(on_retry=lambda: events.append(("on_retry",)),
                       on_stop_reason=lambda r: events.append(("on_stop_reason", r)), **kw)
    except Exception as exc:  # the no-salvage path re-raises in both versions
        out = ("raised", type(exc).__name__)
    finally:
        llm.set_call_log_callback(None)
    return out, requests, events


def _agent_kw():
    return dict(system_prompt="SYS", messages=[{"role": "user", "content": "hi"}], model="m",
                max_tokens=1000, log_meta={"agent_id": "a", "phase": "p"})


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_generate_agent_response_parity(name, monkeypatch):
    new = await _run(llm.generate_agent_response, _SCENARIOS[name](), monkeypatch, **_agent_kw())
    old = await _run(frozen.generate_agent_response, _SCENARIOS[name](), monkeypatch, **_agent_kw())
    assert new == old
    assert new[1], "the scenario must have issued at least one request"


async def test_empty_content_parity(monkeypatch):
    from tests.fakes import empty_response
    new = await _run(llm.generate_agent_response, [empty_response()], monkeypatch, **_agent_kw())
    old = await _run(frozen.generate_agent_response, [empty_response()], monkeypatch, **_agent_kw())
    assert new == old
    assert new[0] == ""


async def _executor(name, args):
    return f"result of {name}"


def _tools_kw():
    return dict(system_prompt="SYS", messages=[{"role": "user", "content": "hi"}],
                tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
                tool_executor=_executor, model="m", max_tokens=1000, max_tool_rounds=2,
                log_meta={"agent_id": "a", "phase": "p"})


@pytest.mark.parametrize("name", sorted(_TOOL_SCENARIOS))
async def test_generate_with_tools_parity(name, monkeypatch):
    new = await _run(llm.generate_with_tools, _TOOL_SCENARIOS[name](), monkeypatch, **_tools_kw())
    old = await _run(frozen.generate_with_tools, _TOOL_SCENARIOS[name](), monkeypatch, **_tools_kw())
    assert new == old
    assert new[1], "the scenario must have issued at least one request"


async def test_forced_final_retry_raise_salvage_parity(monkeypatch):
    """Review Focus 5: the forced final is truncated, its retry raises -> the
    truncated text is returned, callbacks in the same order as before."""
    kw = _tools_kw()
    new = await _run(llm.generate_with_tools, _TOOL_SCENARIOS["forced_final_retry_raises"](), monkeypatch, **kw)
    old = await _run(frozen.generate_with_tools, _TOOL_SCENARIOS["forced_final_retry_raises"](), monkeypatch, **_tools_kw())
    assert new == old
    assert new[0] == "cut"
