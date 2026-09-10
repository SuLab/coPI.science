"""Reply-path fixes from the 2026-08-14 inbound email rollout.

Two defects observed live during the P2 end-to-end test:

1. A user with no registered email (private ORCID) skipped the sender-match
   check entirely — the reply token plus SPF/DKIM/DMARC were the only controls.
   We now fail closed: no registered address, no email review (the dashboard
   remains that PI's review path).

2. The help email ("Could not process your reply") instructed the PI to reply
   but was sent from noreply@copi.science with no Reply-To — the apex domain's
   MX is Namecheap forwarding, so following the instructions bounced. It now
   carries the notification's reply token in Reply-To (valid because an
   unparseable reply deliberately leaves the notification at status='sent').
   The review/instruction confirmations, whose tokens ARE consumed, instead say
   plainly that replies are not monitored.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import src.services.email_inbound as inbound
from src.config import get_settings
from src.models import AgentChannel, EmailNotification, ProposalReview
from src.services.email_inbound import (
    MAX_REPLIES_PER_TOKEN_PER_HOUR,
    process_inbound_email,
)
from tests import factories
from tests.fakes import FakeSlackClient


def _raw_reply(token: str, from_addr: str, body: str) -> bytes:
    return (
        factories.SES_PASS_HEADER
        + f"From: {from_addr}\n"
        + f"To: review+{token}@reply.copi.science\n"
        + 'Content-Type: text/plain; charset="UTF-8"\n'
        + "\n"
        + body
        + "\n"
    ).encode()


@pytest.fixture(autouse=True)
def _fresh_rate_limit(monkeypatch):
    monkeypatch.setattr(inbound, "_RECENT_REPLY_TIMES", {})
    # Minor 8 (fix round A): without this, the one-email cap set by an earlier test
    # in the same process leaks into a later test keyed by an unrelated notification
    # id colliding only by bad luck — cheap insurance, mirrors the rate-limit reset.
    inbound._INSTRUCTION_FAILURE_EMAILS_SENT.clear()


@pytest.fixture
def sent_emails(monkeypatch):
    """Record _send_simple_email calls instead of hitting SES."""
    calls: list[dict] = []

    def _record(to_email, subject, text_body, reply_to=None):
        calls.append(
            {"to": to_email, "subject": subject, "body": text_body, "reply_to": reply_to}
        )
        return True

    monkeypatch.setattr(inbound, "_send_simple_email", _record)
    return calls


def _classifies_as(monkeypatch, classification: dict):
    async def _classify(body, proposal_summary):
        return {"rating": None, "comment": "", "instruction": "", **classification}

    monkeypatch.setattr(inbound, "classify_reply", _classify)


async def _slack_on(*a, **k):
    """Stub for `slack_tokens.slack_globally_enabled`, forcing the legacy Slack path
    (rather than the DB-only "Slack off" branch) in the fix-round COR-32 tests below."""
    return True


async def _world(db_session, *, recipient_email, token, sent_at=None, category="proposal_review"):
    """An agent-owning PI, a notification recipient, and a live notification."""
    owner = await factories.make_user(db_session)
    recipient = await factories.make_user(db_session, email=recipient_email)
    agent = await factories.make_agent(db_session, user=owner)
    td = await factories.make_thread_decision(
        db_session,
        agent_a=agent.agent_id,
        summary_text="A proposal to collaborate on the thing.",
    )
    notification = EmailNotification(
        user_id=recipient.id,
        thread_decision_id=td.id,
        agent_registry_id=agent.id,
        reply_token=token,
        category=category,
        status="sent",
        **({"sent_at": sent_at} if sent_at is not None else {}),
    )
    db_session.add(notification)
    await db_session.flush()
    return recipient, agent, td, notification


async def _reviews(db_session):
    return (await db_session.execute(select(ProposalReview))).scalars().all()


# --- 0. RC-4: a reply token expires at the consumer, not just on resend -------
#
# Previously `expired` was written only when a REPLACEMENT reminder was sent
# (email_notifications.py), and the inbound side checked only `status == "sent"` --
# so a PI who never got a second reminder (nothing left to send, or suppressed by the
# allowlist) held a bearer credential good forever. `settings.email_notification_
# expiry_days` (default 14) is now enforced here too, measured from `sent_at`.


async def test_a_reply_past_the_expiry_window_is_refused_and_the_row_expires(
    db_session, monkeypatch, sent_emails
):
    token = "expiredtok" + "c" * 40
    old_sent_at = datetime.now(UTC) - timedelta(
        days=get_settings().email_notification_expiry_days + 1
    )
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.expired@scripps.edu", token=token,
        sent_at=old_sent_at,
    )
    _classifies_as(monkeypatch, {"category": "review", "rating": 4, "comment": "great"})

    await process_inbound_email(
        _raw_reply(token, recipient.email, "4 great"), db_session
    )

    assert await _reviews(db_session) == [], "a rating must not be applied past the window"
    await db_session.refresh(notification)
    assert notification.status == "expired"
    assert len(sent_emails) == 1, "the PI should get exactly one expiry notice"
    assert "expired" in sent_emails[0]["subject"].lower()
    assert sent_emails[0]["to"] == recipient.email


async def test_an_expired_new_proposal_reply_is_also_refused(
    db_session, monkeypatch, sent_emails
):
    """The expiry check in process_inbound_email sits after notification lookup and
    sender verification, common to every reply regardless of the outbound e-mail's
    EmailNotification.category -- 'new_proposal' tokens are just as much a bearer
    credential as 'proposal_review' ones, and get no special treatment here."""
    token = "expirednp" + "f" * 40
    old_sent_at = datetime.now(UTC) - timedelta(
        days=get_settings().email_notification_expiry_days + 1
    )
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.expirednp@scripps.edu", token=token,
        sent_at=old_sent_at, category="new_proposal",
    )
    _classifies_as(monkeypatch, {"category": "review", "rating": 4, "comment": "great"})

    await process_inbound_email(
        _raw_reply(token, recipient.email, "4 great"), db_session
    )

    assert await _reviews(db_session) == [], "a rating must not be applied past the window"
    await db_session.refresh(notification)
    assert notification.status == "expired"
    assert len(sent_emails) == 1, "the PI should get exactly one expiry notice"
    assert "expired" in sent_emails[0]["subject"].lower()


async def test_a_reply_inside_the_expiry_window_is_still_applied(
    db_session, monkeypatch, sent_emails
):
    """Control: one day short of the window, the reply is applied exactly as before."""
    token = "freshtok" + "d" * 40
    recent_sent_at = datetime.now(UTC) - timedelta(
        days=get_settings().email_notification_expiry_days - 1
    )
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.fresh@scripps.edu", token=token,
        sent_at=recent_sent_at,
    )
    _classifies_as(monkeypatch, {"category": "review", "rating": 3, "comment": "good"})

    await process_inbound_email(
        _raw_reply(token, recipient.email, "3 good"), db_session
    )

    (review,) = await _reviews(db_session)
    assert review.rating == 3
    await db_session.refresh(notification)
    assert notification.status == "responded"


async def test_a_second_reply_after_expiry_gets_no_further_notice(
    db_session, monkeypatch, sent_emails
):
    """The expiry notice fires at most once per token: once the row is `expired`, a
    later reply hits the pre-existing `status != "sent"` gate before ever reaching the
    new expiry check, so it can't send a second notice."""
    token = "twicetok" + "e" * 40
    old_sent_at = datetime.now(UTC) - timedelta(
        days=get_settings().email_notification_expiry_days + 1
    )
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.twice@scripps.edu", token=token,
        sent_at=old_sent_at,
    )
    _classifies_as(monkeypatch, {"category": "review", "rating": 4, "comment": "great"})

    await process_inbound_email(_raw_reply(token, recipient.email, "4 great"), db_session)
    assert len(sent_emails) == 1

    await process_inbound_email(_raw_reply(token, recipient.email, "4 great again"), db_session)
    assert len(sent_emails) == 1, "a stale token must not earn a second expiry notice"
    assert await _reviews(db_session) == []


