"""Integration tests for the DB-native PI inbox (src/services/pi_inbox.py).

These helpers are how a PI's web-authored messages and DMs enter the simulation
when Slack is off — the engine ingests the rows they write. Exercised against the
real migrated Postgres so the actual agent_messages / pi_dm_messages schema
(including the 0019/0020 columns) is validated. See specs/local-db-conversations.md.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import AgentMessage, PiDmMessage
from src.services.pi_inbox import (
    get_latest_run_id,
    record_pi_dm,
    record_pi_message,
    web_pi_user_id,
)
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


async def test_record_pi_dm_inbound_and_outbound(db_session):
    run = await factories.make_simulation_run(db_session)
    uid = uuid.uuid4()
    inbound = await record_pi_dm(
        db_session, run_id=run.id, agent_id="su", pi_user_id=web_pi_user_id(uid),
        direction="inbound", content="always cc me on proposals", sender_name="PI",
    )
    await record_pi_dm(
        db_session, run_id=run.id, agent_id="su", pi_user_id=web_pi_user_id(uid),
        direction="outbound", content="noted — will do", sender_name="SuBot",
    )
    await db_session.flush()

    assert inbound.pi_user_id == f"local:{uid}"
    rows = (await db_session.execute(
        select(PiDmMessage).where(PiDmMessage.simulation_run_id == run.id)
        .order_by(PiDmMessage.posted_at.asc())
    )).scalars().all()
    assert [r.direction for r in rows] == ["inbound", "outbound"]
    assert rows[0].content == "always cc me on proposals"
    assert rows[1].agent_id == "su"
    assert all(r.ts and r.posted_at > 0 for r in rows)
