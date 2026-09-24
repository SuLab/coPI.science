"""Pure pieces of the chat service (spec §5.1-§5.3, §6.4, §6.5)."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.services import assessment_chat as chat
from src.services.assessment_chat_record import build_chat_record
from tests.assessment_chat_support import synthetic_detail

MODEL = "claude-opus-5-5"


def _record():
    return build_chat_record(synthetic_detail(), tier="staff")


def _turn(question, answer, status="complete"):
    return SimpleNamespace(id=object(), question=question, answer_text=answer, status=status)


def _entry(model, billed=True, **tokens):
    base = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    base.update(tokens)
    return {"model": model, "billed": billed, **base}


@pytest.mark.parametrize(
    "raw",
    [None, 42, "", "   ", "x" * 11, "bad" + chr(0) + "byte", "lone" + chr(0xD800)],
)
def test_invalid_questions_are_refused(raw):
    with pytest.raises(chat.ChatError) as caught:
        chat.validate_question(raw, max_chars=10)
    assert (caught.value.status, caught.value.code) == (400, "invalid_question")


def test_a_question_is_stripped_and_counted_in_code_points():
    assert chat.validate_question("  ask me  ", max_chars=6) == "ask me"
    assert chat.validate_question("é" * 10, max_chars=10) == "é" * 10


def test_the_request_has_exactly_the_specified_shape():
    request = chat.build_request(
        model=MODEL, effort="medium", system_prompt="SYSTEM",
        messages=[{"role": "user", "content": "q"}],
    )
    assert request == {
        "model": MODEL,
        "max_tokens": 12000,
        "system": [{"type": "text", "text": "SYSTEM"}],
        "messages": [{"role": "user", "content": "q"}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "cache_control": {"type": "ephemeral"},
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


def test_the_record_comes_first_and_only_its_last_document_carries_a_breakpoint():
    record = _record()
    messages = chat.build_messages(record, [], "What is proposed?")
    assert len(messages) == 1
    content = messages[0]["content"]
    assert [c["type"] for c in content] == ["document"] * 5 + ["text"]
    assert content[-1] == {"type": "text", "text": "What is proposed?"}
    assert all("cache_control" not in c for c in content[:4])
    assert content[4]["cache_control"] == {"type": "ephemeral"}
    # The record's own documents are never mutated: its hash must not move.
    assert all("cache_control" not in d for d in record.documents)


def test_history_is_replayed_as_plain_text_only():
    messages = chat.build_messages(_record(), [_turn("Q1", "A1"), _turn("Q2", "A2")], "Q3")
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[0]["content"][-1] == {"type": "text", "text": "Q1"}
    assert messages[1]["content"] == [{"type": "text", "text": "A1"}]
    assert messages[2]["content"] == [{"type": "text", "text": "Q2"}]
    assert messages[3]["content"] == [{"type": "text", "text": "A2"}]
    assert messages[4]["content"] == [{"type": "text", "text": "Q3"}]
    assert {block["type"] for m in messages for block in m["content"]} == {"document", "text"}


def test_the_replay_window_is_the_newest_replayable_run_within_the_budget(monkeypatch):
    monkeypatch.setattr(chat, "HISTORY_REPLAY_MAX_CHARS", 10)
    turns = [
        _turn("old", "answer"),                   # 9 characters: pushed out
        _turn("q", "   ", status="truncated"),    # blank: never sent
        _turn("r", "no", status="refused"),       # refused: never sent
        _turn("f", "x", status="failed"),
        _turn("q2", "ok"),                        # 4
        _turn("q3", "fine"),                      # 6
    ]
    assert [t.question for t in chat.replay_window(turns)] == ["q2", "q3"]


def test_the_system_prompt_is_read_per_call_with_its_hash(tmp_path, monkeypatch):
    path = tmp_path / "assessment-chat.md"
    path.write_text("  You answer questions.\n", encoding="utf-8")
    monkeypatch.setattr(chat, "PROMPT_PATH", path)
    text, sha = chat.load_system_prompt()
    assert text == "You answer questions." and len(sha) == 12
    path.write_text("Edited.", encoding="utf-8")
    assert chat.load_system_prompt()[0] == "Edited."


@pytest.mark.parametrize("content", [None, "   \n"])
def test_a_missing_or_empty_prompt_is_503(tmp_path, monkeypatch, content):
    path = tmp_path / "assessment-chat.md"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(chat, "PROMPT_PATH", path)
    with pytest.raises(chat.ChatError) as caught:
        chat.load_system_prompt()
    assert (caught.value.status, caught.value.code) == (503, "prompt_missing")


def test_row_spend_prices_billed_entries_only():
    entries = [
        _entry(MODEL, billed=False, input_tokens=5_000_000),
        _entry("claude-opus-5", output_tokens=1_000_000),
    ]
    assert chat.row_spend(entries) == Decimal("25")


def test_row_spend_counts_the_reserve_for_unknown_or_unpriced_usage(caplog):
    assert chat.row_spend(None) == Decimal("2.50")
    assert chat.row_spend([]) == Decimal("0")
    with caplog.at_level(logging.WARNING, logger="src.services.assessment_chat"):
        assert chat.row_spend([_entry("claude-unknown-9", input_tokens=1)]) == Decimal("2.50")
    assert "unpriced model" in caplog.text


def test_row_spend_floors_a_partial_entry_at_the_reserve():
    # A partial entry priced cheaper than the reserve is still billed at the
    # reserve — its output count is message_start's placeholder, not the truth.
    cheap = [{**_entry(MODEL, input_tokens=1), "partial": True}]
    assert chat.row_spend(cheap) == Decimal("2.50")


def test_row_spend_keeps_a_partial_entry_priced_above_the_reserve():
    # A partial entry that already prices above the reserve (a long answer cut
    # off after its message_delta arrived) keeps its own, larger cost.
    expensive = [{**_entry(MODEL, output_tokens=1_000_000), "partial": True}]
    assert chat.row_spend(expensive) == Decimal("20")


def test_an_sse_frame_is_one_event_and_one_line_of_json():
    frame = chat.sse_frame("text", {"seg": 0, "text": "two\nlines"})
    assert frame == 'event: text\ndata: {"seg":0,"text":"two\\nlines"}\n\n'


async def test_the_stream_pings_after_silence_and_ends_on_none():
    queue = asyncio.Queue()
    stream = chat.sse_stream(queue, heartbeat=0.01)
    assert await anext(stream) == ": ping\n\n"
    await queue.put(("done", {"ok": True}))
    await queue.put(None)
    assert await anext(stream) == 'event: done\ndata: {"ok":true}\n\n'
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


def test_turn_payload_flags_a_changed_record_and_the_window():
    row = SimpleNamespace(
        id="t1", question="Q", answer_text="A", answer_segments=None, citations=None,
        allowed_links=None, status="complete", stop_reason="end_turn", refusal_category=None,
        error_code=None, served_by_model=MODEL, fallback_used=False,
        record_sha256_12="aaaaaaaaaaaa",
        created_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC), completed_at=None,
    )
    same = chat.turn_payload(row, current_sha="aaaaaaaaaaaa", in_window=True)
    assert (same["record_changed"], same["in_window"]) == (False, True)
    assert (same["segments"], same["citations"], same["allowed_links"]) == ([], [], [])
    assert same["created_at"] == "2026-09-24T10:00:00+00:00" and same["completed_at"] is None
    changed = chat.turn_payload(row, current_sha="bbbbbbbbbbbb", in_window=False)
    assert (changed["record_changed"], changed["in_window"]) == (True, False)


def test_chat_error_carries_its_extras():
    err = chat.ChatError(429, "daily_limit", resets_at="2026-09-25T10:00:00+00:00")
    assert (err.status, err.code, err.extra) == (
        429, "daily_limit", {"resets_at": "2026-09-25T10:00:00+00:00"},
    )
