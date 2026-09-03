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

import pytest
from sqlalchemy import select

import src.services.email_inbound as inbound
from src.config import get_settings
from src.models import EmailNotification, ProposalReview
from src.services.email_inbound import process_inbound_email
from tests import factories


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


async def _world(db_session, *, recipient_email, token):
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
        category="proposal_review",
        status="sent",
    )
    db_session.add(notification)
    await db_session.flush()
    return recipient, agent, td, notification


async def _reviews(db_session):
    return (await db_session.execute(select(ProposalReview))).scalars().all()


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


# --- 2c. A failed instruction post notifies the PI and is retried (COR-32) -----


async def test_a_retryable_instruction_failure_notifies_the_pi_and_does_not_retire_the_notification(
    db_session, monkeypatch, sent_emails,
):
    """COR-32: a `_handle_instruction` failure that previously returned False silently (no
    post, no email, but the notification was retired and the S3 object deleted) must now
    email the PI and leave the notification retryable. Forces the blanket except by making
    migrate_public_thread_to_private raise — the DEFAULT-config path
    (enable_private_refinement=True, td.origin_visibility='public', both factory defaults),
    reachable in production today, not just the legacy flag-off path.
    """
    token = "instrfail" + "e" * 40
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.instr@scripps.edu", token=token
    )
    monkeypatch.setattr(inbound, "_INSTRUCTION_FAILURE_EMAILS_SENT", {})
    _classifies_as(monkeypatch, {"category": "instruction", "instruction": "focus on X"})

    async def _boom(*a, **k):
        raise RuntimeError("Slack outage")

    # migrate_public_thread_to_private is imported with a LOCAL `from ... import` inside
    # _handle_instruction, so it is looked up fresh at call time — patching the source
    # module's attribute (not email_inbound's namespace, which has no such name at
    # module scope) is what actually takes effect.
    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _boom
    )

    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(
            _raw_reply(token, "pi.instr@scripps.edu", "please focus on X"), db_session,
        )

    assert notification.status == "sent", "the notification must stay retryable, not retired"
    assert await _reviews(db_session) == []
    (mail,) = sent_emails
    assert mail["to"] == recipient.email
    assert "instruction" in mail["subject"].lower()

    # COR-32 retry cap: process_inbound_email is re-entered on every poll until the S3
    # object is quarantined (MAX_S3_PROCESS_ATTEMPTS) — migrate_public_thread_to_private
    # keeps raising on each retry in this test, so _handle_instruction keeps raising too,
    # but the PI must not get a second identical failure email for the same notification.
    with pytest.raises(inbound.InstructionApplyFailed):
        await process_inbound_email(
            _raw_reply(token, "pi.instr@scripps.edu", "please focus on X"), db_session,
        )
    assert len(sent_emails) == 1, "a second retry sent a second identical failure email"


async def test_a_previously_migrated_proposal_is_not_re_migrated_on_retry(
    db_session, monkeypatch, sent_emails,
):
    """COR-32: migrate_public_thread_to_private is NOT idempotent — a retry after an earlier
    attempt already migrated the thread (refined_in_channel set by that earlier attempt, but
    the review never got added because something failed after the migration) must not mint a
    SECOND private Slack channel. The ProposalReview guard just above this one in
    _handle_instruction only catches a COMPLETED reopen; refined_in_channel is the only marker
    available for a migration that started but never finished."""
    token = "instrretry" + "f" * 39
    recipient, agent, td, notification = await _world(
        db_session, recipient_email="pi.instr2@scripps.edu", token=token
    )
    td.refined_in_channel = "priv-c1234567"
    await db_session.flush()

    # A bare raise here would be swallowed by _handle_instruction's own blanket
    # except-and-return-False (pre-fix), which would make this assertion pass for the
    # WRONG reason. Track the call directly instead.
    called: list[bool] = []

    async def _must_not_run(*a, **k):
        called.append(True)
        raise RuntimeError("migrate_public_thread_to_private must not run on a retry")

    monkeypatch.setattr(
        "src.services.private_channels.migrate_public_thread_to_private", _must_not_run
    )

    reopened = await inbound._handle_instruction(
        user=recipient, notification=notification, td=td,
        instruction="focus on X", db=db_session,
    )

    assert called == [], (
        "migrate_public_thread_to_private ran again on a retry — refined_in_channel, set by "
        "an earlier attempt, should have short-circuited before reaching it"
    )
    assert reopened is False
    assert sent_emails == [], "no NEW failure email is needed — this is not a new failure"


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
    implicit = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
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
    # Every side effect that follows a fresh insert must still fire.
    assert len(sent_emails) == 1, "_send_review_confirmation must still fire on the upgrade path"


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
    implicit = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=-1,
        comment=None,
        submitted_via="engine",
    )
    db_session.add(implicit)
    await db_session.flush()
    implicit_id = implicit.id

    calls: list[str] = []

    class _MigrateResult:
        channel_name = "priv-d6instr"

    async def _migrate_stub(
        db, *, thread_decision, creator_agent_id, creator_pi_user, guidance_text
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
    explicit = ProposalReview(
        thread_decision_id=td.id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,
        rating=3,
        comment="already rated",
        submitted_via="email",
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