# --- 1. Fail closed on a NULL registered email --------------------------------


async def test_reply_for_a_null_email_user_files_nothing(
    db_session, monkeypatch, sent_emails
):
    """No registered address to match the sender against -> reject, even with a
    valid token and passing SES verdicts (previously: token-only auth)."""
    token = "failclosed" + "a" * 40
    _, _, _, notification = await _world(db_session, recipient_email=None, token=token)
    _classifies_as(monkeypatch, {"category": "review", "rating": 3})

    await process_inbound_email(
        _raw_reply(token, "anyone@example.com", "3 great idea"), db_session
    )

    assert await _reviews(db_session) == []
    assert notification.status == "sent"  # nothing consumed, nothing recorded
    assert sent_emails == []  # and no help email to a user with no address


async def test_a_reply_to_an_unknown_token_from_a_registered_user_gets_a_bounce(
    db_session, monkeypatch
):
    """REV3-3 (opus review, audit 2026-09-08): F1's token rotation means a PI
    answering a superseded reminder now hits an unknown token and previously
    got silence. If the From address matches a KNOWN user, send one short
    bounce explaining the link is stale."""
    from src.services.email_notifications import SendOutcome

    bounces = []

    def _record(to_email, subject, text_body, html_body):
        bounces.append({"to": to_email, "subject": subject})
        return SendOutcome.SENT

    monkeypatch.setattr(inbound, "send_html_email_outcome", _record)
    recipient = await factories.make_user(db_session, email="pi.stale@scripps.edu")

    await process_inbound_email(
        _raw_reply("nosuchtoken" + "z" * 39, recipient.email, "1"), db_session
    )

    assert len(bounces) == 1
    assert bounces[0]["to"] == recipient.email
    assert "no longer valid" in bounces[0]["subject"].lower()


async def test_a_reply_to_an_unknown_token_from_an_unknown_address_gets_no_bounce(
    db_session, monkeypatch
):
    """Never bounce to an address that does not match a registered user --
    that would turn this into an oracle for guessing registered emails."""
    from src.services.email_notifications import SendOutcome

    bounces = []
    monkeypatch.setattr(
        inbound, "send_html_email_outcome",
        lambda *a, **k: bounces.append(a) or SendOutcome.SENT,
    )

    await process_inbound_email(
        _raw_reply("nosuchtoken" + "y" * 39, "nobody@example.com", "1"), db_session
    )

    assert bounces == []


async def test_stale_token_bounces_are_capped_per_address(db_session, monkeypatch):
    from src.services.email_notifications import SendOutcome

    bounces = []

    def _record(to_email, subject, text_body, html_body):
        bounces.append(to_email)
        return SendOutcome.SENT

    monkeypatch.setattr(inbound, "send_html_email_outcome", _record)
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    recipient = await factories.make_user(db_session, email="pi.capped@scripps.edu")

    for i in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 2):
        await process_inbound_email(
            _raw_reply(f"nosuchtoken{i}".ljust(48, "w"), recipient.email, "1"), db_session
        )

    assert len(bounces) == inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS


async def test_matching_sender_still_files_a_review_case_insensitively(
    db_session, monkeypatch, sent_emails
):
    """Regression pin for the fail-closed change: a registered user replying
    from their own address (any case) still files the review."""
    token = "caseinsens" + "b" * 40
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="PI.Alpha@Scripps.edu", token=token
    )
    _classifies_as(
        monkeypatch, {"category": "review", "rating": 2, "comment": "needs work"}
    )

    await process_inbound_email(
        _raw_reply(token, "pi.alpha@scripps.edu", "2 needs work"), db_session
    )

    (review,) = await _reviews(db_session)
    assert review.rating == 2
    assert review.submitted_via == "email"
    assert review.agent_id == agent.agent_id
    assert review.reviewed_by_user_id == recipient.id
    assert notification.status == "responded"


# --- 2. The help email is actually reply-able ---------------------------------


async def test_help_email_carries_the_reply_token_in_reply_to(
    db_session, monkeypatch, sent_emails
):
    """The help email tells the PI to reply; without a token Reply-To the reply
    goes to noreply@copi.science and bounces off Namecheap forwarding."""
    token = "helpreply" + "c" * 40
    recipient, _, _, _ = await _world(
        db_session, recipient_email="pi.beta@scripps.edu", token=token
    )
    _classifies_as(monkeypatch, {"category": "unparseable"})

    await process_inbound_email(
        _raw_reply(token, "pi.beta@scripps.edu", "thanks, looks interesting?"),
        db_session,
    )

    (help_email,) = sent_emails
    assert help_email["to"] == recipient.email
    expected = f"review+{token}@{get_settings().ses_reply_domain}"
    assert help_email["reply_to"] == expected


async def test_unparseable_reply_leaves_the_token_answerable(
    db_session, monkeypatch, sent_emails
):
    """An unparseable reply must not consume the notification: a follow-up
    reply with a rating on the SAME token files the review (this retry path is
    what the help email's Reply-To depends on)."""
    token = "retrypath" + "d" * 40
    _, _, _, notification = await _world(
        db_session, recipient_email="pi.gamma@scripps.edu", token=token
    )

    _classifies_as(monkeypatch, {"category": "unparseable"})
    await process_inbound_email(
        _raw_reply(token, "pi.gamma@scripps.edu", "no rating here"), db_session
    )
    assert notification.status == "sent"
    assert await _reviews(db_session) == []

    _classifies_as(monkeypatch, {"category": "review", "rating": 4})
    await process_inbound_email(
        _raw_reply(token, "pi.gamma@scripps.edu", "4 excellent"), db_session
    )
    (review,) = await _reviews(db_session)
    assert review.rating == 4
    assert notification.status == "responded"


async def test_help_emails_are_capped_per_notification(
    db_session, monkeypatch, sent_emails
):
    """Unparseable replies never consume the token (by design), so without a
    ceiling a confused sender — or an autoresponder the RFC 3834 gate misses —
    trades help emails with us at the rate limiter's pace forever."""
    token = "helpcap" + "g" * 40
    _, _, _, notification = await _world(
        db_session, recipient_email="pi.eta@scripps.edu", token=token
    )
    monkeypatch.setattr(inbound, "_HELP_EMAILS_SENT", {})
    _classifies_as(monkeypatch, {"category": "unparseable"})

    for _ in range(inbound.MAX_HELP_EMAILS_PER_NOTIFICATION + 2):
        await process_inbound_email(
            _raw_reply(token, "pi.eta@scripps.edu", "still no rating"), db_session
        )

    assert len(sent_emails) == inbound.MAX_HELP_EMAILS_PER_NOTIFICATION
    assert notification.status == "sent"

    # The cap silences the help emails, never the PI: a rating still lands.
    _classifies_as(monkeypatch, {"category": "review", "rating": 1})
    await process_inbound_email(
        _raw_reply(token, "pi.eta@scripps.edu", "1"), db_session
    )
    (review,) = await _reviews(db_session)
    assert review.rating == 1


