"""synthesize_profile tells a model refusal (dead at once) from other failures (retried)
(spec 2026-10-05 §7; DECISION 2026-10-06)."""
import pytest

from src.services import llm
from tests.fakes import FakeAnthropic, multi_text_response, text_response


async def test_a_refusal_raises_synthesis_refused(monkeypatch):
    fake = FakeAnthropic([multi_text_response(stop_reason="refusal")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)
    with pytest.raises(llm.SynthesisRefused):
        await llm.synthesize_profile("context", "Ada Lovelace")


async def test_an_unparseable_reply_is_a_plain_value_error(monkeypatch):
    fake = FakeAnthropic([text_response("not json")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)
    with pytest.raises(ValueError) as ei:
        await llm.synthesize_profile("context", "Ada Lovelace")
    assert not isinstance(ei.value, llm.SynthesisRefused)


def test_the_fallback_prompt_matches_the_prompt_file_rules():
    prompt = llm._default_synthesis_prompt()
    assert "submitted" not in prompt.lower()
    assert "100–350 word" in prompt and "30 items" in prompt
