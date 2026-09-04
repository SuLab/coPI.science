"""V4-3/V4-4a: an unanswered proposal_review reminder expires instead of being immortal, and the
downgrade ladder can advance past its first rung. V4-3's third test also pins B3/M1: expiring then
falling through to a re-send for the SAME proposal must reconcile the existing row, not violate
uq_email_notification_user_thread_category.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import src.services.email_notifications as en
from src.config import get_settings
from src.models import EmailEngagementTracker, EmailNotification, ProposalReview, User
from tests import factories

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def no_ses(monkeypatch):
    """Nothing in this file may reach SES, except the one test that explicitly re-patches
    boto3.client AFTER this fixture runs (monkeypatch applies patches in call order, so a
    later setattr in the test body wins)."""

    def _boom(*a, **k):
        raise AssertionError(f"boto3.client() called unexpectedly: {a!r} {k!r}")

    monkeypatch.setattr("boto3.client", _boom)


async def _eager(db_session, user_id) -> User:
    """Re-fetch with User.agent loaded — the real sweep uses selectinload(User.agent)
    (email_notifications.py:194), and a lazy load on an AsyncSession raises MissingGreenlet.
    factories.make_user's returned object does not have the relationship loaded."""
    return (
        await db_session.execute(
            select(User).options(selectinload(User.agent)).where(User.id == user_id)
        )
    ).scalar_one()


async def test_an_outstanding_notification_past_the_expiry_window_is_marked_expired(db_session):
    user = await factories.make_user(db_session, email="pi.expiry@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    old_sent_at = datetime.now(UTC) - timedelta(
        days=get_settings().email_notification_expiry_days + 1
    )
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=f"tok-{uuid.uuid4().hex}", category="proposal_review",
        status="sent", sent_at=old_sent_at,
    )
    db_session.add(notification)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    # Already reviewed, so the fall-through finds nothing to send and never reaches SES —
    # this test is about the expiry itself; the re-send is exercised by the third test below.
    # tests/factories.py has no make_proposal_review at 18ba52c — insert the row directly.
    db_session.add(ProposalReview(
        thread_decision_id=td.id, agent_id=agent.agent_id, user_id=user.id,
        reviewed_by_user_id=user.id, rating=3, submitted_via="web",
    ))
    await db_session.flush()
    notif_id = notification.id

    await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    await db_session.refresh(notification)
    assert notification.status == "expired", (
        "an unanswered proposal_review notification past the expiry window is still 'sent' — "
        "it is immortal, and it permanently blocks a new reminder from ever going out"
    )
    assert notif_id  # keep the id referenced for the failure message


async def test_a_recently_sent_outstanding_notification_is_not_expired(db_session):
    """Control: an outstanding row that is still within the reply window must be left alone —
    otherwise every outstanding reminder would be treated as expired immediately."""
    user = await factories.make_user(db_session, email="pi.fresh@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=f"tok-{uuid.uuid4().hex}", category="proposal_review",
        status="sent", sent_at=datetime.now(UTC),
    )
    db_session.add(notification)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    await db_session.flush()

    await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    await db_session.refresh(notification)
    assert notification.status == "sent"


async def test_expiring_then_resending_does_not_violate_the_uniqueness_constraint(
    db_session, monkeypatch,
):
    """B3: after expiry the sweep falls through and sends again for the SAME proposal —
    uq_email_notification_user_thread_category means the row must be reconciled, not
    re-INSERTed, or the mail goes out and the bookkeeping INSERT fails behind it. This
    exercises Task 21.11's SELECT-then-upsert end to end."""
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    user = await factories.make_user(db_session, email="pi.reexpiry@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    old_token = f"tok-{uuid.uuid4().hex}"
    old_sent_at = datetime.now(UTC) - timedelta(
        days=get_settings().email_notification_expiry_days + 1
    )
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=old_token, category="proposal_review",
        status="sent", sent_at=old_sent_at,
    )
    db_session.add(notification)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    await db_session.flush()
    notif_id = notification.id

    class _RecordingSES:
        def __init__(self):
            self.sent: list[dict] = []

        def send_raw_email(self, **kwargs):
            self.sent.append(kwargs)
            return {"MessageId": "m-1"}

    recorder = _RecordingSES()
    # Overrides the module's no_ses autouse fixture for this test only (monkeypatch applies
    # in call order; this setattr runs after the fixture's).
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    assert sent is True, "the fall-through re-send did not happen after expiry"
    assert len(recorder.sent) == 1, "expected exactly one SES send for the re-sent reminder"

    rows = (
        await db_session.execute(
            select(EmailNotification).where(
                EmailNotification.user_id == user.id,
                EmailNotification.thread_decision_id == td.id,
                EmailNotification.category == "proposal_review",
            )
        )
    ).scalars().all()
    assert [r.id for r in rows] == [notif_id], (
        "expiring then re-sending must reconcile the SAME row, not insert a second one — "
        f"found {len(rows)} row(s) for this (user, proposal, category): {[r.status for r in rows]}"
    )
    assert rows[0].status == "sent"
    assert rows[0].reply_token != old_token, (
        "the reconciled row kept the OLD reply token — a PI replying to the new email "
        "would be validated against a token nobody sent"
    )