async def test_the_reply_rate_limit_survives_a_token_rotation(
    db_session, monkeypatch, sent_emails, caplog
):
    """SEC2-5 (audit 2026-09-08): the reply-rate limiter used to be keyed on
    the reply token itself, but RC-4 made the token rotate on resend — a
    token-keyed limiter's window resets to empty every time the PI's
    notification is resent, so a sender who can trigger resends (or who is
    handed a fresh reminder mid-window) evades the per-notification cap
    entirely. Keying on notification.id closes that: the cap holds across a
    token rotation on the SAME underlying notification."""
    import logging

    token = "rotatecap" + "r" * 39
    _, _, _, notification = await _world(
        db_session, recipient_email="pi.rho@scripps.edu", token=token
    )
    _classifies_as(monkeypatch, {"category": "unparseable"})

    with caplog.at_level(logging.WARNING):
        for i in range(MAX_REPLIES_PER_TOKEN_PER_HOUR):
            await process_inbound_email(
                _raw_reply(token, "pi.rho@scripps.edu", "still no rating"),
                db_session,
            )
            # Simulate RC-4's token rotation (a resend mints a new token for
            # the same notification row) without actually resending.
            token = f"rotatecap{i}" + "s" * 38
            notification.reply_token = token
            await db_session.flush()

        await process_inbound_email(
            _raw_reply(token, "pi.rho@scripps.edu", "still no rating"), db_session
        )

    rate_limited = [
        r for r in caplog.records
        if "Rate limit exceeded" in r.getMessage()
        and str(notification.id) in r.getMessage()
    ]
    assert rate_limited, (
        "the reply after the token rotated must still be rate-limited by "
        "the notification's id"
    )


async def test_an_out_of_range_rating_falls_back_to_the_help_email_path(
    db_session, monkeypatch, sent_emails,
):
    """COR-19.4 coercion pin: _coerce_rating(7) == 7 passes an out-of-range int
    through unchanged — it IS a real int, just not a valid 1-4 rating.
    process_inbound_email's own `rating < 1 or rating > 4` guard is what rejects it,
    downgrading the category to "unparseable" so it falls into the help-email path
    rather than TypeErroring or silently filing a rating=7 review."""
    assert inbound._coerce_rating(7) == 7
    token = "outofrange" + "k" * 39
    recipient, _, _, notification = await _world(
        db_session, recipient_email="pi.iota@scripps.edu", token=token
    )
    _classifies_as(monkeypatch, {"category": "review", "rating": 7})

    await process_inbound_email(
        _raw_reply(token, "pi.iota@scripps.edu", "7 off the scale"), db_session
    )

    assert await _reviews(db_session) == []
    assert notification.status == "sent"
    (mail,) = sent_emails
    assert mail["to"] == recipient.email
    assert "could not process" in mail["subject"].lower()


# --- 2b. Commit before the confirmation send (COR-19.6) ------------------------


async def test_review_confirmation_failure_does_not_roll_back_the_already_committed_review(
    engine, monkeypatch, sent_emails,
):
    """COR-19.6: db.commit() must happen before the SES confirmation send inside
    process_inbound_email, so a send failure (which propagates to poll_inbound_emails'
    per-object except — it does not delete the S3 object, and retries) can't also roll
    back a review that already succeeded. Needs a REAL committing session (not the
    rollback-on-teardown db_session fixture): the defect only shows up across the
    poller's async-with session boundary, which the shared db_session fixture papers
    over (attribute mutations are visible in-session whether or not a real commit ran).
    """
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    token = "commitfirst" + "f" * 40

    async def _boom(*a, **k):
        raise RuntimeError("SES throttled")

    monkeypatch.setattr(inbound, "_send_review_confirmation", _boom)
    _classifies_as(monkeypatch, {"category": "review", "rating": 3})

    async with factory() as db:
        recipient, agent, td, notification = await _world(
            db, recipient_email="pi.commit2@scripps.edu", token=token
        )
        await db.commit()
        notif_id, td_id, agent_id, recipient_id, owner_id, td_run_id = (
            notification.id, td.id, agent.id, recipient.id, agent.user_id,
            td.simulation_run_id,
        )

    try:
        # Mirrors exactly what poll_inbound_emails does: one committing session,
        # process_inbound_email raising past its own internal commit.
        with pytest.raises(RuntimeError):
            async with factory() as db:
                await process_inbound_email(
                    _raw_reply(token, "pi.commit2@scripps.edu", "3 sounds great"), db,
                )
                await db.commit()  # mirrors poll_inbound_emails's own commit call

        # A FRESH session/connection must see the review as durably committed — proving
        # process_inbound_email's own commit landed before the send that then failed,
        # not merely flushed-and-later-lost when the session above closed.
        async with factory() as verify_db:
            notif = await verify_db.get(EmailNotification, notif_id)
            assert notif.status == "responded", (
                "the review's own commit must precede the confirmation send, so a send "
                "failure does not roll back work that already succeeded"
            )
            reviews = (await verify_db.execute(
                select(ProposalReview).where(ProposalReview.thread_decision_id == td_id)
            )).scalars().all()
            assert len(reviews) == 1 and reviews[0].rating == 3
    finally:
        async with factory() as cleanup_db:
            await cleanup_db.execute(
                text("DELETE FROM proposal_reviews WHERE thread_decision_id = :t"), {"t": td_id}
            )
            await cleanup_db.execute(
                text("DELETE FROM email_notifications WHERE id = :i"), {"i": notif_id}
            )
            await cleanup_db.execute(text("DELETE FROM thread_decisions WHERE id = :t"), {"t": td_id})
            await cleanup_db.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
            await cleanup_db.execute(
                text("DELETE FROM users WHERE id IN (:r, :o)"), {"r": recipient_id, "o": owner_id}
            )
            # _world() (via factories.make_thread_decision) creates a SimulationRun when no run
            # is passed — thread_decisions.simulation_run_id is CASCADE only in the OTHER
            # direction, so without this delete the run survives every run of this test,
            # permanently, in the session-scoped test database. get_latest_run_id
            # (src/services/pi_inbox.py:26-30) and _latest_simulation_run_id
            # (src/services/private_channels.py:200-215) both resolve "the latest run" by
            # `ORDER BY started_at DESC LIMIT 1`, so a leaked row is exactly the shape that
            # makes another suite flaky.
            await cleanup_db.execute(
                text("DELETE FROM simulation_runs WHERE id = :r"), {"r": td_run_id}
            )
            await cleanup_db.commit()

        # Leak guard: if a future change to _world() starts sharing a run across tests (or
        # this cleanup is ever trimmed), this catches the leak here instead of it silently
        # reappearing as flakiness in an unrelated suite.
        async with factory() as check_db:
            assert await check_db.scalar(
                text("SELECT count(*) FROM simulation_runs WHERE id = :r"), {"r": td_run_id}
            ) == 0, "this test committed a simulation_runs row it did not clean up"


