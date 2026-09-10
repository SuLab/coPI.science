"""V4-3/V4-4a: an unanswered proposal_review reminder expires instead of being immortal, and the
downgrade ladder can advance past its first rung. V4-3's third test also pins B3/M1: expiring then
falling through to a re-send for the SAME proposal must reconcile the existing row, not violate
uq_email_notification_user_thread_category.

Originally (V4-3) `expired` was written only once a REPLACEMENT had been accepted by SES, because
`email_inbound.process_inbound_email` dropped any reply whose notification was not `status ==
"sent"` -- so retiring the row here, with nothing to replace it, would have killed the only reply
address the PI was ever given. RC-4 (audit 2026-09-08, #21 V4-3) closed that gap on the inbound
side: a reply is now refused on its own once `sent_at` is older than
`settings.email_notification_expiry_days`, independent of this row's `status`. That makes it safe
for the SWEEP to mark `expired` on the two bail paths below where nothing was sent AND the
outstanding row was already past the window -- inbound would refuse a reply to it regardless. The
THIRD no-send path (`test_a_failed_ses_send_leaves_the_notification_answerable`) is deliberately
unchanged: an SES-refused send is a transient failure, not a sign there is nothing left to answer,
so that row must stay `sent` and answerable on the next attempt.

Also here (same sweep, same file): the reopen sentinel. `reopen_proposal` files a ProposalReview
with `rating=0`, which `_get_unreviewed_proposals_for_user` and the status_overview digest both
counted as a completed review -- see Task 16's ruling, docs/plans/2026-09-04-decisions/task-16.md.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import src.services.email_inbound as inbound
import src.services.email_notifications as en
from src.config import get_settings
from src.models import EmailEngagementTracker, EmailNotification, ProposalReview, User
from src.services.email_inbound import process_inbound_email
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


async def test_a_sweep_with_nothing_to_send_and_a_lapsed_reminder_expires_it(db_session):
    """RC-4: `_process_user_notifications` returns at its `if not proposals` bail. The
    reply window lapsed, so the sweep falls through — but the proposal has since been
    reviewed on the dashboard, so there is nothing left to send. Nothing will ever
    replace this row, and `email_inbound.process_inbound_email` now refuses a reply to
    it on its own (RC-4's `sent_at` age check) regardless of `status` — so marking it
    `expired` here is bookkeeping, not a functional change to what a reply would do.
    """
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
    # Already reviewed, so the fall-through finds nothing to send and never reaches SES.
    # tests/factories.py has no make_proposal_review at 18ba52c — insert the row directly.
    db_session.add(ProposalReview(
        thread_decision_id=td.id, agent_id=agent.agent_id, user_id=user.id,
        reviewed_by_user_id=user.id, rating=3, submitted_via="web",
    ))
    await db_session.flush()
    notif_id = notification.id

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    assert sent is False, "nothing was sendable, so the sweep must report no send"
    await db_session.refresh(notification)
    assert notification.status == "expired", (
        f"notification {notif_id} is {notification.status!r} after a sweep that found "
        "nothing left to send for an outstanding row already past the reply window — RC-4 "
        "expects it retired here, since email_inbound's own age check would refuse a reply "
        "to it either way."
    )


async def test_an_allowlist_suppressed_sweep_with_a_lapsed_reminder_expires_it(
    db_session, monkeypatch,
):
    """RC-4: the outbound allowlist bail in `_process_user_notifications` (it advances
    the send clock and returns False without sending) marks the outstanding row expired
    when it was already past the reply window — nothing will ever replace it while the
    allowlist blocks this recipient, and a reply to it would be refused by
    email_inbound's own age check regardless. Driven end to end: the PI's reply then
    hits the ordinary `status != "sent"` gate and is dropped, exactly as a reply to any
    other already-expired row would be.
    """
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "someone.else@scripps.edu")
    monkeypatch.setattr(inbound, "_send_simple_email", lambda *a, **k: True)

    async def _classify(body, proposal_summary):
        return {"category": "review", "rating": 4, "comment": "yes", "instruction": ""}

    monkeypatch.setattr(inbound, "classify_reply", _classify)

    user = await factories.make_user(db_session, email="pi.blocked@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    token = f"tok-{uuid.uuid4().hex}"
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=token, category="proposal_review", status="sent",
        sent_at=datetime.now(UTC) - timedelta(
            days=get_settings().email_notification_expiry_days + 1
        ),
    )
    db_session.add(notification)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    await db_session.flush()

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)
    assert sent is False, "the allowlist should have suppressed the replacement"

    row = (
        await db_session.execute(
            select(EmailNotification).where(EmailNotification.reply_token == token)
        )
    ).scalar_one()
    assert row.status == "expired", (
        f"notification {notification.id} is {row.status!r} after an allowlist-suppressed "
        "sweep found the outstanding row already past the reply window — RC-4 expects it "
        "retired here, since nothing will ever replace it while the allowlist blocks this "
        "recipient and email_inbound's own age check would refuse a reply to it either way."
    )

    raw = (
        factories.SES_PASS_HEADER
        + f"From: {user.email}\n"
        + f"To: review+{token}@reply.copi.science\n"
        + 'Content-Type: text/plain; charset="UTF-8"\n'
        + "\n4 excellent\n"
    ).encode()
    await process_inbound_email(raw, db_session)

    await db_session.refresh(row)
    assert row.status == "expired", "a reply to an already-expired row must not be applied"
    review = (
        await db_session.execute(
            select(ProposalReview).where(ProposalReview.thread_decision_id == td.id)
        )
    ).scalar_one_or_none()
    assert review is None, "an expired token must not be able to file a review"


async def test_a_failed_ses_send_leaves_the_notification_answerable(db_session, monkeypatch):
    """V4-3, no-send path 3 of 3: `send_proposal_notification` returns False (SES refused).
    Nothing replaced the outstanding reminder, so its token must keep working.
    """
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    user = await factories.make_user(db_session, email="pi.sesfail@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    notification = EmailNotification(
        user_id=user.id, thread_decision_id=td.id, agent_registry_id=agent.id,
        reply_token=f"tok-{uuid.uuid4().hex}", category="proposal_review", status="sent",
        sent_at=datetime.now(UTC) - timedelta(
            days=get_settings().email_notification_expiry_days + 1
        ),
    )
    db_session.add(notification)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    await db_session.flush()

    class _RefusingSES:
        def send_raw_email(self, **kwargs):
            raise RuntimeError("SES throttled this account")

    monkeypatch.setattr("boto3.client", lambda *a, **k: _RefusingSES())

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    assert sent is False
    await db_session.refresh(notification)
    assert notification.status == "sent", (
        f"the row is {notification.status!r} after a send that SES REFUSED — the PI's only "
        "live reminder was retired in exchange for an e-mail that never left."
    )


async def test_a_replacement_for_a_different_proposal_expires_the_old_row(
    db_session, monkeypatch,
):
    """The other half of V4-3: once SES HAS accepted a replacement, the superseded row must be
    retired — and not merely for tidiness. The outstanding-row lookup is `scalar_one_or_none()`
    over (user, category, status='sent'), so leaving two live rows raises MultipleResultsFound
    and the sweep's per-item `except` then starves that PI every cycle.
    """
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    user = await factories.make_user(db_session, email="pi.twoprops@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td_old = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    td_new = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    # td_old is reviewed, so the fall-through picks td_new and the replacement lands on a
    # DIFFERENT row than the outstanding one.
    db_session.add(ProposalReview(
        thread_decision_id=td_old.id, agent_id=agent.agent_id, user_id=user.id,
        reviewed_by_user_id=user.id, rating=3, submitted_via="web",
    ))
    old = EmailNotification(
        user_id=user.id, thread_decision_id=td_old.id, agent_registry_id=agent.id,
        reply_token=f"tok-{uuid.uuid4().hex}", category="proposal_review", status="sent",
        sent_at=datetime.now(UTC) - timedelta(
            days=get_settings().email_notification_expiry_days + 1
        ),
    )
    db_session.add(old)
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=1))
    await db_session.flush()

    class _RecordingSES:
        def __init__(self):
            self.sent: list[dict] = []

        def send_raw_email(self, **kwargs):
            self.sent.append(kwargs)
            return {"MessageId": "m-1"}

    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    assert sent is True
    assert len(recorder.sent) == 1
    await db_session.refresh(old)
    assert old.status == "expired", (
        f"the superseded row is still {old.status!r} after a replacement went out for a "
        "DIFFERENT proposal — the next sweep's scalar_one_or_none() sees two 'sent' rows and "
        "raises MultipleResultsFound"
    )
    new_row = (
        await db_session.execute(
            select(EmailNotification).where(
                EmailNotification.user_id == user.id,
                EmailNotification.thread_decision_id == td_new.id,
            )
        )
    ).scalar_one()
    assert new_row.status == "sent"


async def test_a_reopened_proposal_is_still_unreviewed_for_the_reminder_sweep(
    db_session, monkeypatch,
):
    """Task 16's rating predicate, in this file's sweep. `reopen_proposal` files a
    ProposalReview with `rating=0` as its marker — the sentinel simulation.py:3510-3512 names
    next to the engine's `-1`, and neither is submittable (agent_page.py:509 and
    email_inbound.py:383 both reject anything outside 1-4).
    `_get_unreviewed_proposals_for_user` excluded only `-1`, so a proposal the PI explicitly
    reopened counted as reviewed and its reminder was never sent again.
    """
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    user = await factories.make_user(db_session, email="pi.reopened@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(db_session, agent_a=agent.agent_id)
    db_session.add(ProposalReview(
        thread_decision_id=td.id, agent_id=agent.agent_id, user_id=user.id,
        reviewed_by_user_id=user.id, rating=0, comment="[Reopened] please refine the budget",
        submitted_via="web",
    ))
    db_session.add(EmailEngagementTracker(user_id=user.id, consecutive_missed=0))
    await db_session.flush()

    class _RecordingSES:
        def __init__(self):
            self.sent: list[dict] = []

        def send_raw_email(self, **kwargs):
            self.sent.append(kwargs)
            return {"MessageId": "m-1"}

    recorder = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)

    assert sent is True, (
        "the reopen marker (rating=0) was counted as a completed review, so the sweep found "
        "nothing outstanding and this PI never gets another reminder about the proposal they "
        "themselves reopened"
    )
    assert len(recorder.sent) == 1


async def test_the_digest_does_not_label_a_reopened_proposal_reviewed(db_session, monkeypatch):
    """Task 16's rating predicate, in the status_overview digest. `_status_label`'s lookup has
    no key for 0, so a reopened proposal fell through to its "reviewed" default and the PI was
    told in writing that a proposal still awaiting their rating had been reviewed. Excluding
    the marker leaves `ratings_by_td` empty for that proposal, which is the existing
    "awaiting your review" branch — no wording changes (D33).
    """
    user = await factories.make_user(db_session, email="pi.digest@scripps.edu")
    agent = await factories.make_agent(db_session, user=user)
    td = await factories.make_thread_decision(
        db_session, agent_a=agent.agent_id, agent_b="somebot",
        decided_at=datetime.now(UTC), summary_text="A joint proteomics screen.",
    )
    db_session.add(ProposalReview(
        thread_decision_id=td.id, agent_id=agent.agent_id, user_id=user.id,
        reviewed_by_user_id=user.id, rating=0, comment="[Reopened] please refine the budget",
        submitted_via="web",
    ))
    await db_session.flush()

    pref = await en.get_or_create_pref(user.id, "status_overview", db_session)
    captured: dict = {}

    def _fake_send(to_email, subject, text_body, html_body, **kwargs):
        captured["text"] = text_body
        return True

    monkeypatch.setattr(en, "_send_html_email", _fake_send)

    assert await en._send_status_overview(await _eager(db_session, user.id), pref, db_session)

    assert "— reviewed" not in captured["text"], (
        "the digest tells the PI a proposal they reopened was 'reviewed':\n"
        + captured["text"]
    )
    assert "— awaiting your review" in captured["text"]


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
        "SEC-F1 (opus review, audit 2026-09-08): expiring then re-sending must mint a "
        "FRESH reply_token — reusing the old one (I2, #21 fix round B) left the FIRST "
        "e-mail's reply address permanently redeemable, since process_inbound_email "
        "looks up purely by token with no per-send identity."
    )


async def test_a_reply_to_the_superseded_email_is_refused_after_a_resend(
    db_session, monkeypatch,
):
    """SEC-F1 (opus review, audit 2026-09-08), the inbound half: I2 (#21 fix round B)
    reused the token across a resend so a PI replying to the SUPERSEDED (pre-resend)
    e-mail still resolved -- but that meant the first e-mail's reply address stayed
    redeemable forever. A resend now mints a fresh token, so a reply quoting the old
    one must be refused (the same "No notification found for token" path a stranger's
    token hits) rather than silently applied against the now-current row."""
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    monkeypatch.setattr(inbound, "_send_simple_email", lambda *a, **k: True)

    async def _classify(body, proposal_summary):
        return {"category": "review", "rating": 3, "comment": "great", "instruction": ""}

    monkeypatch.setattr(inbound, "classify_reply", _classify)

    user = await factories.make_user(db_session, email="pi.oldtoken@scripps.edu")
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

    class _RecordingSES:
        def send_raw_email(self, **kwargs):
            return {"MessageId": "m-1"}

    monkeypatch.setattr("boto3.client", lambda *a, **k: _RecordingSES())

    sent = await en._process_user_notifications(await _eager(db_session, user.id), db_session)
    assert sent is True, "the fall-through re-send did not happen after expiry"

    raw = (
        factories.SES_PASS_HEADER
        + f"From: {user.email}\n"
        + f"To: review+{old_token}@reply.copi.science\n"
        + 'Content-Type: text/plain; charset="UTF-8"\n'
        + "\n3 great idea\n"
    ).encode()
    await process_inbound_email(raw, db_session)

    # The old token was retired by the resend, so it no longer resolves to any row.
    stale = (
        await db_session.execute(
            select(EmailNotification).where(EmailNotification.reply_token == old_token)
        )
    ).scalar_one_or_none()
    assert stale is None, "the superseded token must not still be attached to a row"

    current = (
        await db_session.execute(
            select(EmailNotification).where(
                EmailNotification.user_id == user.id,
                EmailNotification.thread_decision_id == td.id,
            )
        )
    ).scalar_one()
    assert current.status == "sent", (
        "a reply quoting the SUPERSEDED (pre-resend) token must be refused, leaving the "
        "current row's status untouched"
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
