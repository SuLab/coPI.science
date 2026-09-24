"""Opt-in, spends real money (well under $0.10): two identical questions through the
real SDK on a small seeded record (spec §11.5). Needs BOTH `ANTHROPIC_API_KEY` and
`RUN_ASSESSMENT_CHAT_REAL_LLM=1`, so `./scripts/ci.sh` on a host that exports a key
never runs it by accident. Prints the SEEDED record's input-token count — a small
synthetic record, so not a production size: the measured replacement for the spec's F13
estimate is `scripts/dev/assessment_chat_refusal_sweep.py --count-tokens`.

    RUN_ASSESSMENT_CHAT_REAL_LLM=1 ANTHROPIC_API_KEY=... \
      .venv-test/bin/python -m pytest tests/integration/test_assessment_chat_real_llm.py -s
"""

import os

import anthropic
import pytest

from src.config import get_settings
from src.services import assessment_chat as chat
from src.services.assessment_chat_record import load_chat_record
from src.services.assessment_chat_stream import (
    CitationBook,
    UsageSnapshot,
    consume_stream,
    outcome_from_final,
)
from tests.assessment_chat_support import seed_interview

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_llm,
    pytest.mark.skipif(
        not (
            os.environ.get("ANTHROPIC_API_KEY")
            and os.environ.get("RUN_ASSESSMENT_CHAT_REAL_LLM") == "1"
        ),
        reason="real-API chat check is opt-in: set ANTHROPIC_API_KEY and RUN_ASSESSMENT_CHAT_REAL_LLM=1",
    ),
]

QUESTION = "In one sentence, what does the lab's agent say it has built?"


async def _ask_once(client, record, system_prompt, model):
    request = chat.build_request(
        model=model, effort="low", system_prompt=system_prompt,
        messages=chat.build_messages(record, [], QUESTION),
    )
    snapshot = UsageSnapshot()

    async def emit(event, data):
        return None

    async with client.beta.messages.stream(**request) as stream:
        await consume_stream(stream, emit=emit, snapshot=snapshot, book=CitationBook(record))
        final = await stream.get_final_message()
    return outcome_from_final(final, record=record, requested_model=model), final


async def test_a_real_answer_cites_the_record_and_reuses_the_cache(db_session):
    seeded = await seed_interview(db_session)
    loaded = await load_chat_record(db_session, seeded.assessment_id, tier="staff")
    assert loaded is not None
    record = loaded[0]
    system_prompt, _ = chat.load_system_prompt()
    model = get_settings().llm_assessment_chat_model
    client = anthropic.AsyncAnthropic()

    try:
        counted = await client.beta.messages.count_tokens(
            model=model,
            system=[{"type": "text", "text": system_prompt}],
            messages=chat.build_messages(record, [], QUESTION),
        )
        print(f"seeded record + prompt: {counted.input_tokens} input tokens")
    except anthropic.APIError as exc:  # informational only
        print(f"count_tokens unavailable: {type(exc).__name__}")

    first, first_final = await _ask_once(client, record, system_prompt, model)
    second, second_final = await _ask_once(client, record, system_prompt, model)
    assert first.status == "complete", (first.status, first.error_code, first.stop_reason)
    assert any(c["anchor"] for c in first.citations), first.citations
    first_cache = (first_final.usage.cache_creation_input_tokens or 0) + (
        first_final.usage.cache_read_input_tokens or 0
    )
    # Written OR read: a rerun inside the cache's lifetime reads the entry the previous
    # run wrote, so "written on the first call" would fail on a legitimate rerun.
    assert first_cache > 0
    assert (second_final.usage.cache_read_input_tokens or 0) > 0
    print(f"served by {first.served_by_model} / {second.served_by_model}; usage {first.usage_by_model}")
