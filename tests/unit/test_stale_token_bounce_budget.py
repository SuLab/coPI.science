"""REV4-6 (audit 2026-09-08) and its follow-up: _maybe_send_stale_token_bounce's
per-address budget is consumed by every bounce that reaches SES (a post-dispatch
failure still means a mail may have left, so an autoresponder ping-pong stays
capped), and NOT by an allowlist-suppressed recipient, which never reaches SES.
"""

import pytest

import src.services.email_inbound as inbound


@pytest.mark.asyncio
async def test_an_allowlist_suppressed_recipient_does_not_consume_the_budget(monkeypatch):
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: False)
    sends = []
    monkeypatch.setattr(inbound, "_send_html_email", lambda *a, **k: sends.append(a) or True)

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("suppressed@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT.get("suppressed@example.com", 0) == 0
    assert sends == []


@pytest.mark.asyncio
async def test_a_send_that_reached_ses_consumes_the_budget_even_if_it_reported_failure(monkeypatch):
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(inbound, "_send_html_email", lambda *a, **k: False)

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert (
        inbound._STALE_TOKEN_BOUNCES_SENT["real@example.com"]
        == inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS
    )