# --- 2b2. A retried inbound e-mail and the private-channel migration (COR-19.6) --


@pytest.fixture
def slack_migration_stub(monkeypatch):
    """Force the ONLINE Slack migration path in migrate_public_thread_to_private with
    FakeSlackClient doubles, mirroring test_proposal_review.py's `slack_on` fixture
    (kept local rather than imported across test files). Returns the list of fake
    clients constructed -- `sum(len(c.created_channels) for c in made)` counts every
    private channel the migration actually asked Slack to create, across as many
    calls to process_inbound_email as the test drives."""
    made: list[FakeSlackClient] = []

    async def _on(*args, **kwargs):
        return True

    async def _token(db, agent_id):
        return f"xoxb-fake-{agent_id}"

    def _client(agent_id, bot_token):
        c = FakeSlackClient(agent_id=agent_id, bot_token=bot_token)
        # Offset each instance's ts counter so a SECOND migration attempt (this
        # test's whole point) doesn't collide with the first's on
        # uq_agent_messages_run_ts -- FakeSlackClient always starts a fresh
        # instance's _ts at the same constant, which two real Slack API calls,
        # minutes apart, never would.
        c._ts += len(made) * 100_000
        made.append(c)
        return c

    monkeypatch.setattr(
        "src.services.private_channels._slack_enabled_for_migration", _on
    )
    monkeypatch.setattr(
        "src.services.private_channels._get_or_fail_bot_token", _token
    )
    monkeypatch.setattr("src.services.private_channels._make_client", _client)
    return made


