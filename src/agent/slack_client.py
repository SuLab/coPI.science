"""Slack client per agent — Web API only (no Socket Mode).

Uses conversations.history and conversations.replies for polling,
chat.postMessage for posting.

**Every Slack Web API call this module makes goes through one chokepoint**,
``AgentSlackClient._api``. Pagination, rate-limit backoff, the >4000-char split
and inbound response normalisation are therefore properties of the *client*, not
of each call site. That shape exists because the alternative was measured: this
file grew a correct paginator in ``get_full_channel_history`` and never
retrofitted ``list_channels``; grew a retry in ``create_private_channel`` and
never retrofitted ``create_channel``; and normalised ``thread_ts == ts`` in the
engine's Slack reconcile but not in its live poller. Four defects, one structural
absence. ``_api`` takes the endpoint's method *name* rather than a bound
callable so the chokepoint is enforceable: ``self._client.`` appears exactly once
in this module, and ``tests/unit/test_slack_client_contract.py`` asserts that at
the source level.
"""

import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


class BotNotInvitedToPrivateChannel(Exception):
    """Raised when a bot attempts to post to or join a collab_private channel it is not a member of.

    This should only fire in response to a genuine invite-path bug — any private
    channel a bot is asked to act on should have been one the bot was invited to
    at channel-creation time. See specs/agent-system.md §"Auto-join retry must
    gate on visibility".
    """

    def __init__(self, agent_id: str, channel_id: str, slack_error: str | None = None):
        self.agent_id = agent_id
        self.channel_id = channel_id
        self.slack_error = slack_error
        super().__init__(
            f"[{agent_id}] bot is not a member of private channel {channel_id}"
            + (f" (slack_error={slack_error})" if slack_error else "")
        )


class ThreadNotFound(Exception):
    """Raised when a thread_ts points at a deleted/missing parent message.

    Callers must evict the thread_ts from any in-memory state (active_threads,
    pending_proposals) when this fires, otherwise they will burn API calls
    re-polling a grave or — worse — post "replies" that Slack silently
    converts to top-level posts because the parent is gone.
    """

    def __init__(self, channel_id: str, thread_ts: str, slack_error: str | None = None):
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.slack_error = slack_error
        super().__init__(
            f"thread {thread_ts} not found in channel {channel_id}"
            + (f" (slack_error={slack_error})" if slack_error else "")
        )


class SlackListingIncomplete(Exception):
    """A cursor-paginated read stopped before Slack said it was done.

    Raised by ``AgentSlackClient._paginate`` when a page after the first fails,
    when Slack hands back a cursor it has already given us, or when the page
    bound is reached. It carries ``.partial`` — the items collected so far — so a
    caller can degrade deliberately, but it exists so that a *subset can never be
    returned as if it were the whole*. That distinction is the entire production
    bug behind ``list_channels``: a partial channel listing makes
    ``_ensure_seeded_channels`` believe an existing channel is missing, and the
    resulting ``conversations.create`` answers ``name_taken``.
    """

    def __init__(self, method: str, partial: list | dict, reason: str):
        self.method = method
        self.partial = partial
        self.reason = reason
        super().__init__(
            f"{method} pagination incomplete after {len(partial)} item(s): {reason}"
        )


class SlackNotConnected(RuntimeError):
    """``_api`` was reached with no authenticated WebClient behind it.

    Every public method guards on ``self._client`` and takes a mock/no-op path
    instead, so this is a programming error rather than a runtime condition.
    """


def markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn dialect.

    Key differences handled:
    - **bold** -> *bold*  (double asterisks to single)
    - Standard bullet lists (- item) -> Slack bullet (• item)

    Length-safe by construction: ``**x**`` -> ``*x*`` shortens by two characters
    and ``- `` -> ``• `` keeps the same character count, so this never makes a
    string longer. ``split_for_slack`` relies on that — it splits the *source*
    markdown and each resulting chunk is still within the limit after conversion.
    """
    # Convert **bold** → *bold* (but don't touch already-single *)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Convert bullet-list lines: leading "- " to "• "
    text = re.sub(r'^(\s*)- ', r'\1• ', text, flags=re.MULTILINE)
    return text

MAX_RETRIES = 3

# How many times to attempt private-channel creation. Attempt 0 uses a plain
# timestamp suffix; later attempts add random entropy to survive the (extremely
# rare) case of two channels minted in the same second. See create_private_channel.
_MAX_PRIVATE_CHANNEL_ATTEMPTS = 3

# Slack's largest accepted page for every cursor-paginated endpoint we call.
SLACK_PAGE_LIMIT = 200

# Hard bound on a cursor loop. Slack has been observed handing back a cursor it
# already issued; ``_paginate`` also detects the repeat directly, but a bound is
# what guarantees termination if Slack cycles through several cursors instead of
# repeating one. 200 pages x 200 items is far past anything this system holds.
MAX_PAGES = 200

# chat.postMessage splits a longer `text` into several messages *and returns only
# the last chunk's ts*. Measured against the live workspace: 4000 and 4001
# characters arrive as one message; 4050 arrives as two of 4000 + 50 and the
# returned ts is the second. 8192 characters arrive as three of 4000/4000/192 and
# the returned ts is the third. The limit is characters, not bytes — 2000
# three-byte characters (6000 bytes) stay one message. So a client that posts
# blind records a ts naming the *tail* of its own message and leaves every
# earlier chunk in Slack with no database row. `split_for_slack` cuts at this
# boundary ourselves so each Slack message is one we know about.
SLACK_MAX_TEXT_CHARS = 4000

_FENCE = "```"
# Room reserved per chunk to close and reopen a fenced code block that a split
# would otherwise leave unbalanced: len("\n```") + len("```\n").
_FENCE_REPAIR_BUDGET = 2 * (len(_FENCE) + 1)

# Boundaries to prefer when cutting, best first. A cut inside a word is a visible
# corruption; a cut inside a paragraph is merely a pause.
_SPLIT_BOUNDARIES = ("\n\n", "\n", ". ", ", ", " ")


def _cut_at(text: str, budget: int) -> int:
    """Index to cut ``text`` at so the left side is <= ``budget`` characters.

    Prefers a paragraph, line, sentence, clause and finally word boundary, in
    that order, and refuses a boundary that would leave a uselessly small chunk
    (which is how a document full of long lines degenerates into one chunk per
    line). Falls back to a hard cut at ``budget`` for an unbreakable run — a
    2000-character token has no non-corrupting split point.
    """
    window = text[: budget + 1]
    floor = budget // 2
    for sep in _SPLIT_BOUNDARIES:
        idx = window.rfind(sep)
        if idx > floor:
            # Keep the separator on the left for "\n\n"/"\n"/". "/", " so the
            # right side starts at real content; lstrip below removes the rest.
            return idx + len(sep)
    return budget


def _repair_fences(chunks: list[str]) -> list[str]:
    """Close and reopen a ``` code fence that a split left hanging.

    Slack renders ``text`` as mrkdwn, so a chunk ending inside a fenced block
    renders its tail as code and the *next* chunk renders its head as prose —
    the block boundary moves. Balancing each chunk keeps every piece rendering
    the way the whole message would have.
    """
    out: list[str] = []
    open_fence = False
    for chunk in chunks:
        body = f"{_FENCE}\n{chunk}" if open_fence else chunk
        if body.count(_FENCE) % 2:
            body = f"{body.rstrip()}\n{_FENCE}"
            open_fence = True
        else:
            open_fence = False
        out.append(body)
    return out


def split_for_slack(text: str, limit: int = SLACK_MAX_TEXT_CHARS) -> list[str]:
    """Split ``text`` into pieces Slack will each accept as ONE message.

    Returns ``[text]`` unchanged when it already fits — a message of exactly
    ``limit`` characters is one message, as measured live. Guarantees:

    - no chunk exceeds ``limit`` characters (before *or* after
      ``markdown_to_mrkdwn``, which never lengthens a string);
    - no non-whitespace character is lost or duplicated;
    - cuts land on a paragraph/line/sentence/word boundary where one exists
      within the budget, and a fenced code block spanning a cut is closed and
      reopened so each chunk renders as the whole would have.

    Splitting the *source* markdown rather than the converted mrkdwn is
    deliberate: it keeps each chunk's text identical to what the database records
    for that chunk, which is what puts ``agent_messages`` in bijection with Slack.
    """
    if len(text) <= limit:
        return [text]
    fenced = _FENCE in text
    # Clamped to >= 1 so the loop below always makes progress. A budget of zero makes
    # ``_cut_at`` return 0, ``rest`` never shrinks, and this hangs the calling turn
    # forever — measured: ``split_for_slack(fenced_text, limit=8)`` never returned,
    # because the fence-repair reserve is 8 characters. Unreachable at the module's own
    # 4000-character limit, but ``limit`` is a parameter and a hang is not a failure mode
    # worth leaving available to a future caller.
    budget = max(1, limit - _FENCE_REPAIR_BUDGET if fenced else limit)
    chunks: list[str] = []
    rest = text
    while len(rest) > budget:
        cut = _cut_at(rest, budget)
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        chunks.append(rest)
    chunks = [c for c in chunks if c.strip()]
    return _repair_fences(chunks) if fenced else chunks


def normalize_inbound_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Normalise one raw inbound Slack message dict. Mutates and returns it.

    Slack sets ``thread_ts == ts`` on a *parent* once it has replies, so a
    conversations.history page hands back thread roots that look like replies to
    themselves. Anything downstream that treats a non-null ``thread_ts`` as "this
    is a reply" then loses the root entirely — ``MessageLog.get_new_top_level_posts``
    skips it, so it never surfaces to any reader of that method (e.g. the hub's
    Phase 3 auto-activation scan), and the next rebuild makes that permanent.
    Nulling it here, at the one point where Slack dicts enter the
    process, is what keeps the rule from being applied in one ingest path and
    forgotten in another.

    Part of the declared inbound contract of ``Transport`` — see
    ``src/agent/transport.py``.
    """
    if msg.get("thread_ts") and msg.get("thread_ts") == msg.get("ts"):
        msg["thread_ts"] = None
    return msg


# Slack message subtypes that are workspace bookkeeping rather than conversation.
_SYSTEM_SUBTYPES = (
    "message_deleted", "message_changed",
    "channel_join", "channel_leave",
    "channel_purpose", "channel_topic",
    "channel_name", "channel_archive", "channel_unarchive",
    "bot_add", "bot_remove",
)


class AgentSlackClient:
    """
    Manages a Slack Web API client for a single agent.
    No Socket Mode — the simulation engine polls for new messages.
    """

    def __init__(
        self,
        agent_id: str,
        bot_token: str,
        visibility_lookup: Callable[[str], str | None] | None = None,
    ):
        self.agent_id = agent_id
        self.bot_token = bot_token
        self._client: WebClient | None = None
        self._bot_user_id: str | None = None
        self._channel_name_to_id: dict[str, str] = {}  # name -> ID cache
        self._dm_channels: dict[str, str] = {}  # user_id -> DM channel_id
        # Channel-visibility lookup: takes a Slack channel_id and returns
        # 'public' | 'collab_private' | None (unknown). Used to gate the
        # auto-join retry so bots never try conversations.join on private
        # channels they weren't invited to. See specs/agent-system.md.
        self._visibility_lookup = visibility_lookup

    # ------------------------------------------------------------------
    # The chokepoint
    # ------------------------------------------------------------------

    def _api(self, method: str, **kwargs) -> Any:
        """Call one Slack Web API endpoint. **Every** call in this class comes here.

        Takes the slack_sdk method *name* rather than a bound callable, which is
        what makes the chokepoint enforceable rather than merely conventional:
        ``self._client.`` appears exactly once in this module (right here), and a
        source-level test in ``tests/unit/test_slack_client_contract.py`` fails if
        a second one appears. A new endpoint therefore inherits the retry/backoff
        path by construction instead of by the author remembering.
        """
        if self._client is None:
            raise SlackNotConnected(
                f"[{self.agent_id}] {method} called with no authenticated client"
            )
        return self._call_with_retry(getattr(self._client, method), **kwargs)

    def _call_with_retry(self, method, **kwargs) -> Any:
        """Call a Slack API method with retry on rate limiting.

        The retry primitive behind ``_api``. Kept as a separate public-ish seam
        because test teardown reaches for endpoints the client has no wrapper for
        (``conversations_archive``) and must still get the backoff.

        ``last_exc`` exists because Python unbinds an ``except ... as exc`` name at the
        end of the except block. Referring to ``exc`` after the loop raised
        ``UnboundLocalError`` instead of the intended ``SlackApiError`` — and callers
        catch ``SlackApiError``, so an exhausted retry escaped ``post_message``'s
        handler entirely and crashed the turn. That happens precisely when Slack is
        throttling us, i.e. when the system is busiest.
        """
        last_exc: SlackApiError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return method(**kwargs)
            except SlackApiError as exc:
                if exc.response.get("error") == "ratelimited":
                    last_exc = exc
                    retry_after = int(exc.response.headers.get("Retry-After", 5))
                    logger.warning(
                        "[%s] Rate limited, retrying in %ds (attempt %d/%d)",
                        self.agent_id, retry_after, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                else:
                    raise
        raise SlackApiError(
            "Rate limit retries exhausted",
            response=last_exc.response if last_exc else None,
        )

    def _paginate(
        self,
        method: str,
        key: str,
        *,
        limit: int = SLACK_PAGE_LIMIT,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """Follow ``response_metadata.next_cursor`` to the end and return every item.

        Every cursor-paginated endpoint this client touches goes through here:
        conversations.list, conversations.history and conversations.replies.
        (users.list and conversations.members are cursor-paginated too but this
        codebase never calls them outside test teardown.)

        Raises ``SlackListingIncomplete`` — carrying whatever was collected — when
        a page after the first fails, when Slack repeats a cursor, or when
        ``MAX_PAGES`` is reached. Raises the underlying ``SlackApiError`` when the
        *first* page fails, so a caller's existing error handling still sees the
        error it expects for "the request did not work at all". The distinction
        matters: "I got 400 of an unknown number of channels" must never be
        indistinguishable from "there are 400 channels".

        An empty page carrying a cursor is followed, not treated as the end —
        Slack does return those.
        """
        items: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor = ""
        for page in range(MAX_PAGES):
            call = dict(kwargs)
            call["limit"] = limit
            if cursor:
                call["cursor"] = cursor
            try:
                result = self._api(method, **call)
            except SlackApiError as exc:
                if page == 0:
                    raise
                raise SlackListingIncomplete(
                    method, items,
                    f"page {page + 1} failed: {exc.response.get('error') if exc.response else exc}",
                ) from exc
            items.extend(result.get(key) or [])
            cursor = ((result.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                return items
            if cursor in seen_cursors:
                raise SlackListingIncomplete(
                    method, items, f"Slack repeated cursor {cursor!r} at page {page + 1}",
                )
            seen_cursors.add(cursor)
        raise SlackListingIncomplete(
            method, items, f"still paginating after {MAX_PAGES} pages",
        )

    # ------------------------------------------------------------------
    # Identity / lifecycle
    # ------------------------------------------------------------------

    def _is_private_channel(self, channel_id: str) -> bool:
        """True only if we positively know the channel is collab_private."""
        if self._visibility_lookup is None:
            return False
        try:
            return self._visibility_lookup(channel_id) == "collab_private"
        except Exception:
            # A bad lookup should not break Slack calls; fail open to public.
            logger.warning("[%s] visibility_lookup raised; treating %s as public", self.agent_id, channel_id)
            return False

    def _try_autojoin(self, channel_id: str) -> None:
        """Best-effort self-join for public channels only.

        Skips entirely for collab_private channels — a bot that wasn't invited
        cannot self-join, and we don't want to hide an invite-path bug behind
        a silently-swallowed Slack error.
        """
        if self._is_private_channel(channel_id):
            return
        try:
            self._api("conversations_join", channel=channel_id)
        except Exception as exc:
            # Best-effort: SlackApiError, socket TimeoutError, SSL/DNS issues
            # must not crash the simulation. Next poll cycle will retry.
            logger.debug("[%s] autojoin failed for %s: %s", self.agent_id, channel_id, exc)

    def connect(self) -> bool:
        """Authenticate and cache bot user ID. Returns True on success."""
        if not self.bot_token or self.bot_token.startswith("xoxb-placeholder"):
            logger.warning("[%s] No valid Slack token — running in mock mode", self.agent_id)
            return False

        self._client = WebClient(token=self.bot_token)
        try:
            auth = self._api("auth_test")
            self._bot_user_id = auth["user_id"]
            logger.info(
                "[%s] Connected as %s (%s)",
                self.agent_id, auth["user"], self._bot_user_id,
            )
            return True
        except SlackApiError as exc:
            # Drop the client. `is_connected` is `self._client is not None`, and nine
            # call sites gate "is Slack usable" on it — the poll-client rotation,
            # _client_for_channel, _ensure_seeded_channels, the mirror branch in
            # _post_message, the PI DM path. Leaving a client behind after a failed
            # auth made every one of them take the Slack-ON path with a dead token, so
            # an invalid_auth degraded into every call failing on every tick instead of
            # into the DB-only mode the design already has.
            logger.error("[%s] Slack auth failed: %s", self.agent_id, exc)
            self._client = None
            return False

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    @staticmethod
    def _conversation_messages(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop workspace bookkeeping, normalise what's left, order it oldest-first.

        Ordering belongs here — one place, all four inbound reads — because Slack's page
        order is not one rule. conversations.history pages *backwards* in time when no
        ``oldest`` is given, and *forwards* from ``oldest`` when one is. Measured against
        the live workspace: five messages, ``oldest`` set to the first and ``limit=2``,
        and page 1 came back as the OLDEST pair (newest-first within the page). So
        reversing the concatenated walk — which is exactly what a single page needed, and
        what this client did — assembled the pages newest-block-first as soon as
        pagination was added. ``_poll_slack_for_human_messages`` advances
        ``_poll_cursors[ch_id]`` to the last message it iterates, so the cursor landed on
        the second-oldest message of the window instead of the newest, and every later
        tick re-polled messages it had already handled: idempotent ``MessageLog.append``
        keeps that from duplicating rows, but a re-polled message would otherwise be
        re-logged each time.

        Sorting by ts depends on no Slack ordering at all, which is the point. The thread
        parent keeps its position for free: it is the oldest message in its thread.
        """
        def _by_ts(msg: dict[str, Any]) -> float:
            try:
                return float(msg.get("ts") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return sorted(
            (
                normalize_inbound_message(m) for m in raw
                if m.get("subtype") not in _SYSTEM_SUBTYPES
            ),
            key=_by_ts,
        )

    def poll_channel_messages(
        self,
        channel_id: str,
        oldest: str = "0",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch messages from a channel newer than `oldest` timestamp.
        Returns list of raw Slack message dicts, oldest first.

        Fully paginated: ``limit`` is the *page* size, which is what Slack's
        ``limit`` means. Before this, a tick that found more than ``limit`` new
        messages got the newest ``limit`` of them and the caller then advanced its
        cursor past the ones it never saw — a silent, permanent loss. Pagination
        here is bounded by the same ``MAX_PAGES`` guard as everything else, and an
        incomplete listing returns ``[]`` rather than a partial window precisely
        so the caller's cursor cannot step over the gap.

        Oldest-first, and ordered by ts rather than by Slack's page order — see
        ``_conversation_messages`` for the measurement that makes the distinction
        matter once there is more than one page.
        """
        if not self._client:
            return []
        # Ensure bot is in the channel — rotating pollers across tokens means
        # whichever bot is picked may not yet be a member. Skipped for private
        # channels, which require explicit invite.
        self._try_autojoin(channel_id)
        try:
            messages = self._paginate(
                "conversations_history", "messages",
                limit=limit, channel=channel_id, oldest=oldest, inclusive=False,
            )
            return self._conversation_messages(messages)
        except SlackListingIncomplete as exc:
            logger.error(
                "[%s] Poll of %s is INCOMPLETE (%s) — dropping the partial window so "
                "the caller's cursor does not skip the messages we could not fetch",
                self.agent_id, channel_id, exc.reason,
            )
            return []
        except SlackApiError as exc:
            if exc.response.get("error") == "channel_not_found" and self._is_private_channel(channel_id):
                raise BotNotInvitedToPrivateChannel(self.agent_id, channel_id, "channel_not_found") from exc
            logger.error("[%s] Failed to poll channel %s: %s", self.agent_id, channel_id, exc)
            return []

    def get_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
        oldest: str = "0",
    ) -> list[dict[str, Any]]:
        """
        Fetch replies in a thread newer than `oldest`.
        Returns list of raw Slack message dicts, oldest first.
        """
        if not self._client:
            return []
        # Same rationale as poll_channel_messages: the rotated poll client may
        # not be a channel member, and conversations.replies also requires it.
        # Skipped for private channels, which require explicit invite.
        self._try_autojoin(channel_id)
        try:
            # First message is always the parent — callers that only want replies
            # filter on ts themselves.
            return self._conversation_messages(self._paginate(
                "conversations_replies", "messages",
                channel=channel_id, ts=thread_ts, oldest=oldest, inclusive=False,
            ))
        except SlackListingIncomplete as exc:
            logger.error(
                "[%s] Thread %s in %s is INCOMPLETE (%s) — returning the partial "
                "history; the caller re-polls from its own cursor",
                self.agent_id, thread_ts, channel_id, exc.reason,
            )
            return self._conversation_messages(exc.partial)
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "thread_not_found":
                raise ThreadNotFound(channel_id, thread_ts, err) from exc
            if err == "channel_not_found" and self._is_private_channel(channel_id):
                raise BotNotInvitedToPrivateChannel(self.agent_id, channel_id, "channel_not_found") from exc
            logger.error("[%s] Failed to get thread replies: %s", self.agent_id, exc)
            return []

    def get_full_channel_history(
        self,
        channel_id: str,
    ) -> list[dict[str, Any]]:
        """
        Fetch all messages from a channel (paginated).
        Returns list of raw Slack message dicts, oldest first.
        """
        if not self._client:
            return []
        try:
            messages = self._paginate(
                "conversations_history", "messages", channel=channel_id,
            )
        except SlackListingIncomplete as exc:
            # Pages run newest-first, so a partial history is missing its OLDEST
            # messages. Harmless here: the DB is the primary store and already has
            # them, this pass only adds what Slack has and the DB lacks, and the
            # poll cursor derived from it still ends at the newest message.
            logger.error(
                "[%s] History of %s is INCOMPLETE (%s) — reconciling the %d message(s) "
                "fetched; the DB rebuild remains the primary source",
                self.agent_id, channel_id, exc.reason, len(exc.partial),
            )
            messages = exc.partial
        except SlackApiError as exc:
            logger.error("[%s] Failed to get channel history %s: %s", self.agent_id, channel_id, exc)
            return []
        return self._conversation_messages(messages)

    def get_all_thread_replies(
        self,
        channel_id: str,
        thread_ts: str,
    ) -> list[dict[str, Any]]:
        """
        Fetch all replies in a thread (paginated).
        Returns list including parent message, oldest first.
        """
        if not self._client:
            return []
        try:
            return self._conversation_messages(self._paginate(
                "conversations_replies", "messages",
                channel=channel_id, ts=thread_ts,
            ))
        except SlackListingIncomplete as exc:
            logger.error(
                "[%s] Thread %s in %s is INCOMPLETE (%s) — returning the %d reply/ies "
                "fetched",
                self.agent_id, thread_ts, channel_id, exc.reason, len(exc.partial),
            )
            return self._conversation_messages(exc.partial)
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "thread_not_found":
                raise ThreadNotFound(channel_id, thread_ts, err) from exc
            logger.error("[%s] Failed to get thread replies: %s", self.agent_id, exc)
            return []

    # ------------------------------------------------------------------
    # User resolution
    # ------------------------------------------------------------------

    def resolve_user_name(self, user_id: str) -> str:
        """Resolve a Slack user ID to display name."""
        if not user_id or not self._client:
            return user_id or "unknown"
        try:
            info = self._api("users_info", user=user_id)
            user = info.get("user", {})
            return user.get("display_name") or user.get("real_name") or user_id
        except SlackApiError:
            return user_id

    def is_bot_user(self, user_id: str) -> bool:
        """Check if a user ID corresponds to a bot."""
        if not self._client:
            return False
        try:
            info = self._api("users_info", user=user_id)
            user = info.get("user", {})
            return user.get("is_bot", False)
        except SlackApiError:
            return False

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def _post_one(
        self,
        channel_id: str,
        channel_label: str,
        text: str,
        thread_ts: str | None,
        *,
        may_raise_thread_not_found: bool,
    ) -> dict | None:
        """Post exactly one chat.postMessage and normalise the response.

        Returns ``{"ts", "channel", "text", "thread_ts"}`` where ``text`` is the
        *source* text this message carries (what the DB should record for it) and
        ``thread_ts`` is the parent **Slack reports**, not the one we asked for —
        so the recorded row always describes the message Slack actually made.
        Returns None on a handled failure.
        """
        slack_text = markdown_to_mrkdwn(text)
        kwargs: dict[str, Any] = {"channel": channel_id, "text": slack_text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            data = self._api("chat_postMessage", **kwargs).data
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "thread_not_found" and thread_ts and may_raise_thread_not_found:
                raise ThreadNotFound(channel_id, thread_ts, err) from exc
            if err in ("channel_not_found", "not_in_channel") and self._is_private_channel(channel_id):
                raise BotNotInvitedToPrivateChannel(self.agent_id, channel_id, err) from exc
            logger.error("[%s] Failed to post to #%s: %s", self.agent_id, channel_label, exc)
            return None

        posted_thread_ts = (data.get("message") or {}).get("thread_ts")

        # Detect the silent orphan case: Slack accepts chat.postMessage with
        # thread_ts pointing at a deleted parent but drops the thread_ts and
        # creates a top-level message. Left alone, each deleted-root
        # produces a cascade of top-level "replies" that other agents then
        # pick up as fresh roots. Delete our orphan and signal the caller
        # to evict the dead thread_ts from state.
        if thread_ts and posted_thread_ts != thread_ts:
            orphan_ts = data.get("ts")
            if orphan_ts:
                try:
                    self._api("chat_delete", channel=channel_id, ts=orphan_ts)
                except SlackApiError as delete_exc:
                    logger.warning(
                        "[%s] Failed to delete orphan post %s in #%s: %s",
                        self.agent_id, orphan_ts, channel_label, delete_exc,
                    )
            if may_raise_thread_not_found:
                raise ThreadNotFound(channel_id, thread_ts, "silent_thread_drop")
            # A continuation chunk: its parent is a message we posted moments ago,
            # so this is not the caller's thread dying. The orphan is already
            # deleted, which keeps Slack and the DB in step; stop here rather than
            # evict a thread that demonstrably exists.
            logger.error(
                "[%s] Slack dropped thread_ts on a continuation chunk in #%s — "
                "the rest of the message was not posted",
                self.agent_id, channel_label,
            )
            return None

        return {
            "ts": data.get("ts"),
            "channel": data.get("channel") or channel_id,
            "text": text,
            "thread_ts": posted_thread_ts,
        }

    def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict | None:
        """Post a message to a Slack channel (accepts name or ID).

        Returns the *first* Slack message's response augmented with
        ``"posted_messages"``: one normalised record per Slack message this call
        actually created, in order. There is more than one exactly when ``text``
        exceeds ``SLACK_MAX_TEXT_CHARS`` and ``split_for_slack`` cut it; a caller
        that records one database row per entry in that list stays in bijection
        with Slack. ``"ts"`` names the FIRST message, which is the one a reply
        must thread onto — Slack's own blind split returned the last, which is how
        ``_slack_parent_ts`` came to thread replies onto a fragment.

        A split *root* post keeps its continuation chunks in the root's own Slack
        thread rather than as further top-level messages: one logical post must
        stay one top-level post, or the hub's Phase 3 auto-activation scan sees N
        fresh roots where the author wrote one.
        """
        if not self._client:
            # Not connected: report "not posted" so the engine mints a unique
            # canonical id via mint_ts (a hardcoded ts here would collide and,
            # under idempotent append, drop real messages). See
            # specs/local-db-conversations.md.
            logger.info("[%s] MOCK post to #%s: %s", self.agent_id, channel, text[:80])
            return None

        channel_id = self._resolve_channel_id(channel)
        # Ensure bot is in the channel. Skipped for private channels, which
        # require explicit invite.
        self._try_autojoin(channel_id)

        chunks = split_for_slack(text)
        if len(chunks) > 1:
            logger.info(
                "[%s] Splitting a %d-char post to #%s into %d Slack messages "
                "(limit %d); Slack would have split it anyway and returned only the "
                "last ts",
                self.agent_id, len(text), channel, len(chunks), SLACK_MAX_TEXT_CHARS,
            )

        posted: list[dict] = []
        for index, chunk in enumerate(chunks):
            # Chunk 0 uses the caller's thread_ts (None for a root). Later chunks
            # of a reply stay in the same thread; later chunks of a root hang off
            # chunk 0 so the post stays a single top-level message.
            parent = thread_ts if (thread_ts or index == 0) else posted[0]["ts"]
            result = self._post_one(
                channel_id, channel, chunk, parent,
                may_raise_thread_not_found=(index == 0),
            )
            if result is None:
                # Never post the tail of a message whose head failed: stop and let
                # the caller record only what actually landed.
                logger.error(
                    "[%s] Post to #%s stopped after %d/%d chunk(s)",
                    self.agent_id, channel, index, len(chunks),
                )
                break
            posted.append(result)

        if not posted:
            return None
        return {**posted[0], "posted_messages": posted}

    # ------------------------------------------------------------------
    # Direct messages
    # ------------------------------------------------------------------

    def open_dm_channel(self, user_id: str) -> str | None:
        """Open a DM channel with a user. Returns the DM channel ID, cached."""
        if user_id in self._dm_channels:
            return self._dm_channels[user_id]
        if not self._client:
            return None
        try:
            result = self._api("conversations_open", users=user_id)
            ch_id = result["channel"]["id"]
            self._dm_channels[user_id] = ch_id
            return ch_id
        except SlackApiError as exc:
            logger.error("[%s] Failed to open DM with %s: %s", self.agent_id, user_id, exc)
            return None

    def send_dm(self, user_id: str, text: str) -> dict | None:
        """Send a DM to a user. Returns message result or None."""
        dm_channel = self.open_dm_channel(user_id)
        if not dm_channel:
            logger.warning("[%s] Cannot send DM — no channel for %s", self.agent_id, user_id)
            return None
        return self.post_message(dm_channel, text)

    def poll_dm_messages(
        self,
        user_id: str,
        oldest: str = "0",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Poll for new DM messages from a specific user."""
        dm_channel = self.open_dm_channel(user_id)
        if not dm_channel:
            return []
        messages = self.poll_channel_messages(dm_channel, oldest=oldest, limit=limit)
        # Filter to only messages from the target user (not from the bot)
        return [m for m in messages if m.get("user") == user_id]

    # ------------------------------------------------------------------
    # Channel operations
    # ------------------------------------------------------------------

    def create_channel(self, name: str) -> dict | None:
        """Create a new Slack channel, or adopt the existing one of that name.

        Goes through the chokepoint, so a ``ratelimited`` is now *retried* rather
        than collapsed into the same ``None`` that means "Slack refused". The
        residual ambiguity is closed from the other side too: ``name_taken`` means
        the channel exists (an archived channel still owns its name), so we look
        it up and return it instead of reporting failure. That is what makes
        ``_ensure_seeded_channels`` self-healing rather than leaving the channel
        with no id at all.
        """
        if not self._client:
            logger.info("[%s] MOCK create channel: #%s", self.agent_id, name)
            return {"id": f"local:{name}", "name": name}
        try:
            result = self._api("conversations_create", name=name)
        except SlackApiError as exc:
            err = exc.response.get("error") if exc.response else None
            if err == "name_taken":
                existing_id = self.get_channel_id(name)
                if existing_id:
                    logger.info(
                        "[%s] #%s already exists (%s) — adopting it",
                        self.agent_id, name, existing_id,
                    )
                    return {"id": existing_id, "name": name}
                logger.error(
                    "[%s] #%s is name_taken but is not in the channel listing — it is "
                    "most likely a private channel this bot cannot see",
                    self.agent_id, name,
                )
                return None
            logger.error(
                "[%s] Failed to create channel %s: %s", self.agent_id, name, err or exc,
            )
            return None
        ch = result["channel"]
        self._channel_name_to_id[ch["name"]] = ch["id"]
        return ch

    def create_private_channel(self, name: str) -> dict | None:
        """Create a new Slack private channel (is_private=true).

        Returns the channel dict on success or None on failure. The creating
        bot is automatically a member; additional members must be added via
        invite_to_channel. See specs/privacy-and-channel-visibility.md for
        the full migration flow.
        """
        # The reopen slug (priv-{a}-{b}-{origin}) is deterministic per agent
        # pair + origin channel, so a second proposal between the same pair in
        # the same channel produces an identical base name that Slack rejects
        # with 'name_taken'. Append a UTC creation timestamp so each refinement
        # gets a unique channel in one shot (no probe-and-increment loop) and
        # records when it was opened. The endpoint's idempotency guard already
        # blocks re-reopening the *same* proposal; the only residual collision
        # — two *different* proposals for the same pair/channel reopened within
        # the same second — is handled by retrying with random entropy.
        # (Slack caps names at 80 chars, so the base is truncated to fit.)
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        for attempt in range(_MAX_PRIVATE_CHANNEL_ATTEMPTS):
            suffix = f"-{ts}" if attempt == 0 else f"-{ts}-{secrets.token_hex(2)}"
            candidate = f"{name[: 80 - len(suffix)].rstrip('-')}{suffix}"
            if not self._client:
                logger.info("[%s] MOCK create private channel: #%s", self.agent_id, candidate)
                return {"id": f"local:{candidate}", "name": candidate, "is_private": True}
            try:
                result = self._api(
                    "conversations_create", name=candidate, is_private=True,
                )
                ch = result["channel"]
                self._channel_name_to_id[ch["name"]] = ch["id"]
                return ch
            except SlackApiError as exc:
                err = exc.response.get("error")
                if err == "name_taken" and attempt < _MAX_PRIVATE_CHANNEL_ATTEMPTS - 1:
                    logger.info(
                        "[%s] Private channel '%s' name_taken — retrying with entropy",
                        self.agent_id, candidate,
                    )
                    continue
                logger.error(
                    "[%s] Failed to create private channel %s: %s",
                    self.agent_id, candidate, err,
                )
                return None
        return None

    def invite_to_channel(self, channel_id: str, user_ids: list[str]) -> bool:
        """Invite one or more Slack user IDs (bots or humans) to a channel.

        Returns True on success. Tolerates per-user errors ('already_in_channel',
        'cant_invite_self') and logs them without failing the whole call — the
        invite is considered successful as long as every user ends up as a member.
        """
        if not user_ids:
            return True
        if not self._client:
            logger.info(
                "[%s] MOCK invite to %s: %s",
                self.agent_id, channel_id, ", ".join(user_ids),
            )
            return True
        # Slack accepts a comma-separated list, but per-user errors abort the
        # call — invite one at a time so tolerable errors don't block others.
        all_ok = True
        for uid in user_ids:
            try:
                self._api("conversations_invite", channel=channel_id, users=uid)
            except SlackApiError as exc:
                err = exc.response.get("error")
                if err in ("already_in_channel", "cant_invite_self"):
                    logger.debug(
                        "[%s] Invite %s -> %s: tolerable (%s)",
                        self.agent_id, uid, channel_id, err,
                    )
                    continue
                logger.error(
                    "[%s] Invite %s -> %s failed: %s",
                    self.agent_id, uid, channel_id, err,
                )
                all_ok = False
        return all_ok

    def join_channel(self, channel_id: str) -> None:
        """Join a Slack channel by ID.

        No-op for collab_private channels — those require explicit invite and
        cannot be self-joined.
        """
        if not self._client:
            return
        if self._is_private_channel(channel_id):
            logger.debug(
                "[%s] Skipping conversations_join for private channel %s — requires invite",
                self.agent_id, channel_id,
            )
            return
        try:
            self._api("conversations_join", channel=channel_id)
        except SlackApiError as exc:
            logger.warning("[%s] Failed to join channel %s: %s", self.agent_id, channel_id, exc)

    def list_channels(
        self,
        include_private: bool = False,
        *,
        exclude_archived: bool = False,
    ) -> dict[str, str]:
        """List channels. Returns {name: id} dict.

        Fully paginated. Raises ``SlackListingIncomplete`` (after caching what it
        did see) rather than returning a subset that looks complete: a subset is
        what makes ``_ensure_seeded_channels`` re-create a channel Slack already
        has, get ``name_taken``, and leave the channel with no id — after which
        every post to it is addressed by name and Slack answers
        ``not_in_channel``.

        ``exclude_archived`` defaults to **False**, i.e. archived channels are
        included, because both callers ask this question to find out whether a
        *name* is in use, and an archived channel still owns its name. Excluding
        them would reintroduce exactly the defect above by a different route.
        Callers that want only channels they can post in pass True.

        Default returns only public channels (original behavior, required for
        the seeded-channel bootstrap). Passing ``include_private=True`` adds
        collab_private channels this bot is a member of — but note that with
        private channels included, Slack's conversations.list behaves
        differently and may omit public channels the bot is not a member of.
        Prefer DB-driven discovery via ``_sync_private_channels_from_db``
        instead of using this flag.
        """
        if not self._client:
            return {}
        types = "public_channel,private_channel" if include_private else "public_channel"
        try:
            channels = self._paginate(
                "conversations_list", "channels",
                types=types, exclude_archived=exclude_archived,
            )
        except SlackListingIncomplete as exc:
            # Caching the partial answer is purely additive — a name->id pair we
            # did see is still correct — but the *return* must not pretend to be
            # the whole workspace.
            self._channel_name_to_id.update(
                {ch["name"]: ch["id"] for ch in exc.partial}
            )
            logger.error(
                "[%s] Channel listing INCOMPLETE: %s", self.agent_id, exc.reason,
            )
            raise
        except SlackApiError as exc:
            logger.warning("[%s] Failed to list channels: %s", self.agent_id, exc)
            return {}
        mapping = {ch["name"]: ch["id"] for ch in channels}
        self._channel_name_to_id.update(mapping)
        return mapping

    def _refresh_channel_cache(self) -> None:
        """Repopulate the name->id cache, tolerating an incomplete listing.

        Name resolution can always fall back to the cache, so an incomplete
        listing degrades to "resolve from what we know" instead of propagating
        into ``post_message``.
        """
        try:
            self.list_channels()
        except SlackListingIncomplete as exc:
            logger.error(
                "[%s] Resolving channel names from the %d channel(s) seen before the "
                "listing failed (%s)",
                self.agent_id, len(exc.partial), exc.reason,
            )
        except SlackApiError as exc:
            logger.warning("[%s] Channel cache refresh failed: %s", self.agent_id, exc)

    def _resolve_channel_id(self, channel: str) -> str:
        """Resolve a channel name to its ID."""
        if channel.startswith("C") or channel.startswith("G"):
            return channel
        if channel in self._channel_name_to_id:
            return self._channel_name_to_id[channel]
        # Refresh cache
        self._refresh_channel_cache()
        return self._channel_name_to_id.get(channel, channel)

    def get_channel_id(self, channel_name: str) -> str | None:
        """Get channel ID for a channel name, or None."""
        if channel_name in self._channel_name_to_id:
            return self._channel_name_to_id[channel_name]
        self._refresh_channel_cache()
        return self._channel_name_to_id.get(channel_name)

    def cache_channel_ids(self, mapping: dict[str, str]) -> None:
        """Seed the name→id cache (engine shares discovered channel ids here)."""
        self._channel_name_to_id.update(mapping)
