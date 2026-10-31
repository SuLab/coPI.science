"""The landscaping LLM ledger, ``landscape_llm_calls`` (spec §9.5, D19).

One row per API call, synchronous or batched, is the only cost record landscaping keeps:
``llm_call_logs`` requires a simulation run. Spend for the day and the optional daily cap
are read from it.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models.landscape import LandscapeLlmCall
from src.services.llm_pricing import cost_for_request

#: The token classes a ``usage`` dict carries, named as the SDK's ``usage`` fields.
USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)


def usage_from(message: Any) -> dict:
    """``message.usage`` as a dict keyed by ``USAGE_KEYS``; a missing or None count is 0."""
    usage = getattr(message, "usage", None)
    return {key: int(getattr(usage, key, None) or 0) for key in USAGE_KEYS}


async def record_call(
    db: AsyncSession, *, stage: str, model: str, batch_id: uuid.UUID | None,
    item_seq: int | None, custom_id: str | None, usage: dict, refusal_category: str | None,
    served_by: str | None, batch: bool, latency_ms: int | None, item_ref: str | None = None,
) -> None:
    """Add one ledger row in the caller's transaction; the caller commits.

    ``usage`` is keyed by ``USAGE_KEYS`` (absent keys count 0). The cost is
    ``cost_for_request`` on ``model`` at the batch rate when ``batch``, and NULL for an
    unpriced model. A batched row inserts with ON CONFLICT (batch_id, item_seq) DO NOTHING,
    so a results chunk replayed after a crash adds nothing (spec §7.1 step 5); synchronous
    rows leave both NULL and never conflict.
    """
    counts = {key: int(usage.get(key) or 0) for key in USAGE_KEYS}
    cost = cost_for_request(
        model,
        input_tokens=counts["input_tokens"],
        output_tokens=counts["output_tokens"],
        cache_read=counts["cache_read_input_tokens"],
        cache_creation=counts["cache_creation_input_tokens"],
        batch=batch,
    )
    stmt = insert(LandscapeLlmCall).values(
        id=uuid.uuid4(),
        stage=stage,
        model=model,
        served_by_model=served_by,
        batch_id=batch_id,
        item_seq=item_seq,
        custom_id=custom_id,
        item_ref=item_ref,
        batch=batch,
        input_tokens=counts["input_tokens"],
        output_tokens=counts["output_tokens"],
        cache_read_tokens=counts["cache_read_input_tokens"],
        cache_creation_tokens=counts["cache_creation_input_tokens"],
        cost_usd=cost,
        refusal_category=refusal_category,
        latency_ms=latency_ms,
    ).on_conflict_do_nothing(index_elements=["batch_id", "item_seq"])
    await db.execute(stmt)


async def spend_today(db: AsyncSession) -> Decimal:
    """Dollars recorded since 00:00 UTC today; unpriced (NULL-cost) rows add nothing."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total = await db.scalar(
        select(func.coalesce(func.sum(LandscapeLlmCall.cost_usd), 0)).where(
            LandscapeLlmCall.created_at >= start
        )
    )
    return Decimal(str(total))


async def over_daily_cap(db: AsyncSession) -> bool:
    """True once today's spend reaches ``landscape_daily_usd_cap``; False when it is None,
    which means no cap (spec §9.5, L16)."""
    cap = get_settings().landscape_daily_usd_cap
    if cap is None:
        return False
    return await spend_today(db) >= Decimal(str(cap))