async def test_a_retried_inbound_email_does_not_create_a_second_private_channel(
    engine, monkeypatch, slack_migration_stub,
):
    """COR-19.6: a retried inbound e-mail must not mint a second private channel.

    Two guards are needed, and this pins both. `migrate_public_thread_to_private`
    commits its own AgentChannel/member/handover rows and
    `thread_decisions.refined_in_channel` as soon as its Slack side effects are
    irreversible (private_channels.py, commit 34d3c15). That alone was NOT enough, and
    this test is what disproved it: `_handle_instruction`'s original guard only looked
    for a ProposalReview row, never at `refined_in_channel`, and the migration never
    flips `origin_visibility` away from 'public'. So when the OUTER
    `process_inbound_email` commit failed after the migration's own commit had landed,
    the review row and the notification flip were rolled back; the retry over the same
    S3 object found `notification.status` still 'sent' and no review row, and ran the
    migration end to end again -- two AgentChannel rows with the same channel_name, and
    a second real Slack channel requested.

    The second guard (email_inbound.py, "Second idempotency guard") reads
    `refined_in_channel` and skips the migration when it is already set, letting the
    retry go on to record the review row and retire the notification. The first
    attempt's handover already put this guidance in the private channel, so nothing is
    re-posted. This ran as a strict xfail while the claim was false; it is a plain
    regression pin now that both guards are in place.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    token = "retrychannel" + "z" * 38
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    async with factory() as db:
        recipient, agent, td, notification = await _world(
            db, recipient_email="pi.retry@scripps.edu", token=token
        )
        await db.commit()
        notif_id, td_id, agent_id, recipient_id, owner_id, run_id = (
            notification.id, td.id, agent.id, recipient.id, agent.user_id,
            td.simulation_run_id,
        )

    try:
        # --- Pass 1: the migration's own commit lands; the caller's later commit
        # (the 2nd db.commit() this session makes) is forced to fail.
        async with factory() as db1:
            real_commit = db1.commit
            calls = {"n": 0}

            async def _commit_second_fails():
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("db gone away between the migration commit and retire")
                await real_commit()

            monkeypatch.setattr(db1, "commit", _commit_second_fails)

            with pytest.raises(RuntimeError, match="db gone away"):
                await process_inbound_email(
                    _raw_reply(token, "pi.retry@scripps.edu", "please focus on X"), db1,
                )

        async with factory() as verify1:
            notif = await verify1.get(EmailNotification, notif_id)
            assert notif.status == "sent", (
                "the notification-status update must have been lost along with the "
                "outer commit that raised"
            )
            reviews = (await verify1.execute(
                select(ProposalReview).where(ProposalReview.thread_decision_id == td_id)
            )).scalars().all()
            assert reviews == [], "the review add must have been lost along with the outer commit"
            channels = (await verify1.execute(
                select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
            )).scalars().all()
            assert len(channels) == 1, (
                "the migration's OWN commit (private_channels.py:625) must survive the "
                "later, unrelated commit failure"
            )
        assert sum(len(c.created_channels) for c in slack_migration_stub) == 1

        # --- Pass 2: a genuine retry over the same S3 object.
        async with factory() as db2:
            await process_inbound_email(
                _raw_reply(token, "pi.retry@scripps.edu", "please focus on X"), db2,
            )
            await db2.commit()

        async with factory() as verify2:
            channels = (await verify2.execute(
                select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
            )).scalars().all()
            assert len(channels) == 1, (
                "a retried inbound e-mail must not mint a second private channel"
            )
        assert sum(len(c.created_channels) for c in slack_migration_stub) == 1, (
            "the retry must not have asked Slack to create a second private channel"
        )
    finally:
        async with factory() as cleanup_db:
            await cleanup_db.execute(
                text(
                    "DELETE FROM private_channel_members WHERE agent_channel_id IN "
                    "(SELECT id FROM agent_channels WHERE simulation_run_id = :r)"
                ),
                {"r": run_id},
            )
            await cleanup_db.execute(
                text("DELETE FROM agent_messages WHERE simulation_run_id = :r"), {"r": run_id}
            )
            await cleanup_db.execute(
                text("DELETE FROM agent_channels WHERE simulation_run_id = :r"), {"r": run_id}
            )
            await cleanup_db.execute(
                text("DELETE FROM proposal_reviews WHERE thread_decision_id = :t"), {"t": td_id}
            )
            await cleanup_db.execute(
                text("DELETE FROM email_notifications WHERE id = :i"), {"i": notif_id}
            )
            await cleanup_db.execute(
                text("DELETE FROM thread_decisions WHERE id = :t"), {"t": td_id}
            )
            await cleanup_db.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
            await cleanup_db.execute(
                text("DELETE FROM users WHERE id IN (:r, :o)"), {"r": recipient_id, "o": owner_id}
            )
            await cleanup_db.execute(
                text("DELETE FROM simulation_runs WHERE id = :r"), {"r": run_id}
            )
            await cleanup_db.commit()


# --- 2c. A failed instruction post notifies the PI (COR-32 fix round) ----------


@pytest.mark.parametrize("kind", ["plain_exception", "db_flavored"])
async def test_a_terminal_migration_failure_notifies_the_pi_and_retires_the_notification(
    db_session, monkeypatch, sent_emails, kind,
):
    """COR-32 fix round, Critical #1: migrate_public_thread_to_private creates a real
    Slack channel (and DB rows) before most of its work — it is NOT idempotent. Letting
    a failure inside it raise (21.9's original fix) meant the S3 object retried up to
    MAX_S3_PROCESS_ATTEMPTS times, minting up to that many orphan channels. A failure
    here must instead be terminal: notify the PI with the "will not be retried" wording
    and return False so the caller retires the notification and commits as usual — at
    most ONE orphan channel results, matching pre-COR-32 parity, except the PI now finds
    out. Forces the failure on the DEFAULT-config path (enable_private_refinement=True,
    td.origin_visibility='public', both factory defaults) — reachable in production
    today, not just the legacy flag-off path.

    Parametrised (fix round B, audit #21 C1) over TWO failure shapes:
    - "plain_exception": the original RuntimeError, which leaves the session clean.
    - "db_flavored": a genuine flush-time IntegrityError (a duplicate reply_token,
      mirroring migrate_public_thread_to_private's own db.flush() in
      private_channels.py failing mid-write), which poisons the session — every
      subsequent attribute read, even of an already-loaded value, raises
      PendingRollbackError until the session is rolled back. This sub-case was
      previously untested; pre-fix it escaped _handle_instruction's own
      `logger.error(..., td.thread_id, ...)` as a raw PendingRollbackError before the
      PI could be notified, minting up to MAX_S3_PROCESS_ATTEMPTS orphan channels.

    The fixture data is committed (not just flushed) before the failure is forced so
    that the DB-flavored sub-case's `db.rollback()` — which rolls back to the most
    recent savepoint under this session's `join_transaction_mode="create_savepoint"`
    — discards only the failing migration's own writes, not `_world`'s, matching how
    production reaches this code (the notification row was committed by an entirely
    separate, earlier request).
    """
    token = "instrfail" + kind[:1] + "e" * 30
    recipient, agent, td, notification = await _world(
        db_session, recipient_email=f"pi.instr.{kind}@scripps.edu", token=token
    )
    await db_session.commit()
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    calls: list[bool] = []

    if kind == "plain_exception":

        async def _boom(*a, **k):
            calls.append(True)
            raise RuntimeError("Slack outage")
    else:

        async def _boom(db, **k):
            calls.append(True)
            dupe = EmailNotification(
                user_id=recipient.id,
                thread_decision_id=td.id,
                agent_registry_id=agent.id,
                reply_token=notification.reply_token,  # UNIQUE collision -> IntegrityError
                category="proposal_review",
                status="sent",
            )
            db.add(dupe)
            await db.flush()

    # migrate_public_thread_to_private is imported with a LOCAL `from ... import` inside
    # _handle_instruction, so it is looked up fresh at call time — patching the source
    # module's attribute (not email_inbound's namespace, which has no such name at
    # module scope) is what actually takes effect.
    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _boom
    )

    await process_inbound_email(
        _raw_reply(token, f"pi.instr.{kind}@scripps.edu", "please focus on X"), db_session,
    )

    assert calls == [True]
    assert notification.status == "responded", "a terminal failure retires the notification"
    assert await _reviews(db_session) == []
    (mail,) = sent_emails
    assert mail["to"] == recipient.email
    assert "instruction" in mail["subject"].lower()
    assert "will not be retried" in mail["body"]

    # The notification is already retired, so a second reply on the same token hits
    # process_inbound_email's own "already responded" early return and never reaches
    # _handle_instruction (let alone the migration) again.
    await process_inbound_email(
        _raw_reply(token, f"pi.instr.{kind}@scripps.edu", "please focus on X"), db_session,
    )
    assert calls == [True], "a second reply on an already-retired notification re-ran the migration"
    assert len(sent_emails) == 1, "a second reply sent a second failure email"


async def test_a_legacy_pre_slack_failure_still_raises_and_retries(
    db_session, monkeypatch, sent_emails,
):
    """COR-32 fix round, rule 2: the three legacy failures that happen BEFORE any Slack
    mutation (no simulation run, no bot token, channel not found) are still safe to
    retry — unlike the migration failure above, nothing irreversible has happened yet.
    This exercises "no bot token": settings.enable_private_refinement=False routes into
    the legacy branch, slack_globally_enabled is stubbed True (skip the DB-only path),
    and a freshly-factoried agent has no bot token in the DB or in the test env's
    ``.env`` fallback."""
    token = "legacyfail" + "h" * 39
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.instr3@scripps.edu", token=token
    )
    monkeypatch.setattr(get_settings(), "enable_private_refinement", False)
    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _slack_on)
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(
            _raw_reply(token, "pi.instr3@scripps.edu", "please focus on X"), db_session,
        )

    assert notification.status == "sent", "a safe-to-retry failure must not retire the notification"
    assert await _reviews(db_session) == []
    (mail,) = sent_emails
    assert mail["to"] == recipient.email
    assert "We'll retry automatically" in mail["body"]
    assert "will not be retried" not in mail["body"]


# --- 2c-bis. A PRE-mutation migration failure is retried, not consumed (COR-32) --
#
# The terminal path above exists because migrate_public_thread_to_private is not
# idempotent ONCE IT HAS CREATED A SLACK CHANNEL. Everything it does before that --
# resolving the run, both bot tokens, both authenticated clients, the other bot's user
# id, and conversations.create itself when Slack answers and refuses -- leaves nothing
# on Slack and nothing committed in the DB. Consuming the PI's instruction for one of
# those is the COR-32 defect: a DNS blip, a throttled auth.test or a rotated token
# silently discards a real instruction.


class _NoAuthClient(FakeSlackClient):
    """connect() fails -- `_make_client`'s raise site (private_channels.py:196)."""

    def connect(self) -> bool:
        return False


class _NoBotUserIdClient(FakeSlackClient):
    """auth.test came back without a user_id -- private_channels.py:481."""

    @property
    def bot_user_id(self):
        return None


class _RefusingClient(FakeSlackClient):
    """conversations.create answered and refused -- private_channels.py:486."""

    def create_private_channel(self, name):
        return None


def _force_pre_mutation_failure(monkeypatch, case: str) -> list:
    """Drive the REAL migration into one of its pre-Slack-mutation raise sites.

    Each case fails at the raise site itself: a double standing in for
    `migrate_public_thread_to_private` wholesale (what the terminal tests above use)
    would prove nothing about *where* the boundary sits. Returns the Slack client
    doubles the migration constructed, so a caller can assert conversations.create was
    never asked for a channel.
    """
    made: list[FakeSlackClient] = []

    async def _on(*a, **k):
        return True

    monkeypatch.setattr("src.services.private_channels._slack_enabled_for_migration", _on)

    if case == "no_bot_token":
        async def _no_token(db, agent_id):
            return None

        # _get_or_fail_bot_token (private_channels.py:188) imports this by name inside
        # the function body, so patching the source module is what takes effect.
        monkeypatch.setattr("src.services.slack_tokens.get_agent_bot_token", _no_token)
        return made

    async def _token(db, agent_id):
        return f"xoxb-fake-{agent_id}"

    monkeypatch.setattr("src.services.private_channels._get_or_fail_bot_token", _token)

    cls = {
        "client_auth_fails": _NoAuthClient,
        "no_bot_user_id": _NoBotUserIdClient,
        "slack_refuses_create": _RefusingClient,
    }[case]

    def _build(agent_id, bot_token):
        client = cls(agent_id=agent_id, bot_token=bot_token)
        made.append(client)
        return client

    monkeypatch.setattr("src.services.private_channels.AgentSlackClient", _build)
    return made


@pytest.mark.parametrize(
    "case",
    ["no_bot_token", "client_auth_fails", "no_bot_user_id", "slack_refuses_create"],
)
async def test_a_pre_mutation_migration_failure_is_retried_not_consumed(
    db_session, monkeypatch, sent_emails, case,
):
    """#21 COR-32: "on a failed post, don't mark-responded and don't delete the object".

    All four cases fail inside `migrate_public_thread_to_private` before it has asked
    Slack for a channel, so nothing irreversible has happened on Slack OR in the DB.
    The instruction must therefore survive: InstructionApplyFailed propagates out of
    process_inbound_email, which leaves the notification at status='sent' and (in
    poll_inbound_emails) leaves the S3 object in place for the next poll. Pre-fix,
    every one of these took the terminal arm added for the post-mutation case: the
    notification was retired, the object deleted, and the PI told their instruction
    "will not be retried" -- for a transient DNS/throttle/token blip.
    """
    token = f"premut{case}".ljust(48, "p")[:48]
    recipient, agent, td, notification = await _world(
        db_session, recipient_email=f"pi.premut.{case}@scripps.edu", token=token
    )
    # Commit the fixture first: the failure handler rolls back (a DB-flavored failure
    # needs it), and that rollback must discard only the migration's own writes.
    await db_session.commit()
    run_id = td.simulation_run_id
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    made = _force_pre_mutation_failure(monkeypatch, case)

    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(
            _raw_reply(token, f"pi.premut.{case}@scripps.edu", "please focus on X"),
            db_session,
        )

    await db_session.refresh(notification)
    assert notification.status == "sent", (
        "a pre-mutation failure must not retire the notification -- a resend would "
        "then hit the status != 'sent' bail and the instruction is gone"
    )
    assert await _reviews(db_session) == []
    channels = (await db_session.execute(
        select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
    )).scalars().all()
    assert channels == [], "nothing may have been written before a pre-mutation failure"
    assert sum(len(c.created_channels) for c in made) == 0, (
        "the migration must not have reached conversations.create"
    )
    (mail,) = sent_emails
    assert "We'll retry automatically" in mail["body"]
    assert "will not be retried" not in mail["body"]


async def test_a_retried_pre_mutation_failure_applies_the_instruction_exactly_once(
    db_session, monkeypatch, sent_emails, slack_migration_stub,
):
    """The retry has to be worth having: it must apply the instruction, once.

    Poll 1 fails at `_get_or_fail_bot_token` (a rotated/blipped token) and keeps the S3
    object; poll 2 re-delivers the same object with the blip over. The end state must be
    exactly one private channel (on Slack AND in agent_channels), exactly one review
    row, a retired notification, and exactly two emails to the PI -- the retry notice
    and the confirmation. Routing the exception correctly is not enough on its own: a
    retry that duplicated the channel, the review or the mail would be a worse defect
    than the one COR-32 describes.
    """
    addr = "pi.premut.retry@scripps.edu"
    token = "premutretry".ljust(48, "q")[:48]
    recipient, agent, td, notification = await _world(
        db_session, recipient_email=addr, token=token
    )
    await db_session.commit()
    run_id, td_id = td.simulation_run_id, td.id
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    attempts = {"n": 0}

    async def _flaky_token(db, agent_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("slack.com temporarily unresolvable")
        return f"xoxb-fake-{agent_id}"

    # Overrides slack_migration_stub's own _get_or_fail_bot_token patch.
    monkeypatch.setattr("src.services.private_channels._get_or_fail_bot_token", _flaky_token)

    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(_raw_reply(token, addr, "please focus on X"), db_session)

    await db_session.refresh(notification)
    assert notification.status == "sent"
    assert sum(len(c.created_channels) for c in slack_migration_stub) == 0

    # Poll 2: the same S3 object, re-delivered.
    await process_inbound_email(_raw_reply(token, addr, "please focus on X"), db_session)
    await db_session.commit()

    channels = (await db_session.execute(
        select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
    )).scalars().all()
    assert len(channels) == 1, "the retry must mint exactly one private channel"
    assert sum(len(c.created_channels) for c in slack_migration_stub) == 1
    reviews = (await db_session.execute(
        select(ProposalReview).where(ProposalReview.thread_decision_id == td_id)
    )).scalars().all()
    assert len(reviews) == 1 and reviews[0].rating == 0
    await db_session.refresh(notification)
    assert notification.status == "responded"
    assert len(sent_emails) == 2, (
        f"expected the retry notice + the confirmation, got {[m['subject'] for m in sent_emails]}"
    )
    assert not any("will not be retried" in m["body"] for m in sent_emails)


async def test_a_failed_failure_notification_send_does_not_consume_the_cap(
    db_session, monkeypatch,
):
    """Minor 3: _INSTRUCTION_FAILURE_EMAILS_SENT must be set only when the send
    actually succeeded. Pre-fix, a failed send (SES throttled, allowlist suppression,
    ...) still consumed the cap, so the PI could end up with ZERO failure emails ever,
    forever, for that notification even though the underlying problem might later be
    fixed. Same "no bot token" legacy failure as above, replayed twice."""
    token = "capnoteat" + "j" * 40
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.instr4@scripps.edu", token=token
    )
    monkeypatch.setattr(get_settings(), "enable_private_refinement", False)
    monkeypatch.setattr("src.services.slack_tokens.slack_globally_enabled", _slack_on)
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    sent: list[dict] = []
    outcomes = iter([False, True])

    def _send(to_email, subject, text_body, reply_to=None):
        sent.append({"to": to_email, "subject": subject, "body": text_body})
        return next(outcomes)

    monkeypatch.setattr(inbound, "_send_simple_email", _send)

    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(
            _raw_reply(token, "pi.instr4@scripps.edu", "please focus on X"), db_session,
        )
    assert len(sent) == 1, "the first (failed) send must still be attempted"

    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(
            _raw_reply(token, "pi.instr4@scripps.edu", "please focus on X"), db_session,
        )
    assert len(sent) == 2, "the cap must not have been consumed by the earlier failed send"


async def test_a_commit_failure_after_retiring_the_notification_keeps_the_cap_set(
    db_session, monkeypatch, sent_emails,
):
    """Item 2 (COR-32 fix round A tidy): the cap pop() moved to AFTER the commit that
    retires the notification, and only fires when that commit actually succeeds. Reuses
    the terminal-migration-failure setup (which sets the cap via one successful failure
    email), then makes the RETIRING commit itself raise: process_inbound_email must
    propagate the failure (the S3 object is retried, per COR-32) and the cap must stay
    set — clearing it here would let a subsequent retry's own migration failure (another
    orphan channel) re-notify the PI a second time for what looks, from their side, like
    the exact same failure."""
    token = "commitboom" + "k" * 39
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.instr5@scripps.edu", token=token
    )
    # C1 (fix round B): the migration-failure handler now unconditionally rolls back
    # (a DB-flavored failure needs it; a plain one is a no-op rollback). Commit the
    # fixture first so that rollback only discards the failing migration's own writes,
    # not `_world`'s — matching production, where the notification row was committed
    # by an entirely separate, earlier request.
    await db_session.commit()
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    async def _boom_migrate(*a, **k):
        raise RuntimeError("Slack outage")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _boom_migrate
    )

    async def _boom_commit(*a, **kw):
        raise RuntimeError("db gone away")

    monkeypatch.setattr(db_session, "commit", _boom_commit)

    with pytest.raises(RuntimeError, match="db gone away"):
        await process_inbound_email(
            _raw_reply(token, "pi.instr5@scripps.edu", "please focus on X"), db_session,
        )

    assert len(sent_emails) == 1, "the terminal-failure email must still have been sent"
    assert inbound._INSTRUCTION_FAILURE_EMAILS_SENT.get(str(notification.id)) == 1, (
        "a failed commit must not clear the cap"
    )


