"""REV4-6 (audit 2026-09-08) and its follow-up, extended by R-3 (audit
2026-09-10): _maybe_send_stale_token_bounce's per-address budget is consumed
only by an outcome that actually reached ``send_raw_email`` (SENT or FAILED —
a post-dispatch failure still means a mail may have left, so an autoresponder
ping-pong stays capped), and NOT by SUPPRESSED (no recipient / allowlist) or
NOT_DISPATCHED (a boto3-client or MIME-construction error before
``send_raw_email`` was ever called) — neither of those ever reached SES.
"""

import pytest

import src.services.email_inbound as inbound
from src.services.email_notifications import SendOutcome


@pytest.mark.asyncio
async def test_an_allowlist_suppressed_recipient_does_not_consume_the_budget(monkeypatch):
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: False)
    sends = []
    monkeypatch.setattr(
        inbound, "send_html_email_outcome",
        lambda *a, **k: sends.append(a) or SendOutcome.SENT,
    )

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("suppressed@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT.get("suppressed@example.com", 0) == 0
    assert sends == [], "the allowlist gate must short-circuit before send_html_email_outcome"


@pytest.mark.asyncio
async def test_a_suppressed_outcome_does_not_consume_the_budget(monkeypatch):
    """Belt-and-braces: even if send_html_email_outcome itself is reached and
    answers SUPPRESSED (e.g. a race on the allowlist), the budget must not
    charge for it — SUPPRESSED means nothing was dispatched to SES."""
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(inbound, "send_html_email_outcome", lambda *a, **k: SendOutcome.SUPPRESSED)

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT.get("real@example.com", 0) == 0


@pytest.mark.asyncio
async def test_a_not_dispatched_outcome_does_not_consume_the_budget(monkeypatch):
    """R-3's root-cause fix: a boto3-client/MIME construction error never
    reached send_raw_email, so it must not be charged like a real send attempt."""
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(
        inbound, "send_html_email_outcome", lambda *a, **k: SendOutcome.NOT_DISPATCHED,
    )

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT.get("real@example.com", 0) == 0


@pytest.mark.asyncio
async def test_a_failed_outcome_consumes_the_budget(monkeypatch):
    """send_raw_email was actually invoked and raised — mail may still have
    left, so this must count against the cap."""
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(inbound, "send_html_email_outcome", lambda *a, **k: SendOutcome.FAILED)

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert (
        inbound._STALE_TOKEN_BOUNCES_SENT["real@example.com"]
        == inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS
    )


@pytest.mark.asyncio
async def test_a_sent_outcome_consumes_the_budget(monkeypatch):
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(inbound, "send_html_email_outcome", lambda *a, **k: SendOutcome.SENT)

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert (
        inbound._STALE_TOKEN_BOUNCES_SENT["real@example.com"]
        == inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS
    )
