"""The Slack boundary for the web and service layers.

`src/agent/slack_client.py` is the chokepoint for the simulation engine. It was
built because pagination, retry and message splitting had each been reimplemented
— or forgotten — per call site, and four defects turned out to be four instances
of one structural absence. That reasoning applies identically outside the engine,
where eight call sites had constructed `slack_sdk.WebClient` directly: two read a
single 200-item page of a paginated endpoint, none retried a 429, and one posted
unsplit bodies that Slack silently chunked.

This module is the second half of that boundary. `tests/unit/test_slack_boundary.py`
asserts that `slack_sdk` is imported in exactly two modules, so a ninth bypass is
a failing test rather than a defect discovered in production. Every function left
here is a single, unpaginated call that posts nothing, so what this half supplies
is ``_call``'s retry; pagination and message splitting live only in the agent
client.

The core is synchronous, because ``slack_sdk.WebClient`` is and because one route
helper has no event loop. **Async callers must use the ``_async``
wrappers at the bottom of this module, not the sync functions.** Four of the five
call sites run on the event loop (route handlers, and the account-deletion teardown
they call), and a synchronous ``time.sleep`` inside one of those stalls the whole
event loop, not just that request — see ``_call``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5
# Slack can answer a 429 with Retry-After in the tens of seconds. Honouring it
# exactly is right for not getting throttled harder, but three of them would hold a
# request for minutes, so it is capped and the cap is logged. The cap is only safe
# because async callers reach this through the _async wrappers, which run it in a
# worker thread — a bare time.sleep on a route handler's own thread would block
# every other request in the process, not just this one.
_MAX_RETRY_AFTER = 30.0

# Errors that mean "this call will never work", so retrying is pointless.
# ``user_not_found`` is users.info's spelling and ``users_not_found`` is
# users.lookupByEmail's; both are here because a user who does not exist does not
# start existing on attempt four, and the callers that translate them to None sit
# in synchronous request paths where four attempts costs 3.5s of backoff.
_TERMINAL = frozenset({
    "invalid_auth", "account_inactive", "token_revoked", "no_permission",
    "user_not_found", "users_not_found", "channel_not_found", "not_in_channel",
})

__all__ = [
    "get_user_info",
    "lookup_user_by_email",
    "lookup_user_by_email_async",
    "revoke_token",
    "revoke_token_async",
]


def _client(token: str) -> WebClient:
    """Seam for tests; the only WebClient construction in the web layer."""
    return WebClient(token=token)


def _error_code(exc: SlackApiError) -> str:
    """Slack's ``error`` string for a failed call, or ``""`` when it sent none."""
    return (exc.response.get("error") if exc.response else None) or ""


def _call(client: WebClient, method: str, **kwargs: Any) -> Any:
    """One Slack call with bounded retry on rate limits and transient errors.

    Honours ``Retry-After`` when Slack sends it, because guessing is how a
    throttled bot becomes a blocked bot. Terminal errors raise immediately: a
    revoked token does not become valid on attempt four.
    """
    last: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return getattr(client, method)(**kwargs)
        except SlackApiError as exc:
            code = _error_code(exc)
            if code in _TERMINAL:
                raise
            last = exc
            if attempt == _MAX_ATTEMPTS - 1:
                break
            delay = _BACKOFF_BASE * (2 ** attempt)
            if code == "ratelimited":
                retry_after = (getattr(exc.response, "headers", {}) or {}).get("Retry-After")
                if retry_after is not None:
                    try:
                        asked = float(retry_after)
                    except (TypeError, ValueError):
                        asked = delay
                    if asked > _MAX_RETRY_AFTER:
                        logger.warning(
                            "[slack_web] %s asked for Retry-After=%.0fs; capping at %.0fs",
                            method, asked, _MAX_RETRY_AFTER,
                        )
                    delay = min(asked, _MAX_RETRY_AFTER)
            logger.warning("[slack_web] %s failed (%s); retrying in %.1fs", method, code, delay)
            if delay > 0:
                time.sleep(delay)
    assert last is not None
    raise last


def lookup_user_by_email(token: str, email: str) -> str | None:
    """Slack user id for an email, or None when Slack has no such user."""
    try:
        result = _call(_client(token), "users_lookupByEmail", email=email)
    except SlackApiError as exc:
        if _error_code(exc) == "users_not_found":
            return None
        raise
    return ((result.get("user") or {}).get("id")) or None


def get_user_info(token: str, user_id: str) -> dict[str, Any] | None:
    """The ``user`` object for a Slack id, or None when it does not resolve."""
    try:
        result = _call(_client(token), "users_info", user=user_id)
    except SlackApiError as exc:
        if _error_code(exc) in {"user_not_found", "users_not_found"}:
            return None
        raise
    return result.get("user") or None


def revoke_token(token: str) -> bool:
    """Revoke a bot token (auth.revoke). True when the token is dead
    afterwards — including when it already was: a token that is
    ``token_revoked``/``invalid_auth``/``account_inactive`` cannot post, which
    is the outcome revocation exists to guarantee. Exists for the account-
    deletion teardown (docs/audits/2026-08-25-pi-deletion, D3); the app stays
    installed, only this token dies.
    """
    try:
        result = _call(_client(token), "auth_revoke")
    except SlackApiError as exc:
        if _error_code(exc) in {"token_revoked", "invalid_auth", "account_inactive"}:
            return True
        raise
    return bool(result.get("revoked"))


# ---------------------------------------------------------------------------
# Async wrappers — the entry point for every FastAPI route handler.
#
# The sync functions above call slack_sdk, which blocks on network I/O, and _call
# adds up to three time.sleep()s on top of that. Called directly from an `async
# def` route those block the event loop, so ONE throttled Slack call freezes every
# other request the process is serving. That is strictly worse than the raw
# WebClient these functions replaced: it had no retry, so its worst case was a
# single blocking HTTP call rather than four plus backoff.
#
# asyncio.to_thread moves the whole thing to a worker thread, so the wait costs
# that request its latency and nothing else. Four of the five call sites are async;
# _resolve_delegate_names (routers/agent_page.py) is the remaining sync caller. It
# calls get_user_info, which therefore has no wrapper here: the route already runs
# that whole helper through asyncio.to_thread.
# ---------------------------------------------------------------------------


async def lookup_user_by_email_async(token: str, email: str) -> str | None:
    """``lookup_user_by_email`` off the event loop."""
    return await asyncio.to_thread(lookup_user_by_email, token, email)


async def revoke_token_async(token: str) -> bool:
    """``revoke_token`` off the event loop."""
    return await asyncio.to_thread(revoke_token, token)