async def test_a_fault_inside_the_terminal_notify_still_returns_false(
    db_session, monkeypatch,
):
    """Item 4 (COR-32 fix round A tidy): the terminal-migration-failure handler's own
    call to _notify_instruction_failure(will_retry=False) must not let a fault from
    THAT call fall through to _handle_instruction's outer blanket `except Exception` —
    that handler re-raises as InstructionApplyFailed(will_retry=True), which would turn
    a terminal (at-most-one-orphan-channel) failure back into a retried one and risk
    minting a SECOND orphan private channel on the retry."""
    token = "notifyboom" + "m" * 39
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.instr6@scripps.edu", token=token
    )

    async def _boom_migrate(*a, **k):
        raise RuntimeError("Slack outage")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _boom_migrate
    )

    def _boom_notify(*a, **k):
        raise RuntimeError("SES is down too")

    monkeypatch.setattr(inbound, "_notify_instruction_failure", _boom_notify)

    reopened = await inbound._handle_instruction(
        user=recipient, notification=notification, td=td,
        instruction="focus on X", db=db_session,
    )

    assert reopened is False


# --- 2d. D6: an implicit rating=-1 marker is upgraded, not "already acted on" --


async def test_explicit_email_review_upgrades_the_engines_implicit_rating_marker(
    db_session, monkeypatch, sent_emails,
):
    """D6/COR-13: Task 20.9 has the engine persist an implicit
    ProposalReview(rating=-1, submitted_via="engine") the first time a PI engages a
    proposal thread. That row is NOT "already acted on" — pre-ruling, `_handle_review`'s
    `if existing: return` silently dropped the PI's real rating reply forever (the
    unique constraint on (thread_decision_id, agent_id) means a second insert isn't an
    option either). The first explicit e-mail review must upgrade that row in place."""
    token = "d6review" + "a" * 41
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.d6a@scripps.edu", token=token
    )
    implicit_reviewed_at = datetime.now(UTC) - timedelta(days=3)
    implicit = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
        reviewed_at=implicit_reviewed_at,
    )
    db_session.add(implicit)
    await db_session.flush()
    implicit_id = implicit.id

    _classifies_as(monkeypatch, {"category": "review", "rating": 3, "comment": "great"})

    await process_inbound_email(
        _raw_reply(token, "pi.d6a@scripps.edu", "3 great"), db_session
    )

    reviews = await _reviews(db_session)
    assert len(reviews) == 1, "the implicit marker must be upgraded in place, not duplicated"
    (review,) = reviews
    assert review.id == implicit_id, "the same row must be reused (upsert, not a second insert)"
    assert review.rating == 3
    assert review.comment == "great"
    assert review.submitted_via == "email"
    assert review.reviewed_by_user_id == recipient.id
    assert notification.status == "responded"
    # D6 amendment: reviewed_at must move forward to when the explicit action
    # happened, not stay frozen at the engine's implicit-marker timestamp.
    assert review.reviewed_at > implicit_reviewed_at
    # Every side effect that follows a fresh insert must still fire.
    assert len(sent_emails) == 1, "_send_review_confirmation must still fire on the upgrade path"


