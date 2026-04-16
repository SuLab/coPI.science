"""Slack client per agent — Web API only (no Socket Mode).

Uses conversations.history and conversations.replies for polling,
chat.postMessage for posting.
"""

import logging
import re
import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# Slack blocks have a 3000-char limit per text object.
_BLOCK_TEXT_LIMIT = 3000


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

    def __init__(self, agent_id: str, bot_token: str):
        self.agent_id = agent_id
        self.bot_token = bot_token
        self._client: WebClient | None = None
        self._bot_user_id: str | None = None
        self._channel_name_to_id: dict[str, str] = {}  # name -> ID cache
        self._dm_channels: dict[str, str] = {}  # user_id -> DM channel_id

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
        try:
            result = self._call_with_retry(
                self._client.conversations_replies,
                channel=channel_id, ts=thread_ts, oldest=oldest, inclusive=False,
            )
            messages = result.get("messages", [])
            # First message is always the parent — skip if we only want replies
            return messages
        except SlackApiError as exc:
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
        # Ensure bot is in the channel
        try:
            self._client.conversations_join(channel=channel_id)
        except SlackApiError:
            pass

        try:
            slack_text = markdown_to_mrkdwn(text)
            # Build section blocks so Slack renders mrkdwn; split on the
            # 3000-char block limit to avoid API errors on long posts.
            chunks = [
                slack_text[i:i + _BLOCK_TEXT_LIMIT]
                for i in range(0, len(slack_text), _BLOCK_TEXT_LIMIT)
            ]
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": chunk},
                }
                for chunk in chunks
            ]
            kwargs: dict[str, Any] = {
                "channel": channel_id,
                "text": slack_text,   # fallback for notifications / plain clients
                "blocks": blocks,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            result = self._call_with_retry(self._client.chat_postMessage, **kwargs)
            return result.data
        except SlackApiError as exc:
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

    def join_channel(self, channel_id: str) -> None:
        """Join a Slack channel by ID."""
        if not self._client:
            return
        try:
            self._client.conversations_join(channel=channel_id)
        except SlackApiError as exc:
            logger.warning("[%s] Failed to join channel %s: %s", self.agent_id, channel_id, exc)

    def list_channels(self) -> dict[str, str]:
        """List all public channels. Returns {name: id} dict."""
        if not self._client:
            return {}
        try:
            result = self._client.conversations_list(types="public_channel", limit=200)
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
