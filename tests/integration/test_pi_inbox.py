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
    pi_may_post_to_channel,
    pi_may_reply_in_thread,
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
    user = await factories.make_user(db_session)
    msg = await record_pi_message(
        db_session, run_id=run.id, channel_name="general",
        content="please prioritize the kinase panel", sender_name="Dr Smoke (PI)",
        sender_user_id=user.id,
    )
    await db_session.flush()

    assert msg.is_bot is False          # human/PI message
    assert msg.agent_id is None          # NULL sender_agent_id
    assert msg.channel_id == "C-GEN"     # resolved from agent_channels
    assert msg.visibility == "collab_private"
    assert msg.phase == "new_post"       # top-level (no thread_ts)
    assert msg.posted_at > 0 and msg.message_ts
    # RC-1: the ownership carrier _agent_ids_owned_by_user resolves against.
    assert msg.sender_user_id == user.id

    row = (await db_session.execute(
        select(AgentMessage).where(AgentMessage.message_ts == msg.message_ts)
    )).scalar_one()
    assert row.content == "please prioritize the kinase panel"
    assert row.sender_name == "Dr Smoke (PI)"
    assert row.sender_user_id == user.id
    # RC-2: stamped 'pending' at insert time so a down agent-run's cursor
    # jump can never make this row permanently invisible to the poller.
    assert row.pi_inbound_state == "pending"


async def test_record_pi_message_reply_and_local_channel_fallback(db_session):
    run = await factories.make_simulation_run(db_session)
    # No agent_channels row for this name -> local: id, public visibility.
    # sender_user_id=None is an accepted value (the caller must still pass the
    # keyword explicitly) -- record_pi_message must not choke on it, though
    # SimulationEngine._handle_pi_inbound_entry will skip every ownership-
    # gated side effect for a row that ends up with a NULL sender_user_id.
    msg = await record_pi_message(
        db_session, run_id=run.id, channel_name="drug-repurposing",
        content="following up here", sender_name="PI", sender_user_id=None,
        thread_ts="123.456",
    )
    assert msg.channel_id == "local:drug-repurposing"
    assert msg.visibility == "public"
    assert msg.thread_ts == "123.456"
    assert msg.phase == "thread_reply"   # has a thread_ts
    assert msg.sender_user_id is None
    assert msg.pi_inbound_state == "pending"


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


async def test_pi_may_reply_in_thread_true_for_a_real_participant(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", channel_name="general",
        message_ts="1.0", thread_ts=None,
    )
    assert await pi_may_reply_in_thread(
        db_session, run_id=run.id, channel_name="general", thread_ts="1.0", agent_id="su",
    ) is True


async def test_pi_may_reply_in_thread_false_for_unknown_thread(db_session):
    run = await factories.make_simulation_run(db_session)
    assert await pi_may_reply_in_thread(
        db_session, run_id=run.id, channel_name="general", thread_ts="missing", agent_id="su",
    ) is False


async def test_pi_may_reply_in_thread_false_when_the_participant_is_in_another_channel(
    db_session,
):
    """SEC-F5 (opus review, audit 2026-09-08): the root-existence check is scoped to
    `channel_name`, but the participant check previously was not -- an agent_id that
    happens to share the same `thread_ts` value in a DIFFERENT channel's (unrelated)
    thread must not authorize a reply against this channel's root."""
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session, run=run, agent_id="wiseman", channel_name="general",
        message_ts="2.0", thread_ts=None,
    )
    # Same thread_ts value, but the participating agent posted in a DIFFERENT channel.
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", channel_name="random",
        message_ts="2.1", thread_ts="2.0",
    )
    assert await pi_may_reply_in_thread(
        db_session, run_id=run.id, channel_name="general", thread_ts="2.0", agent_id="su",
    ) is False


# --- pi_may_post_to_channel (SEC3-5, audit 2026-09-10) ----------------------
#
# An unknown channel name previously resolved to True (the "unknown channel
# names are public" fallback) and _resolve_channel then mints a `local:<name>`
# public row for it -- an authenticated PI could write into an arbitrary
# channel name they invented, including another run's real private channel
# name (which has no agent_channels row IN THIS run, since channel_name is
# only unique per run).


async def test_pi_may_post_to_channel_false_for_a_channel_with_no_agent_channels_row(
    db_session,
):
    run = await factories.make_simulation_run(db_session)
    user = await factories.make_user(db_session)
    assert await pi_may_post_to_channel(
        db_session, run_id=run.id, channel_name="made-up-channel",
        user_id=user.id, agent_id="su",
    ) is False


async def test_pi_may_post_to_channel_true_for_a_known_public_channel(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_channel(
        db_session, run=run, channel_name="general", visibility="public",
    )
    user = await factories.make_user(db_session)
    assert await pi_may_post_to_channel(
        db_session, run_id=run.id, channel_name="general",
        user_id=user.id, agent_id="su",
    ) is True


async def test_pi_may_post_to_channel_true_for_a_private_member(db_session):
    run = await factories.make_simulation_run(db_session)
    channel = await factories.make_agent_channel(
        db_session, run=run, channel_name="collab-1", visibility="collab_private",
    )
    user = await factories.make_user(db_session)
    await factories.make_private_channel_member(
        db_session, channel=channel, agent_id=None, user_id=user.id,
    )
    assert await pi_may_post_to_channel(
        db_session, run_id=run.id, channel_name="collab-1",
        user_id=user.id, agent_id="su",
    ) is True


async def test_pi_may_post_to_channel_false_for_a_private_non_member(db_session):
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_channel(
        db_session, run=run, channel_name="collab-1", visibility="collab_private",
    )
    user = await factories.make_user(db_session)
    assert await pi_may_post_to_channel(
        db_session, run_id=run.id, channel_name="collab-1",
        user_id=user.id, agent_id="su",
    ) is False
