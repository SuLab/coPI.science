"""Pins the per-phase model tiering.

The high-volume default-model paths (phase-2 scan/prune, memory synthesis,
and the phase-1 decision helper) run on Sonnet to keep the pre-Opus-5 cost
profile; only the phase-4 reply and phase-5 post paths (which pass
settings.llm_agent_model_opus explicitly) run on Opus 5. A default-model
change silently re-prices every scan/prune/memory call, so the wiring is
pinned here — see PR #30's Opus 5 upgrade, which originally moved the
default from claude-sonnet-4-6 to claude-opus-5 and re-priced those paths
~5x without saying so.
"""

import pytest

from src.config import get_settings
from src.services import llm
from tests.fakes import FakeAnthropic, text_response


@pytest.fixture(autouse=True)
def _clear_llm_callback():
    llm.set_call_log_callback(None)
    yield
    llm.set_call_log_callback(None)


def test_default_agent_model_is_sonnet_5_and_opus_knob_is_opus_5():
    settings = get_settings()
    assert settings.llm_agent_model == "claude-sonnet-5"
    assert settings.llm_agent_model_opus == "claude-opus-5"


async def test_generate_agent_response_defaults_to_the_sonnet_tier(monkeypatch):
    """A model-less call (the phase-2 scan/prune and memory-synthesis shape)
    must hit the sonnet-tier default, not Opus."""
    fake = FakeAnthropic([text_response("ok")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    await llm.generate_agent_response("sys", [{"role": "user", "content": "hi"}])

    assert fake.calls[0]["model"] == get_settings().llm_agent_model
    assert fake.calls[0]["model"] == "claude-sonnet-5"
    # The thinking pin must survive the model change: Sonnet 5 runs adaptive
    # thinking when the param is omitted, and max_tokens caps thinking + text
    # together — the tight per-phase caps rely on this being disabled.
    assert fake.calls[0]["thinking"] == {"type": "disabled"}


async def test_make_decision_defaults_to_the_sonnet_tier(monkeypatch):
    fake = FakeAnthropic([text_response('{"action": "skip"}')])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    await llm.make_decision("sys", [{"role": "user", "content": "hi"}])

    assert fake.calls[0]["model"] == "claude-sonnet-5"
