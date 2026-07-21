"""Regression tests for shared email styling.

All CoPI emails wrap their content between ``email_shell_open()`` and
``email_shell_close()`` so they share the welcome email's look and an identical
footer. The footer tagline reads "... SU LAB, Scripps Research".
"""

import types
import uuid

import pytest

from src.services.email import (
    FOOTER_TAGLINE,
    build_welcome_email,
    email_shell_close,
    email_shell_open,
    send_delegate_invitation,
)


def _html_part(msg) -> str:
    return next(
        p.get_payload(decode=True).decode("utf-8")
        for p in msg.walk()
        if p.get_content_type() == "text/html"
    )


def test_footer_tagline_names_su_lab():
    assert FOOTER_TAGLINE == "CoPI — Research Collaboration Platform &bull; SU LAB, Scripps Research"


def test_shell_open_renders_branded_header():
    html = email_shell_open()
    assert html.startswith('<div style="font-family')
    assert "background: #f9fafb" in html  # welcome-email background
    assert ">CoPI</span>" in html


def test_shell_close_with_links_has_tagline_and_both_links():
    html = email_shell_close(
        "https://copi.science/settings",
        "https://copi.science/settings/unsubscribe/TOKEN",
    )
    assert FOOTER_TAGLINE in html
    assert "Manage email preferences" in html
    assert ">Unsubscribe</a>" in html
    assert html.rstrip().endswith("</div>")


def test_shell_close_without_links_is_tagline_only():
    """Transactional emails (e.g. delegate invites) show the tagline, no links."""
    html = email_shell_close()
    assert FOOTER_TAGLINE in html
    assert "Manage email preferences" not in html
    assert "Unsubscribe" not in html


def test_welcome_email_uses_shared_footer():
    _, msg = build_welcome_email(
        "pi@example.com", name="Dr. Example", user_id=str(uuid.uuid4())
    )
    html = _html_part(msg)
    assert html.lstrip().startswith('<div style="font-family')
    assert html.count(FOOTER_TAGLINE) == 1
    assert "Manage email preferences" in html
    assert ">Unsubscribe</a>" in html


def test_delegate_invitation_uses_shared_branding(monkeypatch):
    """Transactional invite shares the wrapper + tagline but has no unsubscribe."""
    captured = {}

    class _FakeSES:
        def send_email(self, **kwargs):
            captured["html"] = kwargs["Message"]["Body"]["Html"]["Data"]

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeSES())

    assert send_delegate_invitation(
        "colleague@example.com", "Dr. PI", "PIBot", "https://copi.science/invite/abc"
    )
    html = captured["html"]
    assert html.lstrip().startswith('<div style="font-family')
    assert FOOTER_TAGLINE in html
    assert "Unsubscribe" not in html


def test_delegate_invitation_escapes_untrusted_names(monkeypatch):
    """PI-chosen pi_name/bot_name must be HTML-escaped in the invite body (SEC-13)."""
    captured = {}

    class _FakeSES:
        def send_email(self, **kwargs):
            captured["html"] = kwargs["Message"]["Body"]["Html"]["Data"]
            captured["subject"] = kwargs["Message"]["Subject"]["Data"]

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeSES())

    assert send_delegate_invitation(
        "colleague@example.com",
        '<img src=x onerror=alert(1)>',
        '<script>alert(2)</script>',
        "https://copi.science/invite/abc",
    )
    html = captured["html"]
    assert "<img src=x onerror=alert(1)>" not in html
    assert "<script>alert(2)</script>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
    # Subject is plain text (not HTML), but must not carry injected newlines.
    assert "\n" not in captured["subject"] and "\r" not in captured["subject"]


@pytest.mark.asyncio
async def test_new_proposal_email_uses_shared_footer(monkeypatch):
    """A DB-backed notification email also renders the shared footer."""
    import src.services.email_notifications as en

    captured = {}

    def _fake_send(to, subject, text_body, html_body, reply_to=None, unsubscribe_url=None):
        captured["html"] = html_body
        return True

    monkeypatch.setattr(en, "_send_html_email", _fake_send)

    class _FakeDB:
        def add(self, _obj):
            pass

        async def flush(self):
            pass

    user = types.SimpleNamespace(id=uuid.uuid4(), email="pi@example.com")
    agent = types.SimpleNamespace(id=uuid.uuid4(), agent_id="su", bot_name="SuBot")
    td = types.SimpleNamespace(
        id=uuid.uuid4(),
        agent_a="su",
        agent_b="lotz",
        summary_text="Joint study of X and Y.",
        channel="drug-repurposing",
    )

    await en._send_new_proposal_email(user, td, agent, "LotzBot", _FakeDB())

    html = captured["html"]
    assert html.lstrip().startswith('<div style="font-family')
    assert html.count(FOOTER_TAGLINE) == 1
    assert "Manage email preferences" in html
    assert ">Unsubscribe</a>" in html
    assert "/settings/unsubscribe/" in html
