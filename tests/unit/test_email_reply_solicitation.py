"""Reply-soliciting email copy must be gated on inbound email being enabled.

Prod (2026-08-11): review emails told PIs "reply to this email to rate it"
while ENABLE_INBOUND_EMAIL was off and the reply pipeline (MX record, S3
bucket, receipt rule) did not exist — every PI who replied got silence plus a
bounce. Until inbound is provisioned AND enabled, outbound mail must direct
PIs to the web dashboard only, and must not carry a Reply-To pointing at the
dead reply domain.
"""

import email
import uuid
from types import SimpleNamespace

import pytest

from src.config import get_settings
from src.services.email import build_welcome_email
from src.services.email_notifications import (
    _send_new_proposal_email,
    send_proposal_notification,
)


class _SESRecorder:
    def __init__(self):
        self.raw_messages: list[str] = []

    def send_raw_email(self, **kwargs):
        self.raw_messages.append(kwargs["RawMessage"]["Data"])
        return {"MessageId": "m-1"}


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


@pytest.fixture
def ses(monkeypatch):
    recorder = _SESRecorder()
    monkeypatch.setattr("boto3.client", lambda *a, **k: recorder)
    monkeypatch.setattr(get_settings(), "outbound_email_allowlist", "")
    return recorder


def _lab():
    user = SimpleNamespace(id=uuid.uuid4(), email="pi@lab.test", name="Ada Alpha")
    agent = SimpleNamespace(id=uuid.uuid4(), agent_id="alpha", bot_name="AlphaBot")
    td = SimpleNamespace(
        id=uuid.uuid4(), summary_text="A joint proposal.", channel="degrader-chem"
    )
    return user, agent, td


def _parts(raw: str) -> tuple[email.message.Message, str, str]:
    msg = email.message_from_string(raw)
    text = html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            text = part.get_payload(decode=True).decode("utf-8")
        elif part.get_content_type() == "text/html":
            html = part.get_payload(decode=True).decode("utf-8")
    return msg, text, html


# --- proposal_review reminder ------------------------------------------------


async def test_review_reminder_is_web_only_while_inbound_is_disabled(
    ses, monkeypatch
):
    monkeypatch.setattr(get_settings(), "enable_inbound_email", False)
    user, agent, td = _lab()

    ok = await send_proposal_notification(
        user=user, thread_decision=td, agent=agent,
        other_bot_name="BetaBot", total_unreviewed=1, db=_FakeDb(),
    )

    assert ok is True
    msg, text, html = _parts(ses.raw_messages[0])
    assert msg["Reply-To"] is None
    assert "Reply to this email" not in text
    assert "Reply to this email" not in html
    assert "/agent/alpha/dashboard" in text  # the web path remains


async def test_review_reminder_solicits_replies_when_inbound_is_enabled(
    ses, monkeypatch
):
    monkeypatch.setattr(get_settings(), "enable_inbound_email", True)
    user, agent, td = _lab()

    await send_proposal_notification(
        user=user, thread_decision=td, agent=agent,
        other_bot_name="BetaBot", total_unreviewed=1, db=_FakeDb(),
    )

    msg, text, html = _parts(ses.raw_messages[0])
    assert msg["Reply-To"].startswith("review+")
    assert msg["Reply-To"].endswith(f"@{get_settings().ses_reply_domain}")
    assert "Reply to this email" in text


# --- new_proposal alert --------------------------------------------------------


async def test_new_proposal_alert_is_web_only_while_inbound_is_disabled(
    ses, monkeypatch
):
    monkeypatch.setattr(get_settings(), "enable_inbound_email", False)
    user, agent, td = _lab()

    ok = await _send_new_proposal_email(user, td, agent, "BetaBot", _FakeDb())

    assert ok is True
    msg, text, html = _parts(ses.raw_messages[0])
    assert msg["Reply-To"] is None
    assert "Reply to this email" not in text
    assert "Reply to this email" not in html
    assert "/agent/alpha/dashboard" in text


async def test_new_proposal_alert_solicits_replies_when_inbound_is_enabled(
    ses, monkeypatch
):
    monkeypatch.setattr(get_settings(), "enable_inbound_email", True)
    user, agent, td = _lab()

    await _send_new_proposal_email(user, td, agent, "BetaBot", _FakeDb())

    msg, text, _ = _parts(ses.raw_messages[0])
    assert msg["Reply-To"].startswith("review+")
    assert "Reply to this email" in text


# --- welcome email -------------------------------------------------------------


def test_welcome_email_omits_reply_instructions_while_inbound_is_disabled(
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "enable_inbound_email", False)
    _, msg = build_welcome_email("pi@lab.test", "Ada")
    _, text, html = _parts(msg.as_string())
    assert "Reply with a rating" not in text
    assert "review" in text.lower()  # web review guidance remains


def test_welcome_email_describes_replying_when_inbound_is_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_inbound_email", True)
    _, msg = build_welcome_email("pi@lab.test", "Ada")
    _, text, _ = _parts(msg.as_string())
    assert "Reply with a rating" in text
