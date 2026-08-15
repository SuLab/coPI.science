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
