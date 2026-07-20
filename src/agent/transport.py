"""Message transport abstraction — decouples the engine from Slack.

The simulation talks to a ``Transport`` rather than to Slack directly. Two
implementations exist:

- ``SlackTransport`` — the real Slack Web API client (``AgentSlackClient`` in
  ``slack_client.py`` already conforms to this Protocol structurally; no
  subclassing is required).
- ``NullTransport`` — a no-op used when Slack is disabled. Outbound calls do
  nothing (the engine mints a local canonical id via ``mint_ts``); inbound
  polls return nothing (human/PI input arrives through the DB inbox instead).

This lets the whole 5-phase loop, PI polling and private-channel flows run with
Slack fully off. See specs/local-db-conversations.md.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Transport(Protocol):
    """The Slack surface the engine actually uses.

    Method names match ``AgentSlackClient`` exactly so it conforms without
    changes and the engine's ``slack_clients`` dict needs no renaming.
    """

    agent_id: str

    # Identity / lifecycle
    def connect(self) -> bool: ...
    @property
    def is_connected(self) -> bool: ...
    @property
    def bot_user_id(self) -> str | None: ...
    def resolve_user_name(self, user_id: str) -> str: ...
    def is_bot_user(self, user_id: str) -> bool: ...

    # Outbound
    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> dict | None: ...
    def send_dm(self, user_id: str, text: str) -> dict | None: ...
    def open_dm_channel(self, user_id: str) -> str | None: ...
    def create_channel(self, name: str) -> dict | None: ...
    def create_private_channel(self, name: str) -> dict | None: ...
    def invite_to_channel(self, channel_id: str, user_ids: list[str]) -> bool: ...
    def join_channel(self, channel_id: str) -> None: ...
    def list_channels(self, include_private: bool = False) -> dict[str, str]: ...
    def get_channel_id(self, channel_name: str) -> str | None: ...

    # Inbound
    def poll_channel_messages(self, channel_id: str, oldest: str = "0", limit: int = 100) -> list[dict[str, Any]]: ...
    def get_thread_replies(self, channel_id: str, thread_ts: str, oldest: str = "0") -> list[dict[str, Any]]: ...
    def get_full_channel_history(self, channel_id: str) -> list[dict[str, Any]]: ...
    def get_all_thread_replies(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]: ...
    def poll_dm_messages(self, user_id: str, oldest: str = "0", limit: int = 20) -> list[dict[str, Any]]: ...


class NullTransport:
    """No-op transport used when Slack is disabled (DB is the sole store).

    Reports ``is_connected == False`` so the engine's existing
    ``if client and client.is_connected`` branches take the no-op path, and the
    Slack pollers (which filter on connected clients) simply find nothing.
    Outbound posts return None so ``_post_message`` mints a local canonical id;
    channel-create calls return ``local:`` ids so DB-native channels still work.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        # Present for parity with AgentSlackClient — the engine updates this
        # shared name->id cache in _ensure_seeded_channels / sync paths.
        self._channel_name_to_id: dict[str, str] = {}

    # Identity / lifecycle
    def connect(self) -> bool:
        return True

    @property
    def is_connected(self) -> bool:
        return False

    @property
    def bot_user_id(self) -> str | None:
        return None

    def resolve_user_name(self, user_id: str) -> str:
        return user_id

    def is_bot_user(self, user_id: str) -> bool:
        return False

    def set_visibility_lookup(self, lookup: Callable[[str], str | None]) -> None:
        return None

    # Outbound — no external side effects
    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> dict | None:
        return None

    def send_dm(self, user_id: str, text: str) -> dict | None:
        return None

    def open_dm_channel(self, user_id: str) -> str | None:
        return None

    def create_channel(self, name: str) -> dict | None:
        return {"id": f"local:{name}", "name": name}

    def create_private_channel(self, name: str) -> dict | None:
        return {"id": f"local:{name}", "name": name, "is_private": True}

    def invite_to_channel(self, channel_id: str, user_ids: list[str]) -> bool:
        return True

    def join_channel(self, channel_id: str) -> None:
        return None

    def list_channels(self, include_private: bool = False) -> dict[str, str]:
        return dict(self._channel_name_to_id)

    def get_channel_id(self, channel_name: str) -> str | None:
        return self._channel_name_to_id.get(channel_name)

    # Inbound — nothing arrives via Slack; PI input comes from the DB inbox
    def poll_channel_messages(self, channel_id: str, oldest: str = "0", limit: int = 100) -> list[dict[str, Any]]:
        return []

    def get_thread_replies(self, channel_id: str, thread_ts: str, oldest: str = "0") -> list[dict[str, Any]]:
        return []

    def get_full_channel_history(self, channel_id: str) -> list[dict[str, Any]]:
        return []

    def get_all_thread_replies(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        return []

    def poll_dm_messages(self, user_id: str, oldest: str = "0", limit: int = 20) -> list[dict[str, Any]]:
        return []