async def test_a_monthly_subscribers_ladder_reaches_off_not_a_valueerror(
    db_session, monkeypatch,
):
    """I1 (V4-4a): FREQUENCY_LADDER omitted "monthly" even though it is user-selectable
    (routers/settings.py's VALID_FREQUENCIES, templates/settings.html), so once a
    monthly subscriber's consecutive_missed count reached MISSED_THRESHOLD,
    FREQUENCY_LADDER.index("monthly") raised ValueError -- which the sweep's per-item
    `except` swallows, silently starving that PI of every future proposal_review
    reminder while logging a traceback every cycle. Pre-fix this line was dead code
    (the outstanding row was immortal, so consecutive_missed was pinned at 1); this
    task's expiry fix made MISSED_THRESHOLD reachable, so it is now live.

    Drives three real expiry -> resend cycles (each ~30 days apart, mirroring a real
    monthly cadence) to get consecutive_missed to 3 the normal way, then exercises the
    ladder function directly -- the actual site of the bug.
    """
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    user = await factories.make_user(db_session, email="pi.monthly@scripps.edu")
    user.email_notification_frequency = "monthly"
    agent = await factories.make_agent(db_session, user=user)
    # The unreviewed proposal itself: never referenced by id below because the
    # outstanding-notification query is scoped to (user, category, status), not a
    # specific thread_decision -- its mere existence is what keeps each cycle's
    # fall-through resend finding something to send.
    await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    tracker = EmailEngagementTracker(user_id=user.id, consecutive_missed=0)
    db_session.add(tracker)
    await db_session.flush()

    class _RecordingSES:
        def __init__(self):
            self.sent: list[dict] = []

        def send_raw_email(self, **kwargs):
            self.sent.append(kwargs)
            return {"MessageId": f"m-{len(self.sent)}"}

    monkeypatch.setattr("boto3.client", lambda *a, **k: _RecordingSES())

    now = datetime.now(UTC)
    for cycle in range(3):
        # Age both clocks well past the monthly pacing interval (29d) AND the expiry
        # window, so any outstanding row from the previous cycle reads as expired and
        # a fresh reminder goes out -- three real ~30-day cycles.
        age = timedelta(days=31 * (cycle + 1))
        tracker.last_notification_sent_at = now - age
        outstanding = (
            await db_session.execute(
                select(EmailNotification).where(
                    EmailNotification.user_id == user.id,
                    EmailNotification.category == "proposal_review",
                    EmailNotification.status == "sent",
                )
            )
        ).scalar_one_or_none()
        if outstanding:
            outstanding.sent_at = now - age
        await db_session.flush()

        sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)
        # Per-item commit (mirrors check_and_send_notifications:228) -- the increment
        # below happens AFTER send_proposal_notification's own internal flush, so
        # nothing has persisted it yet; expire_on_commit=False means this commit does
        # not disturb the in-memory objects the rest of the loop still holds.
        await db_session.commit()
        assert sent is True, f"cycle {cycle}: expected a fresh reminder after expiry"

    assert tracker.consecutive_missed == 3, "three expiry->resend cycles must reach MISSED_THRESHOLD"

    # The bug: this call raised ValueError('monthly' is not in list) pre-fix.
    await en._check_engagement_and_downgrade(user, tracker, db_session)

    assert user.email_notification_frequency == "off", (
        "a monthly subscriber past MISSED_THRESHOLD must downgrade past monthly to "
        "off, not crash the sweep"
    )
    assert user.email_notifications_paused_by_system is True
    assert tracker.consecutive_missed == 0
