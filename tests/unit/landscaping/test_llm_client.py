"""The landscaping request builders, the synchronous client and its refusal chain (spec §9.2,
§9.3, D32, D42). The API is a ``FakeAnthropic``; ledger rows are captured, not written."""
import contextlib
from types import SimpleNamespace

import pytest

from src.landscaping import llm_client
from src.landscaping.llm_client import (
    STAGE_SPECS,
    STAGES,
    UnpricedModelError,
    build_request,
    call_sync,
    require_priced,
)
from src.services import llm
from tests.fakes import FakeAnthropic, text_response

EFFORT = {
    "screen": "low",
    "extract": "medium",
    "triage": "medium",
    "deep": "high",
    "deep_extract": "high",
    "rerank": "medium",
    "similarity": "medium",
    "label": "medium",
    "final": "high",
}
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class _Session:
    async def commit(self):
        return None


@contextlib.asynccontextmanager
async def _db_factory():
    yield _Session()


def _refusal(category: str = "bio"):
    msg = text_response("", stop_reason="refusal")
    msg.stop_details = SimpleNamespace(category=category)
    return msg


@pytest.fixture
def ledger_rows(monkeypatch):
    rows: list[dict] = []

    async def _record(db, **kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(llm_client, "record_call", _record)
    return rows


@pytest.fixture
def fake(monkeypatch):
    def _install(responses):
        client = FakeAnthropic(responses)
        monkeypatch.setattr(llm, "get_anthropic_client", lambda: client)
        return client

    return _install


def _request(stage: str, model: str) -> dict:
    return build_request(stage, model=model, system="System.", user="User.", schema=SCHEMA)


def test_every_stage_constant_within_ceiling():
    assert set(STAGE_SPECS) == set(STAGES)
    for spec in STAGE_SPECS.values():
        assert spec.max_tokens <= llm.NONSTREAMING_MAX_TOKENS
        assert spec.retry_max_tokens <= llm.NONSTREAMING_MAX_TOKENS
        assert spec.retry_max_tokens > spec.max_tokens


def test_build_request_shape():
    for stage in STAGES:
        req = _request(stage, "claude-sonnet-5")
        assert req["thinking"] == {"type": "adaptive"}
        for key in ("temperature", "top_p", "top_k"):
            assert key not in req
        assert req["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
        assert req["max_tokens"] == STAGE_SPECS[stage].max_tokens
        retry = build_request(
            stage, model="claude-sonnet-5", system="s", user="u", schema=None, retry=True
        )
        assert retry["max_tokens"] == STAGE_SPECS[stage].retry_max_tokens
        assert "format" not in retry["output_config"]
        assert "fallbacks" not in req and "fallbacks" not in retry


def test_effort_per_stage():
    assert set(EFFORT) == set(STAGES)
    for stage, effort in EFFORT.items():
        assert _request(stage, "claude-opus-5")["output_config"]["effort"] == effort


async def test_refusal_chain(fake, ledger_rows):
    client = fake([_refusal(), _refusal(), _refusal("cyber")])
    result = await call_sync(
        "screen", _request("screen", "claude-sonnet-5"), db_factory=_db_factory, item_ref="doc-1"
    )
    assert [c["model"] for c in client.calls] == [
        "claude-sonnet-5", "claude-opus-5", "claude-opus-4-8",
    ]
    assert result.stop_reason == "refusal"
    assert result.refusal_category == "cyber"
    assert result.served_by == "claude-opus-4-8"
    assert len(ledger_rows) == 3
    assert [r["model"] for r in ledger_rows] == [c["model"] for c in client.calls]
    assert all(r["batch"] is False and r["stage"] == "screen" for r in ledger_rows)
    assert all("fallbacks" not in c for c in client.calls)

    client = fake([_refusal(), text_response('{"ok": true}')])
    ledger_rows.clear()
    result = await call_sync(
        "final", _request("final", "claude-opus-5"), db_factory=_db_factory, item_ref="opp-1"
    )
    assert [c["model"] for c in client.calls] == ["claude-opus-5", "claude-opus-4-8"]
    assert result.stop_reason == "end_turn"
    assert result.text == '{"ok": true}'
    assert result.served_by == "claude-opus-4-8"
    assert len(ledger_rows) == 2


async def test_batch_refusal_skips_batch_model(fake, ledger_rows):
    client = fake([text_response("{}")])
    await call_sync(
        "screen", _request("screen", "claude-sonnet-5"), db_factory=_db_factory,
        item_ref="doc-1", skip_models=frozenset({"claude-sonnet-5"}),
    )
    assert [c["model"] for c in client.calls] == ["claude-opus-5"]

    client = fake([_refusal()])
    result = await call_sync(
        "final", _request("final", "claude-opus-5"), db_factory=_db_factory,
        item_ref="opp-1", skip_models=frozenset({"claude-opus-5"}), ledger_stage="audit",
    )
    assert [c["model"] for c in client.calls] == ["claude-opus-4-8"]
    assert result.stop_reason == "refusal"
    assert ledger_rows[-1]["stage"] == "audit"


async def test_unpriced_model_refused(fake, ledger_rows):
    with pytest.raises(UnpricedModelError):
        require_priced("claude-unknown")
    require_priced("claude-opus-5")
    client = fake([text_response("{}")])
    with pytest.raises(UnpricedModelError):
        await call_sync(
            "screen", _request("screen", "claude-unknown"), db_factory=_db_factory,
            item_ref="doc-1",
        )
    assert client.calls == []
    assert ledger_rows == []


async def test_truncation_retry_once(fake, ledger_rows):
    spec = STAGE_SPECS["extract"]
    client = fake([
        text_response("{", stop_reason="max_tokens"),
        text_response('{"ok": true}'),
    ])
    result = await call_sync(
        "extract", _request("extract", "claude-sonnet-5"), db_factory=_db_factory,
        item_ref="doc-1",
    )
    assert [c["max_tokens"] for c in client.calls] == [spec.max_tokens, spec.retry_max_tokens]
    assert [c["model"] for c in client.calls] == ["claude-sonnet-5", "claude-sonnet-5"]
    assert result.stop_reason == "end_turn"
    assert len(ledger_rows) == 2

    client = fake([
        text_response("{", stop_reason="max_tokens"),
        text_response("{", stop_reason="max_tokens"),
        text_response('{"ok": true}'),
    ])
    result = await call_sync(
        "extract", _request("extract", "claude-sonnet-5"), db_factory=_db_factory,
        item_ref="doc-1",
    )
    assert len(client.calls) == 2
    assert result.stop_reason == "max_tokens"
