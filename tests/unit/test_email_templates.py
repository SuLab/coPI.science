"""Regression tests for shared email styling.

All CoPI emails wrap their content between ``email_shell_open()`` and
``email_shell_close()`` so they share the welcome email's look and an identical
footer. The footer tagline reads "... SU LAB, Scripps Research".
"""

from src.services import email as email_svc
from src.services.email import (
    FOOTER_TAGLINE,
    build_delegate_invitation,
    build_welcome_email,
    email_shell_close,
    email_shell_open,
    send_transactional_email,
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
    _, msg = build_welcome_email("pi@example.com", name="Dr. Example")
    html = _html_part(msg)
    text = next(
        p.get_payload(decode=True).decode("utf-8")
        for p in msg.walk()
        if p.get_content_type() == "text/plain"
    )
    assert html.lstrip().startswith('<div style="font-family')
    assert html.count(FOOTER_TAGLINE) == 1
    # The preference controls and the unsubscribe link left with PI
    # notification email, so the welcome email no longer points at them.
    assert "Manage email preferences" not in html
    assert "emails you receive" not in html
    assert "emails you receive" not in text
    assert ">Unsubscribe</a>" not in html
    assert "/settings/unsubscribe/" not in html
    assert "/settings/unsubscribe/" not in text


async def test_delegate_invitation_uses_shared_branding(monkeypatch):
    """Transactional invite shares the wrapper + tagline but has no unsubscribe."""
    from src.config import get_settings

    # Hermetic: the test's premise is an unrestricted recipient allowlist (the
    # field's own default, "" = send to everyone). Pin it rather than inherit
    # whatever OUTBOUND_EMAIL_ALLOWLIST the deployed .env on this host sets —
    # otherwise send_transactional_email silently no-ops and returns False.
    monkeypatch.setenv("OUTBOUND_EMAIL_ALLOWLIST", "")
    get_settings.cache_clear()
    try:
        captured = {}

        class _FakeSES:
            def send_email(self, **kwargs):
                captured["html"] = kwargs["Message"]["Body"]["Html"]["Data"]

        monkeypatch.setattr(email_svc, "_ses_client", lambda region: _FakeSES())

        assert await send_transactional_email(build_delegate_invitation(
            "colleague@example.com", "Dr. PI", "PIBot", "https://copi.science/invite/abc"
        ))
        html = captured["html"]
        assert html.lstrip().startswith('<div style="font-family')
        assert FOOTER_TAGLINE in html
        assert "Unsubscribe" not in html
    finally:
        get_settings.cache_clear()


async def test_delegate_invitation_escapes_untrusted_names(monkeypatch):
    """PI-chosen pi_name/bot_name must be HTML-escaped in the invite body (SEC-13)."""
    from src.config import get_settings

    # Hermetic for the same reason as the branding test above: pin the allowlist
    # to its default so this test's arbitrary example.com recipient is accepted
    # regardless of the deployed .env's OUTBOUND_EMAIL_ALLOWLIST.
    monkeypatch.setenv("OUTBOUND_EMAIL_ALLOWLIST", "")
    get_settings.cache_clear()
    try:
        captured = {}

        class _FakeSES:
            def send_email(self, **kwargs):
                captured["html"] = kwargs["Message"]["Body"]["Html"]["Data"]
                captured["subject"] = kwargs["Message"]["Subject"]["Data"]

        monkeypatch.setattr(email_svc, "_ses_client", lambda region: _FakeSES())

        assert await send_transactional_email(build_delegate_invitation(
            "colleague@example.com",
            '<img src=x onerror=alert(1)>',
            '<script>alert(2)</script>',
            "https://copi.science/invite/abc",
        ))
        html = captured["html"]
        assert "<img src=x onerror=alert(1)>" not in html
        assert "<script>alert(2)</script>" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "&lt;script&gt;alert(2)&lt;/script&gt;" in html
        # Subject is plain text (not HTML), but must not carry injected newlines.
        assert "\n" not in captured["subject"] and "\r" not in captured["subject"]
    finally:
        get_settings.cache_clear()
