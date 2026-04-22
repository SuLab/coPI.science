"""Global append-only message log — single source of truth for the simulation."""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """A single message in the global log."""

    ts: str  # Slack message timestamp (unique ID)
    channel: str
    sender_agent_id: str | None  # None for human PI messages
    sender_name: str
    content: str
    thread_ts: str | None = None  # None for top-level posts
    posted_at: float = 0.0  # Unix timestamp (float(ts))
    is_bot: bool = True
    # Visibility class of the channel this entry was posted in. Drives memory-
    # synthesis filtering (G2) — private-channel entries never feed the public
    # memory segment. Default 'public' is safe for all existing callers.
    # See specs/privacy-and-channel-visibility.md §G2.
    visibility: str = "public"


def is_funding_post(content: str) -> bool:
    """Return True if the message is a funding-related post (marked with :moneybag:)."""
    return ":moneybag:" in content


class MessageLog:
    """
    Append-only in-memory message log.

    All posts and replies are recorded here. Agents query it to find
    new posts since their last turn, thread histories, etc.
    """

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []
        self._by_ts: dict[str, LogEntry] = {}  # ts -> entry for fast lookup
        # Map bot_name (lowercase) -> agent_id, set by SimulationEngine
        self._bot_name_to_id: dict[str, str] = {}

    def set_bot_name_map(self, mapping: dict[str, str]) -> None:
        """Register bot_name -> agent_id mapping (lowercase keys)."""
        self._bot_name_to_id = dict(mapping)

    def append(self, entry: LogEntry) -> None:
        """Add a message to the log."""
        self._entries.append(entry)
        self._by_ts[entry.ts] = entry

    def get_entry(self, ts: str) -> LogEntry | None:
        """Look up a single entry by its timestamp."""
        return self._by_ts.get(ts)

    def get_new_top_level_posts(
        self,
        since: float,
        channels: set[str],
        exclude_agent_id: str,
    ) -> list[LogEntry]:
        """
        Return top-level posts (thread_ts is None) in the given channels,
        posted after `since`, excluding posts from `exclude_agent_id`.
        """
        results = []
        for entry in self._entries:
            if entry.posted_at <= since:
                continue
            if entry.thread_ts is not None:
                continue
            if entry.channel not in channels:
                continue
            if entry.sender_agent_id == exclude_agent_id:
                continue
            results.append(entry)
        return results

    def get_thread_history(self, thread_ts: str) -> list[LogEntry]:
        """Return all messages in a thread (including the root post), ordered by time."""
        root = self._by_ts.get(thread_ts)
        replies = [e for e in self._entries if e.thread_ts == thread_ts]
        result = []
        if root:
            result.append(root)
        result.extend(replies)
        return result

    def get_thread_message_count(self, thread_ts: str) -> int:
        """Count total messages in a thread (root + replies)."""
        count = 1 if thread_ts in self._by_ts else 0
        count += sum(1 for e in self._entries if e.thread_ts == thread_ts)
        return count

    def get_agent_top_level_posts(self, agent_id: str, limit: int = 10) -> list[LogEntry]:
        """Return the agent's own top-level posts, most recent first."""
        posts = [
            e for e in self._entries
            if e.sender_agent_id == agent_id and e.thread_ts is None
        ]
        return posts[-limit:]

    def get_last_bot_sender_in_channel(self, channel_name: str) -> str | None:
        """Return the agent_id of the most recent bot-authored message in a channel.

        Returns None if no bot has posted there yet. Used to enforce
        turn-taking in flat collab_private channels (a bot shouldn't post
        back-to-back there without the other bot responding first).
        """
        for entry in reversed(self._entries):
            if entry.channel != channel_name:
                continue
            if not entry.is_bot:
                continue
            if entry.sender_agent_id:
                return entry.sender_agent_id
        return None

    def get_replies_to_agent_posts(
        self,
        agent_id: str,
        since: float,
    ) -> list[LogEntry]:
        """
        Find replies (since cursor) to top-level posts authored by agent_id,
        where the reply is from a different agent.
        """
        # First, find all top-level posts by this agent
        agent_post_ts = {
            e.ts for e in self._entries
            if e.sender_agent_id == agent_id and e.thread_ts is None
        }
        results = []
        for entry in self._entries:
            if entry.posted_at <= since:
                continue
            if entry.thread_ts not in agent_post_ts:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            results.append(entry)
        return results

    def get_tags_for_agent(
        self,
        agent_bot_name: str,
        since: float,
    ) -> list[LogEntry]:
        """
        Find posts/replies that mention (tag) the given agent bot name,
        posted since the given cursor.
        """
        tag = f"@{agent_bot_name}".lower()
        results = []
        for entry in self._entries:
            if entry.posted_at <= since:
                continue
            if tag in entry.content.lower():
                results.append(entry)
        return results

    def get_thread_allowed_agents(self, thread_ts: str) -> set[str] | None:
        """Return the set of agent_ids allowed to participate in this thread.

        Rules:
        - Funding threads (:moneybag:) are open to all → returns None.
        - If the root post tags a specific agent, only the poster and tagged
          agent may participate → returns {poster, tagged}.
        - If no tag, falls back to generic 2-party rule: the first two distinct
          agents to post are the only allowed participants.
        - Returns None if the thread root is not found.
        """
        root = self._by_ts.get(thread_ts)
        if not root:
            return None

        # Funding threads are open to all participants
        if is_funding_post(root.content):
            return None

        poster_id = root.sender_agent_id

        # Check if root post tags a specific agent (e.g. @WisemanBot)
        tagged_id = self._extract_tagged_agent(root.content)
        if tagged_id and tagged_id != poster_id:
            return {poster_id, tagged_id} if poster_id else {tagged_id}

        # No tag — use generic 2-party rule: first 2 distinct agent_ids in thread.
        # If fewer than 2 participants, the thread is open for anyone to join.
        history = self.get_thread_history(thread_ts)
        participants: list[str] = []
        seen: set[str] = set()
        for entry in history:
            aid = entry.sender_agent_id
            if aid and aid not in seen:
                participants.append(aid)
                seen.add(aid)
            if len(participants) >= 2:
                break
        if len(participants) < 2:
            return None  # Thread still open — anyone can join
        return set(participants)

    def is_funding_thread(self, thread_ts: str) -> bool:
        """Return True if the thread root is a funding post."""
        root = self._by_ts.get(thread_ts)
        return bool(root and is_funding_post(root.content))

    def _extract_tagged_agent(self, content: str) -> str | None:
        """Extract a tagged agent_id from message content (e.g. @WisemanBot)."""
        match = re.search(r"@(\w+[Bb]ot)\b", content)
        if match:
            bot_name = match.group(1).lower()
            return self._bot_name_to_id.get(bot_name)
        return None

    def has_new_reply_from_other(
        self,
        thread_ts: str,
        agent_id: str,
        since: float,
    ) -> bool:
        """Check if the other participant posted a new reply since `since`."""
        for entry in self._entries:
            if entry.thread_ts != thread_ts:
                continue
            if entry.posted_at <= since:
                continue
            if entry.sender_agent_id != agent_id:
                return True
        return False

    @property
    def latest_timestamp(self) -> float:
        """Return the timestamp of the most recent entry, or 0."""
        if not self._entries:
            return 0.0
        return self._entries[-1].posted_at

    def __len__(self) -> int:
        return len(self._entries)