async def test_an_email_rating_upgrades_the_reopen_sentinel_instead_of_being_discarded(
    db_session, monkeypatch, sent_emails,
):
    """The rating=0 twin of the -1 case above, and the fix for audit finding D2.

    Three shipped behaviours have to fit together, and they do:
      * the reminder sweep CHASES a reopened proposal -- it is outstanding, the PI's
        attention is needed (`test_a_reopened_proposal_is_still_unreviewed_for_the
        _reminder_sweep`);
      * the WEB form is deliberately not re-offered while it is being refined
        (`test_reopen_opens_the_private_channel_and_files_the_review_together`);
      * so the e-mail reply is the PI's path to rating it -- and THIS path read the
        rating=0 sentinel as "already reviewed" and returned without writing, while
        `_send_review_confirmation` still replied "Got it - you rated it 4".

    An audit measured 11 of 26 notifiable users in that loop: reminded, answered
    politely, never recorded. A marker is not a review; it upgrades in place.
    """
    token = "d6reopen0" + "a" * 40
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.zero@scripps.edu", token=token
    )
    sentinel_reviewed_at = datetime.now(UTC) - timedelta(days=2)
    sentinel = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=0,
        comment="[Reopened] please broaden the target list",
        submitted_via="web",
        reviewed_at=sentinel_reviewed_at,
    )
    db_session.add(sentinel)
    await db_session.flush()
    sentinel_id = sentinel.id

    _classifies_as(monkeypatch, {"category": "review", "rating": 4, "comment": "much better"})

    await process_inbound_email(
        _raw_reply(token, "pi.zero@scripps.edu", "4 much better"), db_session
    )

    reviews = await _reviews(db_session)
    assert len(reviews) == 1, "the reopen sentinel must be upgraded in place, not duplicated"
    (review,) = reviews
    assert review.id == sentinel_id, "the same row must be reused (upsert, not a second insert)"
    assert review.rating == 4, (
        "the PI's rating was discarded: the reopen sentinel was read as a completed "
        "review, which is the reminder loop the audit measured"
    )
    assert review.comment == "much better"
    assert review.submitted_via == "email"
    assert review.reviewed_by_user_id == recipient.id
    assert notification.status == "responded", (
        "the notification must be retired, or the sweep keeps chasing and the loop stays open"
    )
    assert review.reviewed_at > sentinel_reviewed_at
    assert len(sent_emails) == 1


