"""Slack client per agent — Web API only (no Socket Mode).

Uses conversations.history and conversations.replies for polling,
chat.postMessage for posting.
"""

import logging
import re
import time
from typing import Any, Callable

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
    pending_proposals, interesting_posts) when this fires, otherwise they will
    burn API calls re-polling a grave or — worse — post "replies" that Slack
    silently converts to top-level posts because the parent is gone.
    """

    def __init__(self, channel_id: str, thread_ts: str, slack_error: str | None = None):
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.slack_error = slack_error
        super().__init__(
            f"thread {thread_ts} not found in channel {channel_id}"
            + (f" (slack_error={slack_error})" if slack_error else "")
        )


def markdown_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn dialect.

    Key differences handled:
    - **bold** -> *bold*  (double asterisks to single)
    - Standard bullet lists (- item) -> Slack bullet (• item)
    """
    # Convert **bold** → *bold* (but don't touch already-single *)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Convert bullet-list lines: leading "- " to "• "
    text = re.sub(r'^(\s*)- ', r'\1• ', text, flags=re.MULTILINE)
    return text

MAX_RETRIES = 3


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

    def set_visibility_lookup(self, lookup: Callable[[str], str | None]) -> None:
        """Install/replace the visibility lookup after construction."""
        self._visibility_lookup = lookup

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
            self._client.conversations_join(channel=channel_id)
        except SlackApiError:
            pass

    def connect(self) -> bool:
        """Authenticate and cache bot user ID. Returns True on success."""
        if not self.bot_token or self.bot_token.startswith("xoxb-placeholder"):
            logger.warning("[%s] No valid Slack token — running in mock mode", self.agent_id)
            return False

        self._client = WebClient(token=self.bot_token)
        try:
            auth = self._client.auth_test()
            self._bot_user_id = auth["user_id"]
            logger.info(
                "[%s] Connected as %s (%s)",
                self.agent_id, auth["user"], self._bot_user_id,
            )
            return True
        except SlackApiError as exc:
            logger.error("[%s] Slack auth failed: %s", self.agent_id, exc)
            return False

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def _call_with_retry(self, method, **kwargs) -> Any:
        """Call a Slack API method with retry on rate limiting."""
        for attempt in range(MAX_RETRIES):
            try:
                return method(**kwargs)
            except SlackApiError as exc:
                if exc.response.get("error") == "ratelimited":
                    retry_after = int(exc.response.headers.get("Retry-After", 5))
                    logger.warning(
                        "[%s] Rate limited, retrying in %ds (attempt %d/%d)",
                        self.agent_id, retry_after, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                else:
                    raise
        raise SlackApiError("Rate limit retries exhausted", response=exc.response)

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def poll_channel_messages(
        self,
        channel_id: str,
        oldest: str = "0",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch messages from a channel newer than `oldest` timestamp.
        Returns list of raw Slack message dicts, oldest first.
        """
        if not self._client:
            return []
        # Ensure bot is in the channel — rotating pollers across tokens means
        # whichever bot is picked may not yet be a member. Skipped for private
        # channels, which require explicit invite.
        self._try_autojoin(channel_id)
        try:
            result = self._call_with_retry(
                self._client.conversations_history,
                channel=channel_id, oldest=oldest, limit=limit, inclusive=False,
            )
            messages = result.get("messages", [])
            # Filter out system subtypes
            messages = [
                m for m in messages
                if m.get("subtype") not in (
                    "message_deleted", "message_changed",
                    "channel_join", "channel_leave",
                    "channel_purpose", "channel_topic",
                    "channel_name", "channel_archive", "channel_unarchive",
                    "bot_add", "bot_remove",
                )
            ]
            return list(reversed(messages))  # oldest first
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
            result = self._call_with_retry(
                self._client.conversations_replies,
                channel=channel_id, ts=thread_ts, oldest=oldest, inclusive=False,
            )
            messages = result.get("messages", [])
            # First message is always the parent — skip if we only want replies
            return messages
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
        all_messages = []
        cursor = None
        try:
            while True:
                kwargs: dict[str, Any] = {"channel": channel_id, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                result = self._call_with_retry(
                    self._client.conversations_history, **kwargs,
                )
                messages = result.get("messages", [])
                # Filter out system subtypes
                messages = [
                    m for m in messages
                    if m.get("subtype") not in (
                        "message_deleted", "message_changed",
                        "channel_join", "channel_leave",
                        "channel_purpose", "channel_topic",
                        "channel_name", "channel_archive", "channel_unarchive",
                        "bot_add", "bot_remove",
                    )
                ]
                all_messages.extend(messages)
                metadata = result.get("response_metadata", {})
                cursor = metadata.get("next_cursor")
                if not cursor:
                    break
            return list(reversed(all_messages))  # oldest first
        except SlackApiError as exc:
            logger.error("[%s] Failed to get channel history %s: %s", self.agent_id, channel_id, exc)
            return list(reversed(all_messages))

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
        all_messages = []
        cursor = None
        try:
            while True:
                kwargs: dict[str, Any] = {
                    "channel": channel_id, "ts": thread_ts, "limit": 200,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                result = self._call_with_retry(
                    self._client.conversations_replies, **kwargs,
                )
                all_messages.extend(result.get("messages", []))
                metadata = result.get("response_metadata", {})
                cursor = metadata.get("next_cursor")
                if not cursor:
                    break
            return all_messages
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "thread_not_found":
                raise ThreadNotFound(channel_id, thread_ts, err) from exc
            logger.error("[%s] Failed to get thread replies: %s", self.agent_id, exc)
            return all_messages

    # ------------------------------------------------------------------
    # User resolution
    # ------------------------------------------------------------------

    def resolve_user_name(self, user_id: str) -> str:
        """Resolve a Slack user ID to display name."""
        if not user_id or not self._client:
            return user_id or "unknown"
        try:
            info = self._client.users_info(user=user_id)
            user = info.get("user", {})
            return user.get("display_name") or user.get("real_name") or user_id
        except SlackApiError:
            return user_id

    def is_bot_user(self, user_id: str) -> bool:
        """Check if a user ID corresponds to a bot."""
        if not self._client:
            return False
        try:
            info = self._client.users_info(user=user_id)
            user = info.get("user", {})
            return user.get("is_bot", False)
        except SlackApiError:
            return False

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict | None:
        """Post a message to a Slack channel (accepts name or ID)."""
        if not self._client:
            logger.info("[%s] MOCK post to #%s: %s", self.agent_id, channel, text[:80])
            return {"ts": "mock_ts", "channel": channel}

        channel_id = self._resolve_channel_id(channel)
        # Ensure bot is in the channel. Skipped for private channels, which
        # require explicit invite.
        self._try_autojoin(channel_id)

        try:
            # Slack renders the `text` field as mrkdwn by default, so we just
            # need to translate standard markdown (**bold**, - bullets) to
            # Slack's dialect before posting. Using blocks here would trigger
            # Slack's "See more" truncation on long messages.
            slack_text = markdown_to_mrkdwn(text)
            kwargs: dict[str, Any] = {"channel": channel_id, "text": slack_text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            result = self._call_with_retry(self._client.chat_postMessage, **kwargs)
            data = result.data

            # Detect the silent orphan case: Slack accepts chat.postMessage with
            # thread_ts pointing at a deleted parent but drops the thread_ts and
            # creates a top-level message. Left alone, each deleted-root
            # produces a cascade of top-level "replies" that other agents then
            # pick up as fresh roots. Delete our orphan and signal the caller
            # to evict the dead thread_ts from state.
            if thread_ts:
                posted_thread_ts = (data.get("message") or {}).get("thread_ts")
                if posted_thread_ts != thread_ts:
                    orphan_ts = data.get("ts")
                    if orphan_ts:
                        try:
                            self._client.chat_delete(channel=channel_id, ts=orphan_ts)
                        except SlackApiError as delete_exc:
                            logger.warning(
                                "[%s] Failed to delete orphan post %s in #%s: %s",
                                self.agent_id, orphan_ts, channel, delete_exc,
                            )
                    raise ThreadNotFound(channel_id, thread_ts, "silent_thread_drop")

            return data
        except SlackApiError as exc:
            err = exc.response.get("error")
            if err == "thread_not_found" and thread_ts:
                raise ThreadNotFound(channel_id, thread_ts, err) from exc
            if err in ("channel_not_found", "not_in_channel") and self._is_private_channel(channel_id):
                raise BotNotInvitedToPrivateChannel(self.agent_id, channel_id, err) from exc
            logger.error("[%s] Failed to post to #%s: %s", self.agent_id, channel, exc)
            return None

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
            result = self._call_with_retry(self._client.conversations_open, users=user_id)
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
        """Create a new Slack channel."""
        if not self._client:
            logger.info("[%s] MOCK create channel: #%s", self.agent_id, name)
            return {"id": f"mock_{name}", "name": name}
        try:
            result = self._client.conversations_create(name=name)
            ch = result["channel"]
            self._channel_name_to_id[ch["name"]] = ch["id"]
            return ch
        except SlackApiError as exc:
            logger.error("[%s] Failed to create channel %s: %s", self.agent_id, name, exc)
            return None

    def create_private_channel(self, name: str) -> dict | None:
        """Create a new Slack private channel (is_private=true).

        Returns the channel dict on success or None on failure. The creating
        bot is automatically a member; additional members must be added via
        invite_to_channel. See specs/privacy-and-channel-visibility.md for
        the full migration flow.
        """
        if not self._client:
            logger.info("[%s] MOCK create private channel: #%s", self.agent_id, name)
            return {"id": f"mock_priv_{name}", "name": name, "is_private": True}
        try:
            result = self._call_with_retry(
                self._client.conversations_create, name=name, is_private=True,
            )
            ch = result["channel"]
            self._channel_name_to_id[ch["name"]] = ch["id"]
            return ch
        except SlackApiError as exc:
            logger.error(
                "[%s] Failed to create private channel %s: %s",
                self.agent_id, name, exc.response.get("error"),
            )
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
                self._call_with_retry(
                    self._client.conversations_invite, channel=channel_id, users=uid,
                )
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
            self._client.conversations_join(channel=channel_id)
        except SlackApiError as exc:
            logger.warning("[%s] Failed to join channel %s: %s", self.agent_id, channel_id, exc)

    def list_channels(self, include_private: bool = False) -> dict[str, str]:
        """List channels. Returns {name: id} dict.

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
            result = self._client.conversations_list(types=types, limit=200)
            mapping = {ch["name"]: ch["id"] for ch in result.get("channels", [])}
            self._channel_name_to_id.update(mapping)
            return mapping
        except SlackApiError as exc:
            logger.warning("[%s] Failed to list channels: %s", self.agent_id, exc)
            return {}

    def _resolve_channel_id(self, channel: str) -> str:
        """Resolve a channel name to its ID."""
        if channel.startswith("C") or channel.startswith("G"):
            return channel
        if channel in self._channel_name_to_id:
            return self._channel_name_to_id[channel]
        # Refresh cache
        self.list_channels()
        return self._channel_name_to_id.get(channel, channel)

    def get_channel_id(self, channel_name: str) -> str | None:
        """Get channel ID for a channel name, or None."""
        if channel_name in self._channel_name_to_id:
            return self._channel_name_to_id[channel_name]
        self.list_channels()
        return self._channel_name_to_id.get(channel_name)
