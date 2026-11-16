"""The landscaping ledger, ``landscape_llm_calls`` (spec §9.5, §7.1 step 5)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.landscaping import ledger
from src.models.landscape import LandscapeBatch, LandscapeLlmCall

pytestmark = pytest.mark.integration

USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 2_000,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


async def _batch(db) -> LandscapeBatch:
    row = LandscapeBatch(stage="screen", model="claude-sonnet-5", row_table="landscape_screens")
    db.add(row)
    await db.flush()
    return row


async def _record(db, *, batch_id=None, item_seq=None, batch=False, custom_id=None):
    await ledger.record_call(
        db, stage="screen", model="claude-sonnet-5", batch_id=batch_id, item_seq=item_seq,
        custom_id=custom_id, usage=USAGE, refusal_category=None, served_by="claude-sonnet-5",
        batch=batch, latency_ms=None, item_ref="doc-1",
    )


async def test_ledger_cost_uses_batch_flag(db_session):
    batch = await _batch(db_session)
    await _record(db_session)
    await _record(db_session, batch_id=batch.id, item_seq=1, batch=True, custom_id="b1-1")
    rows = {
        r.batch: r
        for r in (await db_session.execute(select(LandscapeLlmCall))).scalars().all()
    }
    # Sonnet 5: $2 / MTok in, $10 / MTok out → $0.022 standard, half of it batched.
    assert rows[False].cost_usd == Decimal("0.022")
    assert rows[True].cost_usd == Decimal("0.011")
    assert rows[True].custom_id == "b1-1"
    assert rows[False].batch_id is None and rows[False].item_ref == "doc-1"


async def test_ledger_batched_row_insert_ignore(db_session):
    batch = await _batch(db_session)
    await _record(db_session, batch_id=batch.id, item_seq=7, batch=True)
    await _record(db_session, batch_id=batch.id, item_seq=7, batch=True)
    count = await db_session.scalar(
        select(func.count()).select_from(LandscapeLlmCall).where(
            LandscapeLlmCall.batch_id == batch.id, LandscapeLlmCall.item_seq == 7
        )
    )
    assert count == 1


async def test_over_daily_cap_none_is_false(db_session, monkeypatch):
    monkeypatch.setattr(
        ledger, "get_settings", lambda: SimpleNamespace(landscape_daily_usd_cap=None)
    )
    await _record(db_session)
    assert await ledger.over_daily_cap(db_session) is False


async def test_over_daily_cap_once_spend_reaches_it(db_session, monkeypatch):
    before = await ledger.spend_today(db_session)
    await _record(db_session)
    assert await ledger.spend_today(db_session) == before + Decimal("0.022")
    cap = float(before) + 0.01
    monkeypatch.setattr(
        ledger, "get_settings", lambda: SimpleNamespace(landscape_daily_usd_cap=cap)
    )
    assert await ledger.over_daily_cap(db_session) is True