async def test_explicit_email_reopen_upgrades_the_engines_implicit_rating_marker(
    db_session, monkeypatch, sent_emails,
):
    """D6/COR-13: same rule for the other writer in this file. Pre-ruling,
    `_handle_instruction`'s `if already: return False` treated the engine's implicit
    rating=-1 row as a completed reopen and silently dropped the PI's real instruction —
    the guidance post never ran and the PI was told nothing."""
    token = "d6instr" + "b" * 43
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.d6b@scripps.edu", token=token
    )
    implicit_reviewed_at = datetime.now(UTC) - timedelta(days=3)
    implicit = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
        reviewed_at=implicit_reviewed_at,
    )
    db_session.add(implicit)
    await db_session.flush()
    implicit_id = implicit.id

    calls: list[str] = []

    class _MigrateResult:
        channel_name = "priv-d6instr"

    async def _migrate_stub(
        db, *, thread_decision, creator_agent_id, creator_pi_user, guidance_text,
        progress=None,
    ):
        calls.append(guidance_text)
        thread_decision.refined_in_channel = _MigrateResult.channel_name
        return _MigrateResult()

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _migrate_stub
    )

    reopened = await inbound._handle_instruction(
        user=recipient, notification=notification, td=td,
        instruction="focus on X", db=db_session,
    )

    assert reopened is True
    assert calls == ["focus on X"], "the guidance post must actually run, not be skipped"

    reviews = await _reviews(db_session)
    assert len(reviews) == 1, "the implicit marker must be upgraded in place, not duplicated"
    (review,) = reviews
    assert review.id == implicit_id, "the same row must be reused (upsert, not a second insert)"
    assert review.rating == 0
    assert review.comment == "[Reopened via email] focus on X"
    assert review.submitted_via == "email"
    assert review.reviewed_by_user_id == recipient.id
    # D6 amendment: reviewed_at must move forward to when the explicit reopen
    # happened, not stay frozen at the engine's implicit-marker timestamp.
    assert review.reviewed_at > implicit_reviewed_at


async def test_an_explicit_rating_still_blocks_a_duplicate_review_and_reopen_by_email(
    db_session, monkeypatch, sent_emails,
):
    """Control for the D6 upgrade above: a row with a REAL rating (not the engine's
    rating=-1 marker) keeps today's rejection for both writers in this file — the
    unqualified "already acted on" short-circuit still applies once a PI (or a prior
    e-mail action) has actually reviewed or reopened."""
    token = "d6control" + "c" * 41
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.d6c@scripps.edu", token=token
    )
    explicit_reviewed_at = datetime.now(UTC) - timedelta(days=3)
    explicit = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=3,
        comment="already rated",
        submitted_via="email",
        reviewed_at=explicit_reviewed_at,
    )
    db_session.add(explicit)
    await db_session.flush()
    explicit_id = explicit.id

    _classifies_as(monkeypatch, {"category": "review", "rating": 4})
    await process_inbound_email(
        _raw_reply(token, "pi.d6c@scripps.edu", "4 excellent"), db_session
    )

    reviews = await _reviews(db_session)
    assert len(reviews) == 1
    assert reviews[0].id == explicit_id
    assert reviews[0].rating == 3, "a real existing rating must not be overwritten"
    # D6 amendment control: an already-explicit row's reviewed_at is untouched —
    # only the rating=-1 upgrade path moves it forward.
    assert reviews[0].reviewed_at == explicit_reviewed_at
    # process_inbound_email's own caller-side bookkeeping is unchanged by D6 — it
    # always marks the notification responded and sends a confirmation once the
    # classifier extracts a valid rating, regardless of whether _handle_review
    # actually wrote anything. Only _handle_review's own write behavior is under
    # test here.
    assert notification.status == "responded"
    assert len(sent_emails) == 1

    async def _must_not_run(*a, **k):
        raise AssertionError("migrate_public_thread_to_private must not run — already acted on")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _must_not_run
    )

    reopened = await inbound._handle_instruction(
        user=recipient, notification=notification, td=td,
        instruction="focus on Y", db=db_session,
    )

    assert reopened is False
    reviews = await _reviews(db_session)
    assert len(reviews) == 1
    assert reviews[0].id == explicit_id
    assert reviews[0].rating == 3, "the existing explicit review must be untouched"
    assert reviews[0].reviewed_at == explicit_reviewed_at, (
        "the already-blocked instruction reopen must not touch reviewed_at either"
    )


# --- 3. Confirmations do not pretend to be reply-able --------------------------


async def test_review_confirmation_says_replies_are_not_monitored(
    db_session, monkeypatch
):
    """A review consumes the token (status='responded' drops later replies), so
    the confirmation must not leave a reply-shaped dead end. Asserted on the
    real SES payload: the note is the no-Reply-To footer _send_simple_email
    appends, not copy the confirmation composes itself."""
    ses = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: ses)
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda e: True)
    token = "confnote" + "e" * 40
    await _world(db_session, recipient_email="pi.delta@scripps.edu", token=token)
    _classifies_as(monkeypatch, {"category": "review", "rating": 3})

    await process_inbound_email(
        _raw_reply(token, "pi.delta@scripps.edu", "3"), db_session
    )

    (confirmation,) = ses.calls
    assert "not monitored" in confirmation["Message"]["Body"]["Text"]["Data"].lower()
    assert "ReplyToAddresses" not in confirmation


async def test_instruction_confirmation_says_replies_are_not_monitored(
    db_session, monkeypatch
):
    ses = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: ses)
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda e: True)
    token = "instrnote" + "f" * 40
    recipient, _, td, notification = await _world(
        db_session, recipient_email="pi.zeta@scripps.edu", token=token
    )

    await inbound._send_instruction_confirmation(recipient, notification, td, db_session)

    (confirmation,) = ses.calls
    assert "not monitored" in confirmation["Message"]["Body"]["Text"]["Data"].lower()
    assert "ReplyToAddresses" not in confirmation


# --- _send_simple_email passes Reply-To through to SES -------------------------


class _RecordingSES:
    def __init__(self):
        self.calls: list[dict] = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "test"}


def test_send_simple_email_sets_reply_to_addresses(monkeypatch):
    ses = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: ses)
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda e: True)

    ok = inbound._send_simple_email(
        "pi@scripps.edu", "subject", "body", reply_to="review+tok@reply.copi.science"
    )

    assert ok is True
    assert ses.calls[0]["ReplyToAddresses"] == ["review+tok@reply.copi.science"]


def test_send_simple_email_omits_reply_to_when_not_given(monkeypatch):
    ses = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: ses)
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda e: True)

    ok = inbound._send_simple_email("pi@scripps.edu", "subject", "body")

    assert ok is True
    assert "ReplyToAddresses" not in ses.calls[0]


def test_send_simple_email_footers_unreplyable_mail(monkeypatch):
    """Without a Reply-To, a natural reply bounces off the apex domain's mail
    forwarding — every such mail must say so, uniformly, not per call site."""
    ses = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: ses)
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda e: True)

    inbound._send_simple_email("pi@scripps.edu", "subject", "body")

    text = ses.calls[0]["Message"]["Body"]["Text"]["Data"]
    assert text.endswith("Replies to this address are not monitored.")


def test_send_simple_email_reply_able_mail_gets_no_footer(monkeypatch):
    """The help email IS monitored (its Reply-To carries the token) — it must
    not claim otherwise."""
    ses = _RecordingSES()
    monkeypatch.setattr("boto3.client", lambda *a, **k: ses)
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda e: True)

    inbound._send_simple_email(
        "pi@scripps.edu", "subject", "body", reply_to="review+tok@reply.copi.science"
    )

    text = ses.calls[0]["Message"]["Body"]["Text"]["Data"]
    assert "not monitored" not in text


def test_reply_address_builder_round_trips_with_the_parser():
    from src.services.email_notifications import build_reply_address

    address = build_reply_address("tok123_-abc")
    assert address.endswith("@" + get_settings().ses_reply_domain)
    assert inbound._extract_reply_token(f"PI <{address}>") == "tok123_-abc"
