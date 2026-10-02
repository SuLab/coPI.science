"""`llm.abeta_create`: `acreate`'s sibling over `client.beta.messages.create`.

It shares `acreate`'s pipeline (`_create_off_loop`: system-prompt cache
breakpoint, `NONSTREAMING_MAX_TOKENS` pre-flight, API pool + semaphore) and
differs in one way: it never defaults `thinking`, because `acreate`'s
`{"type": "disabled"}` is a 400 on Claude Opus 5.5 (spec O14 calls it with
adaptive thinking). Neither wrapper writes a call-log row; that is the caller's.

The fake here records beta and GA calls separately, so a wrapper that reached
the wrong endpoint fails loudly. tests/fakes.py's FakeAnthropic has no `beta`.
"""

import contextvars
import threading
from types import SimpleNamespace

import pytest

from src.services import llm


class _Endpoint:
    def __init__(self, sink: list[dict], threads: list[str]) -> None:
        self._sink = sink
        self._threads = threads

    def create(self, **kwargs):
        self._sink.append(kwargs)
        self._threads.append(threading.current_thread().name)
        return SimpleNamespace(content=[], stop_reason="end_turn", kwargs=kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.beta_calls: list[dict] = []
        self.ga_calls: list[dict] = []
        self.threads: list[str] = []
        self.messages = _Endpoint(self.ga_calls, self.threads)
        self.beta = SimpleNamespace(messages=_Endpoint(self.beta_calls, self.threads))


async def test_calls_beta_messages_create_with_the_given_kwargs():
    fake = _FakeClient()
    reply = await llm.abeta_create(
        fake,
        model="claude-opus-5-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": "x"}],
        thinking={"type": "adaptive"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
    )
    assert fake.ga_calls == []
    assert fake.beta_calls == [
        {
            "model": "claude-opus-5-5",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": "x"}],
            "thinking": {"type": "adaptive"},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
            "output_config": {"effort": "medium"},
        }
    ]
    assert reply.stop_reason == "end_turn"


async def test_does_not_default_thinking():
    """`acreate` injects `{"type": "disabled"}`; Opus 5.5 rejects that with a 400."""
    fake = _FakeClient()
    await llm.abeta_create(fake, model="m", max_tokens=100, messages=[])
    assert "thinking" not in fake.beta_calls[0]

    # Control: acreate still defaults it, so the difference is the wrapper's.
    await llm.acreate(fake, model="m", max_tokens=100, messages=[])
    assert fake.ga_calls[0]["thinking"] == {"type": "disabled"}


async def test_raises_above_the_nonstreaming_ceiling_without_sending():
    fake = _FakeClient()
    with pytest.raises(llm.NonStreamingMaxTokensError, match="NONSTREAMING_MAX_TOKENS"):
        await llm.abeta_create(
            fake, model="m", max_tokens=llm.NONSTREAMING_MAX_TOKENS + 1, messages=[]
        )
    assert fake.beta_calls == [], "a request that cannot succeed must not be issued"

    await llm.abeta_create(
        fake, model="m", max_tokens=llm.NONSTREAMING_MAX_TOKENS, messages=[]
    )
    assert len(fake.beta_calls) == 1


async def test_caches_the_system_prompt_like_acreate():
    prompt = "You extract founder claims.\nRules follow."
    fake = _FakeClient()
    await llm.abeta_create(fake, model="m", max_tokens=100, system=prompt, messages=[])
    await llm.acreate(fake, model="m", max_tokens=100, system=prompt, messages=[])

    expected = [
        {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
    ]
    assert fake.beta_calls[0]["system"] == expected
    assert fake.beta_calls[0]["system"] == fake.ga_calls[0]["system"]


async def test_whitespace_system_prompt_passes_through_untouched():
    fake = _FakeClient()
    await llm.abeta_create(fake, model="m", max_tokens=100, system="  ", messages=[])
    assert fake.beta_calls[0]["system"] == "  "


async def test_runs_on_the_api_pool_with_the_callers_context():
    marker = contextvars.ContextVar("marker", default=None)
    seen: list = []

    class _Recording(_Endpoint):
        def create(self, **kwargs):
            seen.append(marker.get())
            return super().create(**kwargs)

    fake = _FakeClient()
    fake.beta = SimpleNamespace(messages=_Recording(fake.beta_calls, fake.threads))
    marker.set("bound")
    await llm.abeta_create(fake, model="m", max_tokens=100, messages=[])

    assert fake.threads[0].startswith(llm._API_THREAD_NAME_PREFIX)
    assert seen == ["bound"]


async def test_writes_no_call_log_row(monkeypatch):
    """Like `acreate`: the call log is emitted by the caller, not the wrapper."""
    rows: list[dict] = []
    monkeypatch.setattr(llm, "_call_log_callback", rows.append)
    await llm.abeta_create(_FakeClient(), model="m", max_tokens=100, messages=[])
    assert rows == []
