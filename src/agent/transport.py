"""Message transport abstraction — decouples the engine from Slack.

The simulation talks to a ``Transport`` rather than to Slack directly. Two
implementations exist:

- ``AgentSlackClient`` (``slack_client.py``) — the real Slack Web API client,
  the role the spec calls ``SlackTransport``. It already conforms to this
  Protocol structurally; no subclassing is required.
- ``NullTransport`` — a no-op used when Slack is disabled. Outbound calls do
  nothing (the engine mints a local canonical id via ``mint_ts``); inbound
  polls return nothing (human/PI input arrives through the DB inbox instead).

This lets the whole 5-phase loop and PI polling run with Slack fully off. See
specs/local-db-conversations.md.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

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
    # This token's own Slack identity, learned from ``auth.test`` at connect; None
    # before a successful connect. The poller trusts a message only when one of these
    # matches a connected client (A-02b).
    @property
    def bot_id(self) -> str | None: ...
    @property
    def bot_user_id(self) -> str | None: ...
    def is_bot_user(self, user_id: str) -> bool: ...

    # Outbound
    #
    # ``post_message`` returns None when nothing was sent, else the first message's
    # response dict carrying an extra ``"posted_messages"`` key: one record
    # ``{"ts", "channel", "text", "thread_ts"}`` per message the backend really
    # created, in order, where ``text`` is the source text that message carries and
    # ``thread_ts`` is the parent the backend reports. There is more than one entry
    # exactly when the text had to be split to fit the backend's per-message limit
    # (Slack: 4000 characters). The engine writes one ``agent_messages`` row per
    # entry, which is what keeps the database in bijection with Slack. A backend
    # that never splits may omit the key; ``SimulationEngine._mirrored_messages``
    # falls back to treating the response as a single message.
    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> dict | None: ...
    def create_channel(self, name: str) -> dict | None: ...
    def join_channel(self, channel_id: str) -> None: ...
    # The engine awaits this form (``_phase1_channel_discovery``); the real
    # client runs ``join_channel`` off the loop thread. Part of the contract:
    # a transport without it crashes every Slack-off post turn in Phase 1.
    async def ajoin_channel(self, channel_id: str) -> None: ...
    # Must be complete or raise: a backend that returns a *subset* of the workspace
    # as if it were the whole makes the engine re-create channels that already
    # exist. ``AgentSlackClient`` raises ``SlackListingIncomplete``; callers that
    # can tolerate a partial answer catch it and read ``.partial``.
    # ``exclude_archived`` defaults to False because callers ask this question to
    # find out whether a *name* is taken, and an archived channel still owns its name.
    def list_channels(self, *, exclude_archived: bool = False) -> dict[str, str]: ...
    def get_channel_id(self, channel_name: str) -> str | None: ...
    # Channel name→id cache. The engine seeds this so post_message can resolve a
    # channel passed by name (see _ensure_seeded_channels / private-channel sync).
    # Part of the contract: a backend that omits it crashes the engine at setup.
    def cache_channel_ids(self, mapping: dict[str, str]) -> None: ...

    # Inbound
    #
    # Every returned message dict must already be normalised: a thread *root* whose
    # ``thread_ts`` equals its own ``ts`` (which is how Slack marks a parent that has
    # replies) carries ``thread_ts=None``. Without it the engine ingests a root as a
    # reply to itself and ``MessageLog.get_new_top_level_posts`` drops it, so the post
    # never surfaces to any reader of that method (e.g. the hub's Phase 3
    # auto-activation scan). ``AgentSlackClient`` applies this in
    # ``normalize_inbound_message`` — one place, for both inbound methods.
    def poll_channel_messages(self, channel_id: str, oldest: str = "0", limit: int = 100) -> list[dict[str, Any]]: ...
    def get_full_channel_history(self, channel_id: str) -> list[dict[str, Any]]: ...


class NullTransport:
    """No-op transport used when Slack is disabled (DB is the sole store).

    Reports ``is_connected == False`` so the engine's existing
    ``if client and client.is_connected`` branches take the no-op path, and the
    Slack pollers (which filter on connected clients) simply find nothing.
    Outbound posts return None so ``_post_message`` mints a local canonical id;
    ``create_channel`` returns a ``local:`` id so DB-native channels still work.
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
    def bot_id(self) -> str | None:
        return None

    @property
    def bot_user_id(self) -> str | None:
        return None

    def is_bot_user(self, user_id: str) -> bool:
        return False

    # Outbound — no external side effects
    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> dict | None:
        return None

    def create_channel(self, name: str) -> dict | None:
        return {"id": f"local:{name}", "name": name}

    def join_channel(self, channel_id: str) -> None:
        return None

    async def ajoin_channel(self, channel_id: str) -> None:
        return None

    def list_channels(self, *, exclude_archived: bool = False) -> dict[str, str]:
        # Always complete by construction: the cache *is* the workspace here.
        return dict(self._channel_name_to_id)

    def get_channel_id(self, channel_name: str) -> str | None:
        return self._channel_name_to_id.get(channel_name)

    def cache_channel_ids(self, mapping: dict[str, str]) -> None:
        self._channel_name_to_id.update(mapping)

    # Inbound — nothing arrives via Slack; PI input comes from the DB inbox
    def poll_channel_messages(self, channel_id: str, oldest: str = "0", limit: int = 100) -> list[dict[str, Any]]:
        return []

    def get_full_channel_history(self, channel_id: str) -> list[dict[str, Any]]:
        return []
