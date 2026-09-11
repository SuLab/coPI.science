"""A resend for the same
(user, thread_decision, category) must mint a fresh reply_token, not reuse the
earlier one.

`send_proposal_notification` and `_send_new_proposal_email` must not
look up any existing EmailNotification row for the (user, proposal,
category) key and reuse its `reply_token` verbatim on every resend while only
bumping `sent_at`. That would leave the FIRST e-mail's reply address permanently
redeemable (`process_inbound_email` looks up purely by token, with no
per-send identity) and let an already-`expired` row flip back to `sent`
carrying the same, already-superseded token — defeating the reply-token's
expiry window, which is measured from `sent_at`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import src.services.email_notifications as en
from src.models import EmailNotification
from src.services.email_inbound import process_inbound_email
from tests import factories

pytestmark = pytest.mark.integration


class _RecordingSES:
    def __init__(self):
        self.sent: list[dict] = []

    def send_raw_email(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": f"m-{len(self.sent)}"}


async def _classify_rating(body, proposal_summary):
    return {"category": "review", "rating": 4, "comment": "yes", "instruction": ""}


async def test_a_resend_mints_a_fresh_token_and_retires_the_old_one(db_session, monkeypatch):
    monkeypatch.setattr(en.get_settings(), "outbound_email_allowlist", "")
    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    user = await factories.make_user(db_session, email="pi.rotation@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    await db_session.flush()

    ok = await en.send_proposal_notification(
        user=user, thread_decision=td, agent=agent,
        other_bot_name="OtherBot", total_unreviewed=1, db=db_session,
    )
    assert ok is True
    row = (await db_session.execute(
        select(EmailNotification).where(EmailNotification.thread_decision_id == td.id)
    )).scalar_one()
    old_token = row.reply_token
    old_sent_at = row.sent_at

    # Resend for the SAME proposal — reconciles the same row (unique constraint).
    ok = await en.send_proposal_notification(
        user=user, thread_decision=td, agent=agent,
        other_bot_name="OtherBot", total_unreviewed=1, db=db_session,
    )
    assert ok is True
    await db_session.refresh(row)
    new_token = row.reply_token
    assert new_token != old_token, "a resend must mint a fresh reply_token"
    assert row.sent_at > old_sent_at

    import src.services.email_inbound as inbound
    monkeypatch.setattr(inbound, "_send_simple_email", lambda *a, **k: True)
    monkeypatch.setattr(inbound, "classify_reply", _classify_rating)

    old_raw = (
        factories.SES_PASS_HEADER
        + f"From: {user.email}\n"
        + f"To: review+{old_token}@reply.copi.science\n"
        + 'Content-Type: text/plain; charset="UTF-8"\n'
        + "\n4 excellent\n"
    ).encode()
    await process_inbound_email(old_raw, db_session)
    await db_session.refresh(row)
    assert row.response_type is None, "the retired (old) token must not be honoured"
    assert row.status != "responded"

    new_raw = (
        factories.SES_PASS_HEADER
        + f"From: {user.email}\n"
        + f"To: review+{new_token}@reply.copi.science\n"
        + 'Content-Type: text/plain; charset="UTF-8"\n'
        + "\n4 excellent\n"
    ).encode()
    await process_inbound_email(new_raw, db_session)
    await db_session.refresh(row)
    assert row.status == "responded", "the current token must still work"


async def test_resending_an_expired_row_mints_a_fresh_token(db_session, monkeypatch):
    monkeypatch.setattr(en.get_settings(), "outbound_email_allowlist", "")
    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    user = await factories.make_user(db_session, email="pi.expired-resend@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    old_token = f"tok-{uuid.uuid4().hex}"
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=old_token, category="proposal_review", status="expired",
        sent_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.add(notification)
    await db_session.flush()

    ok = await en.send_proposal_notification(
        user=user, thread_decision=td, agent=agent,
        other_bot_name="OtherBot", total_unreviewed=1, db=db_session,
    )
    assert ok is True
    await db_session.refresh(notification)
    assert notification.status == "sent"
    assert notification.reply_token != old_token, (
        "an expired row resent for the same proposal must not flip back to 'sent' "
        "carrying the same (already superseded) token"
    )
