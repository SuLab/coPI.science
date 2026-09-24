"""Optional and owner-gated (spec §11.6): one benign question on every assessment,
through the real model, to measure refusal and fallback rates before users are told the
chat exists. Roughly $12 at 27 assessments. It writes NOTHING: no chat rows, no ledger
rows — it builds each staff-tier record and calls the model directly.

Dry run by default (prints each record's size in characters; nothing leaves the host);
--count-tokens also asks the API's token-counting endpoint for each record's input
tokens (it sends every record to Anthropic but generates nothing) — the measured
replacement for the spec's F13 estimate, which the $2.50 reserve rests on; --apply
spends money. Run it INSIDE a one-off app container so it uses the deployed SDK (1.8.0)
and the real API key:

  docker compose -f docker-compose.prod.yml run --rm -T --no-deps -e PYTHONPATH=/app \\
    -v "$PWD/scripts:/app/scripts:ro" blackbird-app \\
    python scripts/dev/assessment_chat_refusal_sweep.py [--count-tokens | --apply]
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter

QUESTION = "What is being proposed, in plain terms?"


async def main(apply: bool, count_tokens: bool = False) -> int:
    from sqlalchemy import select

    from src.config import get_settings
    from src.database import get_session_factory
    from src.models import OpportunityAssessment
    from src.services import assessment_chat as chat
    from src.services.assessment_chat_record import load_chat_record
    from src.services.assessment_chat_stream import (
        CitationBook,
        UsageSnapshot,
        consume_stream,
        outcome_from_final,
    )

    settings = get_settings()
    system_prompt, _ = chat.load_system_prompt()
    async with get_session_factory()() as db:
        ids = list(
            (
                await db.execute(
                    select(OpportunityAssessment.id).order_by(OpportunityAssessment.created_at)
                )
            ).scalars()
        )
        records = []
        for assessment_id in ids:
            loaded = await load_chat_record(db, assessment_id, tier="staff")
            if loaded is not None:
                records.append((assessment_id, loaded[0]))
    print(f"{len(records)} assessments; model {settings.llm_assessment_chat_model}; apply={apply}")
    if not apply:
        client = chat.get_async_anthropic_client() if count_tokens else None
        counts: list[int] = []
        for assessment_id, record in records:
            size = sum(len(json.dumps(doc, ensure_ascii=False)) for doc in record.documents)
            if client is None:
                print(assessment_id, f"{size} characters")
                continue
            # The same system + messages shape as the real-API test.
            counted = await client.beta.messages.count_tokens(
                model=settings.llm_assessment_chat_model,
                system=[{"type": "text", "text": system_prompt}],
                messages=chat.build_messages(record, [], QUESTION),
            )
            counts.append(counted.input_tokens)
            print(assessment_id, f"{size} characters", f"{counted.input_tokens} input tokens")
        if counts:
            print(f"--- input tokens: min {min(counts)}, max {max(counts)}, "
                  f"mean {sum(counts) // len(counts)} over {len(counts)} records")
        return 0

    async def emit(event, data):
        return None

    client = chat.get_async_anthropic_client()
    tally: Counter = Counter()
    for assessment_id, record in records:
        request = chat.build_request(
            model=settings.llm_assessment_chat_model,
            effort=settings.assessment_chat_effort,
            system_prompt=system_prompt,
            messages=chat.build_messages(record, [], QUESTION),
        )
        try:
            async with asyncio.timeout(chat.DEADLINE_SECONDS):
                async with client.beta.messages.stream(**request) as stream:
                    await consume_stream(
                        stream, emit=emit, snapshot=UsageSnapshot(), book=CitationBook(record)
                    )
                    final = await stream.get_final_message()
            outcome = outcome_from_final(final, record=record, requested_model=request["model"])
            key = (outcome.status, outcome.refusal_category, outcome.served_by_model, outcome.fallback_used)
            print(assessment_id, *key, outcome.usage_by_model)
        except Exception as exc:  # report and continue: this is a measurement
            key = ("error", type(exc).__name__, None, None)
            print(assessment_id, *key)
        tally[key] += 1
    print("--- totals")
    for key, count in tally.most_common():
        print(count, *key)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(asyncio.run(main("--apply" in args, count_tokens="--count-tokens" in args)))
