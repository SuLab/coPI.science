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
a failing test rather than a defect discovered in production.

The core is synchronous, because ``slack_sdk.WebClient`` is and because one route
helper has no event loop. **Async callers must use the ``_async``
wrappers at the bottom of this module, not the sync functions.** Six of the seven
call sites are FastAPI route handlers, and a synchronous ``time.sleep`` inside one
of those stalls the whole event loop, not just that request — see ``_call``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.agent.slack_client import (
    MAX_PAGES,
    SLACK_MAX_TEXT_CHARS,
    SLACK_PAGE_LIMIT,
    SlackListingIncomplete,
    split_for_slack,
)

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
    "SlackListingIncomplete",
    "get_user_info",
    "get_user_info_async",
    "join_channel",
    "join_channel_async",
    "list_channel_ids",
    "list_channel_ids_async",
    "lookup_user_by_email",
    "lookup_user_by_email_async",
    "post_message",
    "post_message_async",
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


def list_channel_ids(
    token: str,
    *,
    include_private: bool = True,
    exclude_archived: bool = False,
) -> dict[str, str]:
    """Every channel the token can see, as ``{name: id}``. Fully paginated.

    Raises ``SlackListingIncomplete`` carrying ``.partial`` rather than returning
    a subset that looks whole — a subset is what makes a caller conclude a channel
    does not exist when it is merely on page two.

    ``exclude_archived`` defaults to False because the callers that ask "does this
    name exist" must count archived channels: an archived channel still owns its
    name. Pass True when the answer feeds an action that archived channels cannot
    take, such as joining.
    """
    types = "public_channel,private_channel" if include_private else "public_channel"
    out: dict[str, str] = {}
    cursor = ""
    seen: set[str] = set()
    client = _client(token)

    for page in range(MAX_PAGES):
        call: dict[str, Any] = {
            "types": types,
            "limit": SLACK_PAGE_LIMIT,
            "exclude_archived": exclude_archived,
        }
        if cursor:
            call["cursor"] = cursor
        try:
            result = _call(client, "conversations_list", **call)
        except SlackApiError as exc:
            if page == 0:
                raise
            raise SlackListingIncomplete(
                "conversations.list", out,
                f"page {page + 1} failed: {_error_code(exc) or exc}",
            ) from exc

        for ch in result.get("channels") or []:
            out[ch["name"]] = ch["id"]

        cursor = ((result.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            return out
        if cursor in seen:
            raise SlackListingIncomplete(
                "conversations.list", out, f"Slack repeated cursor {cursor!r}")
        seen.add(cursor)

    raise SlackListingIncomplete("conversations.list", out, f"exceeded {MAX_PAGES} pages")


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


def join_channel(token: str, channel_id: str) -> None:
    """Join a channel. ``already_in_channel`` is success, not failure."""
    try:
        _call(_client(token), "conversations_join", channel=channel_id)
    except SlackApiError as exc:
        if _error_code(exc) == "already_in_channel":
            return
        raise


def post_message(
    token: str,
    channel: str,
    text: str,
    *,
    thread_ts: str | None = None,
) -> list[dict[str, Any]]:
    """Post ``text``, split so no chunk exceeds Slack's limit.

    Returns one record per Slack message actually created. Callers that persist
    what they posted must write one row per returned record, or the DB and Slack
    disagree about how many messages exist — measured live at >4000 characters,
    where Slack silently splits and returns only the last ts.

    ``thread_ts`` exists because two callers post *threaded* replies — the
    legacy PI-guidance path in ``routers/agent_page.py`` and its email
    equivalent in ``services/email_inbound.py``. Without it they could not come
    through here at all: posting their guidance without a ``thread_ts`` would
    move it out of the proposal thread and into the channel root, which is a
    worse defect than the raw client they were using. It is omitted from the
    payload entirely when None, so a top-level post is byte-identical to before
    this parameter existed.
    """
    client = _client(token)
    posted: list[dict[str, Any]] = []
    for chunk in split_for_slack(text, SLACK_MAX_TEXT_CHARS):
        call: dict[str, Any] = {"channel": channel, "text": chunk}
        if thread_ts:
            call["thread_ts"] = thread_ts
        result = _call(client, "chat_postMessage", **call)
        posted.append({
            "ts": result.get("ts"),
            "channel": channel,
            "text": chunk,
            "thread_ts": thread_ts,
        })
    return posted


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
# that request its latency and nothing else. Six of the seven call sites are async;
# _resolve_delegate_names is the remaining sync caller and uses the plain functions.
# ---------------------------------------------------------------------------


async def list_channel_ids_async(
    token: str,
    *,
    include_private: bool = True,
    exclude_archived: bool = False,
) -> dict[str, str]:
    """``list_channel_ids`` off the event loop."""
    return await asyncio.to_thread(
        list_channel_ids, token,
        include_private=include_private, exclude_archived=exclude_archived,
    )


async def lookup_user_by_email_async(token: str, email: str) -> str | None:
    """``lookup_user_by_email`` off the event loop."""
    return await asyncio.to_thread(lookup_user_by_email, token, email)


async def get_user_info_async(token: str, user_id: str) -> dict[str, Any] | None:
    """``get_user_info`` off the event loop."""
    return await asyncio.to_thread(get_user_info, token, user_id)


async def join_channel_async(token: str, channel_id: str) -> None:
    """``join_channel`` off the event loop."""
    return await asyncio.to_thread(join_channel, token, channel_id)


async def post_message_async(
    token: str, channel: str, text: str, *, thread_ts: str | None = None
) -> list[dict[str, Any]]:
    """``post_message`` off the event loop."""
    return await asyncio.to_thread(
        post_message, token, channel, text, thread_ts=thread_ts)
