"""Global append-only message log — single source of truth for the simulation."""

import logging
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


class MessageLog:
    """
    Append-only in-memory message log.

    All posts and replies are recorded here. Agents query it to find
    new posts since their last turn, thread histories, etc.
    """

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []
        self._by_ts: dict[str, LogEntry] = {}  # ts -> entry for fast lookup

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
