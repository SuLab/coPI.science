"""REV4-6 (audit 2026-09-08): _maybe_send_stale_token_bounce's per-address budget
must only be consumed by a bounce that was ACTUALLY sent. `_send_html_email`
returns False (without raising) for an allowlist-suppressed recipient -- see
its own docstring, "Honors the outbound allowlist" -- and previously the
counter was incremented unconditionally before that call, silently burning
the budget on sends that never left the process. In an allowlist-restricted
environment (dev/test) every reply from an address outside the allowlist would
exhaust MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS after a handful of calls that sent
nothing at all.
"""

import pytest

import src.services.email_inbound as inbound


@pytest.mark.asyncio
async def test_a_suppressed_send_does_not_consume_the_budget(monkeypatch):
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr(inbound, "_send_html_email", lambda *a, **k: False)

    for _ in range(inbound.MAX_STALE_TOKEN_BOUNCES_PER_ADDRESS + 3):
        await inbound._maybe_send_stale_token_bounce("suppressed@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT.get("suppressed@example.com", 0) == 0


@pytest.mark.asyncio
async def test_a_successful_send_still_consumes_the_budget(monkeypatch):
    monkeypatch.setattr(inbound, "_STALE_TOKEN_BOUNCES_SENT", {})
    monkeypatch.setattr(inbound, "_send_html_email", lambda *a, **k: True)

    await inbound._maybe_send_stale_token_bounce("real@example.com")

    assert inbound._STALE_TOKEN_BOUNCES_SENT.get("real@example.com", 0) == 1
