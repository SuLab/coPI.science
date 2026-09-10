"""REV4-6 (audit 2026-09-08) and its follow-up, extended by R-3 and its own
opus-review follow-up (both audit 2026-09-10): _maybe_send_stale_token_bounce's
per-address budget is consumed by SENT, FAILED (send_raw_email was actually
invoked -- a post-dispatch failure still means a mail may have left, so an
autoresponder ping-pong stays capped) and CLIENT_UNAVAILABLE (the SES client
itself failed to construct -- a persistent misconfiguration that will keep
failing every retry, so it must be capped too even though it never reached
SES). It is NOT consumed by SUPPRESSED (no recipient / allowlist) or
NOT_DISPATCHED (a one-off MIME/message-construction error before
``send_raw_email`` was ever called -- a property of THIS message, not a
persistent one) -- neither of those is worth capping retries over. The slot
is reserved before dispatch and refunded only for SUPPRESSED/NOT_DISPATCHED,
closing a check-then-act race a future threaded send could otherwise open.
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
async def test_a_client_unavailable_outcome_consumes_the_budget(monkeypatch):
    """Opus review follow-up, audit 2026-09-10: CLIENT_UNAVAILABLE (the SES
    client itself failed to construct) is a persistent misconfiguration that
    will keep failing every retry -- unlike NOT_DISPATCHED (a one-off
    MIME/message error), it must count against the cap or a broken client
    lets bounce attempts retry unboundedly."""
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(
        inbound, "send_html_email_outcome", lambda *a, **k: SendOutcome.CLIENT_UNAVAILABLE,
    )

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert (
        inbound._STALE_TOKEN_BOUNCES_SENT["real@example.com"]
        == inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS
    )


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


@pytest.mark.asyncio
async def test_the_slot_is_reserved_before_the_send_is_dispatched(monkeypatch):
    """Opus review follow-up, audit 2026-09-10: charging strictly AFTER
    send_html_email_outcome returns is a check-then-act race — a future
    threaded/concurrent send could let two replies from the same address both
    read the same pre-send count and both pass the cap check before either
    charged the budget. Reserving the slot before dispatch (and refunding it
    only for SUPPRESSED/NOT_DISPATCHED) closes that window regardless of how
    the send is implemented. Observed here by asserting the counter is already
    incremented WHILE send_html_email_outcome is running, not only after
    _maybe_send_stale_token_bounce returns."""
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    seen_during_send = []

    def _fake_send(*a, **k):
        seen_during_send.append(inbound._STALE_TOKEN_BOUNCES_SENT.get("real@example.com", 0))
        return SendOutcome.SENT

    monkeypatch.setattr(inbound, "send_html_email_outcome", _fake_send)

    await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert seen_during_send == [1], (
        "the budget must already reflect this attempt while the send is in flight, "
        "not only after it returns"
    )


@pytest.mark.asyncio
async def test_a_refunded_reservation_does_not_leak_across_repeated_suppressions(monkeypatch):
    """The reserve-then-refund dance must net to zero, not merely stay under the
    cap by coincidence — this pins that a SUPPRESSED/NOT_DISPATCHED outcome
    restores the exact pre-call count rather than e.g. floor-clamping at 0."""
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {"real@example.com": 2})
    monkeypatch.setattr("src.services.email.is_allowed_recipient", lambda addr: True)
    monkeypatch.setattr(
        inbound, "send_html_email_outcome", lambda *a, **k: SendOutcome.NOT_DISPATCHED,
    )

    await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT["real@example.com"] == 2
