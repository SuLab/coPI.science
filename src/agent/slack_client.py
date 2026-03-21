"""Slack client per agent — manages connection via Socket Mode."""

import asyncio
import logging
import threading
from typing import Any, Callable

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logger = logging.getLogger(__name__)


class AgentSlackClient:
    """
    Manages a Slack Bolt app for a single agent.
    Listens for messages via Socket Mode.
    """

    def __init__(
        self,
        agent_id: str,
        bot_token: str,
        app_token: str,
        on_message: Callable[[dict[str, Any]], None],
    ):
        self.agent_id = agent_id
        self.bot_token = bot_token
        self.app_token = app_token
        self.on_message = on_message
        self._app: App | None = None
        self._handler: SocketModeHandler | None = None
        self._thread: threading.Thread | None = None
        self._bot_user_id: str | None = None

    def start(self) -> None:
        """Start the Slack Socket Mode connection in a background thread."""
        if not self.bot_token or self.bot_token.startswith("xoxb-placeholder"):
            logger.warning("[%s] No valid Slack tokens — running in mock mode", self.agent_id)
            return

        self._app = App(token=self.bot_token)
        self._setup_handlers()

        try:
            auth_result = self._app.client.auth_test()
            self._bot_user_id = auth_result["user_id"]
            logger.info("[%s] Connected as %s (%s)", self.agent_id, auth_result["user"], self._bot_user_id)
        except Exception as exc:
            logger.error("[%s] Slack auth failed: %s", self.agent_id, exc)
            return

        self._handler = SocketModeHandler(self._app, self.app_token)
        self._thread = threading.Thread(
            target=self._handler.start,
            daemon=True,
            name=f"slack-{self.agent_id}",
        )
        self._thread.start()
        logger.info("[%s] Socket Mode handler started", self.agent_id)

    def stop(self) -> None:
        """Stop the Slack connection."""
        if self._handler:
            try:
                self._handler.close()
            except Exception as exc:
                logger.warning("[%s] Error stopping handler: %s", self.agent_id, exc)

    def _setup_handlers(self) -> None:
        """Register Slack event handlers."""
        app = self._app

        @app.event("message")
        def handle_message(event: dict, say, client) -> None:
            # Ignore bot's own messages
            if event.get("bot_id") or event.get("user") == self._bot_user_id:
                return
            # Ignore message deletions/edits
            if event.get("subtype") in ("message_deleted", "message_changed"):
                return

            self.on_message({
                "agent_id": self.agent_id,
                "channel": event.get("channel"),
                "channel_name": self._resolve_channel_name(client, event.get("channel", "")),
                "sender": self._resolve_user_name(client, event.get("user", "")),
                "sender_id": event.get("user"),
                "content": event.get("text", ""),
                "ts": event.get("ts"),
            })

    def _resolve_channel_name(self, client, channel_id: str) -> str:
        if not channel_id:
            return "unknown"
        try:
            info = client.conversations_info(channel=channel_id)
            return info["channel"].get("name", channel_id)
        except Exception:
            return channel_id

    def _resolve_user_name(self, client, user_id: str) -> str:
        if not user_id:
            return "unknown"
        try:
            info = client.users_info(user=user_id)
            user = info.get("user", {})
            return user.get("display_name") or user.get("real_name") or user_id
        except Exception:
            return user_id

    def post_message(self, channel: str, text: str) -> dict | None:
        """Post a message to a Slack channel."""
        if not self._app:
            logger.info("[%s] MOCK post to #%s: %s", self.agent_id, channel, text[:80])
            return {"ts": "mock_ts", "channel": channel}
        try:
            result = self._app.client.chat_postMessage(channel=channel, text=text)
            return result.data
        except Exception as exc:
            logger.error("[%s] Failed to post message: %s", self.agent_id, exc)
            return None

    def create_channel(self, name: str) -> dict | None:
        """Create a new Slack channel."""
        if not self._app:
            logger.info("[%s] MOCK create channel: #%s", self.agent_id, name)
            return {"id": f"mock_{name}", "name": name}
        try:
            result = self._app.client.conversations_create(name=name)
            return result["channel"]
        except Exception as exc:
            logger.error("[%s] Failed to create channel %s: %s", self.agent_id, name, exc)
            return None

    def join_channel(self, channel_id: str) -> None:
        """Join a Slack channel."""
        if not self._app:
            return
        try:
            self._app.client.conversations_join(channel=channel_id)
        except Exception as exc:
            logger.warning("[%s] Failed to join channel %s: %s", self.agent_id, channel_id, exc)

    def archive_channel(self, channel_id: str) -> bool:
        """Archive a Slack channel."""
        if not self._app:
            logger.info("[%s] MOCK archive channel: %s", self.agent_id, channel_id)
            return True
        try:
            self._app.client.conversations_archive(channel=channel_id)
            return True
        except Exception as exc:
            logger.error("[%s] Failed to archive channel %s: %s", self.agent_id, channel_id, exc)
            return False

    def get_channel_history(self, channel_id: str, limit: int = 20) -> list[dict]:
        """Get recent messages from a channel."""
        if not self._app:
            return []
        try:
            result = self._app.client.conversations_history(channel=channel_id, limit=limit)
            messages = result.get("messages", [])
            return [
                {
                    "sender": m.get("username") or m.get("user") or "bot",
                    "content": m.get("text", ""),
                    "ts": m.get("ts"),
                }
                for m in reversed(messages)
                if not m.get("subtype")
            ]
        except Exception as exc:
            logger.error("[%s] Failed to get channel history: %s", self.agent_id, exc)
            return []
