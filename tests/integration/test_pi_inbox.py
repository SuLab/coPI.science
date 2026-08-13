"""Integration tests for the DB-native PI inbox (src/services/pi_inbox.py).

``record_pi_message`` is how a PI's web-authored guidance (``reopen_proposal``)
enters the simulation's DB inbox when Slack is off — the engine ingests the row
for history/observability only (2026-08-12 PI-interaction removal cycle;
``MessageLog``'s GATED reads filter it out of every trigger path). Exercised
against the real migrated Postgres so the actual ``agent_messages`` schema
(including the 0019 columns) is validated. See specs/local-db-conversations.md.
``record_pi_dm``/``pi_dm_messages`` are out of scope here — that function was
deleted (zero production callers once ``pi_handler.py`` was removed); the table
itself is kept per decision 5.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import AgentMessage
from src.services.pi_inbox import get_latest_run_id, record_pi_message
from tests import factories

pytestmark = pytest.mark.integration


async def test_get_latest_run_id_returns_most_recent(db_session):
    # Explicit started_at: now() is the (shared) txn timestamp, so ordering
    # between two same-transaction rows would otherwise be ambiguous.
    now = datetime.now(UTC)
    await factories.make_simulation_run(db_session, started_at=now - timedelta(minutes=5))
    r2 = await factories.make_simulation_run(db_session, started_at=now)
    latest = await get_latest_run_id(db_session)
    assert latest == r2.id


async def test_record_pi_message_resolves_channel_and_writes_human_row(db_session):
    run = await factories.make_simulation_run(db_session)
    # A known channel with collab_private visibility should be picked up.
    await factories.make_agent_channel(
        db_session, run=run, channel_name="general", channel_id="C-GEN",
        visibility="collab_private",
    )
    msg = await record_pi_message(
        db_session, run_id=run.id, channel_name="general",
        content="please prioritize the kinase panel", sender_name="Dr Smoke (PI)",
    )
    await db_session.flush()

    assert msg.is_bot is False          # human/PI message
    assert msg.agent_id is None          # NULL sender_agent_id
    assert msg.channel_id == "C-GEN"     # resolved from agent_channels
    assert msg.visibility == "collab_private"
    assert msg.phase == "new_post"       # top-level (no thread_ts)
    assert msg.posted_at > 0 and msg.message_ts

    row = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.message_ts == msg.message_ts)
    )).scalar_one()
    assert row.content == "please prioritize the kinase panel"
    assert row.sender_name == "Dr Smoke (PI)"


async def test_record_pi_message_reply_and_local_channel_fallback(db_session):
    run = await factories.make_simulation_run(db_session)
    # No agent_channels row for this name -> local: id, public visibility.
    msg = await record_pi_message(
        db_session, run_id=run.id, channel_name="drug-repurposing",
        content="following up here", sender_name="PI", thread_ts="123.456",
    )
    assert msg.channel_id == "local:drug-repurposing"
    assert msg.visibility == "public"
    assert msg.thread_ts == "123.456"
    assert msg.phase == "thread_reply"   # has a thread_ts
