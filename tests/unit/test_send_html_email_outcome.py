"""``send_html_email_outcome`` distinguishes suppressed-before-dispatch,
SES-client-unavailable, MIME/message-construction-failed, and
dispatch-failed from an actual send (audit 2026-09-10 R-3, split further by
an opus-review follow-up the same day) — ``_send_html_email`` previously
collapsed all of these failure shapes into the same ``False``, so a budget
consumer keyed on "did this go out" could not tell a pre-dispatch suppression
from a real attempt that reached SES, nor a persistent SES-client
misconfiguration (worth capping retries over) from a one-off per-message MIME
error (not worth capping — the next message can still succeed).
"""

import pytest

import src.services.email_notifications as en
from src.services.email_notifications import SendOutcome, send_html_email_outcome


@pytest.fixture(autouse=True)
def _unrestricted_allowlist(monkeypatch):
    """Hermetic: pin the allowlist to its unrestricted default rather than
    inherit whatever OUTBOUND_EMAIL_ALLOWLIST the host's .env sets — mirrors
    tests/unit/test_email_templates.py's delegate-invitation tests."""
    from src.config import get_settings

    monkeypatch.setenv("OUTBOUND_EMAIL_ALLOWLIST", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_no_recipient_is_suppressed():
    assert send_html_email_outcome(None, "s", "t", "<p>h</p>") is SendOutcome.SUPPRESSED


def test_a_disallowed_recipient_is_suppressed(monkeypatch):
    monkeypatch.setenv("OUTBOUND_EMAIL_ALLOWLIST", "allowed.example.com")
    from src.config import get_settings
    get_settings.cache_clear()
    try:
        assert (
            send_html_email_outcome("someone@blocked.example.com", "s", "t", "<p>h</p>")
            is SendOutcome.SUPPRESSED
        )
    finally:
        get_settings.cache_clear()


def test_a_client_construction_failure_is_client_unavailable(monkeypatch):
    """boto3.client(...) itself raising is a persistent misconfiguration (bad
    or missing AWS credentials, a bad region) -- distinct from a one-off
    MIME/message-construction failure below, and from a dispatch that was
    attempted and failed."""
    import boto3

    def _boom(*a, **k):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(boto3, "client", _boom)
    assert (
        send_html_email_outcome("pi@example.com", "s", "t", "<p>h</p>")
        is SendOutcome.CLIENT_UNAVAILABLE
    )


def test_a_mime_construction_failure_is_not_dispatched(monkeypatch):
    """Client construction succeeds; MIME/message construction fails -- a
    property of THIS message (e.g. an unencodable header), not a persistent
    misconfiguration, so it must NOT be CLIENT_UNAVAILABLE."""
    import email.mime.multipart

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: object())

    def _boom(*a, **k):
        raise RuntimeError("bad header")

    monkeypatch.setattr(email.mime.multipart, "MIMEMultipart", _boom)
    assert (
        send_html_email_outcome("pi@example.com", "s", "t", "<p>h</p>")
        is SendOutcome.NOT_DISPATCHED
    )


def test_send_raw_email_raising_is_failed(monkeypatch):
    import boto3

    class _FakeSES:
        def send_raw_email(self, **kwargs):
            raise RuntimeError("read timeout")

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeSES())
    assert (
        send_html_email_outcome("pi@example.com", "s", "t", "<p>h</p>")
        is SendOutcome.FAILED
    )


def test_a_successful_dispatch_is_sent(monkeypatch):
    import boto3

    class _FakeSES:
        def send_raw_email(self, **kwargs):
            return {"MessageId": "abc"}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeSES())
    assert (
        send_html_email_outcome("pi@example.com", "s", "t", "<p>h</p>")
        is SendOutcome.SENT
    )


def test__send_html_email_is_a_thin_bool_wrapper_over_the_outcome(monkeypatch):
    for outcome, expected in (
        (SendOutcome.SUPPRESSED, False),
        (SendOutcome.CLIENT_UNAVAILABLE, False),
        (SendOutcome.NOT_DISPATCHED, False),
        (SendOutcome.FAILED, False),
        (SendOutcome.SENT, True),
    ):
        monkeypatch.setattr(en, "send_html_email_outcome", lambda *a, o=outcome, **k: o)
        assert en._send_html_email("pi@example.com", "s", "t", "<p>h</p>") is expected
